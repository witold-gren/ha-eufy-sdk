"""
The recordings a camera or its HomeBase keeps, for dashboards.

The same contract as the Neolink integration's, so one dashboard window serves both: a
camera that can (the bridge's `canSdRecordings`) tells where in its `sd_recordings`
attribute, `{"list": "eufy_sdk/sd_recordings", "download": "eufy_sdk/sd_download"}`.

- `eufy_sdk/sd_recordings` (`entity_id`, `refresh`, optional `thumbnails`): the list
  the bridge keeps, at once; `refresh: true` makes the bridge read the recordings again
  in the background (it wakes the camera), `thumbnails` asks for the covers of these
  files. Each recording: `file` (`YYYY-MM-DD/HHMMSS-HHMMSS.mp4`, as the live-view
  recordings are named),
  `start`, `end` (the camera's local time), `kinds`, `size`, `local`, `downloading` and
  `thumbnail` (a signed URL, or null until the bridge has the cover).
- `eufy_sdk/sd_download` (`entity_id`, `files`): copies these recordings to the media
  folder, one after another, in the background.

The "Auto-copy recordings" switch copies the newest recording a few minutes after each
detection (the bridge's `sd_auto`). Nothing on the card or the HomeBase is ever deleted.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from aiohttp import ClientError, web
from homeassistant.components import websocket_api
from homeassistant.components.camera import DATA_COMPONENT
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_HOST, CONF_PORT, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .entity import EufySdkDeviceEntity

if TYPE_CHECKING:
    from .coordinator import EufySdkDataUpdateCoordinator

LIST_COMMAND = f"{DOMAIN}/sd_recordings"
DOWNLOAD_COMMAND = f"{DOMAIN}/sd_download"
THUMBNAIL_URL = "/api/eufy_sdk/sd_thumbnail/{sn}/{day}/{file}"
# How long a thumbnail link stays valid (a dashboard asks for the list every few s).
THUMBNAIL_LINK = timedelta(hours=1)
# How long the bridge may take to answer (a refresh runs in the background).
RPC_TIMEOUT = 30
FILE = re.compile(r"^\d{4}-\d{2}-\d{2}/\d{6}-\d{6}\.mp4$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
THUMBNAIL = re.compile(r"^\d{6}-\d{6}\.jpg$")

# The bridge's device record: whether it can list and copy this camera's recordings, and
# whether it copies the newest one after each detection.
CAN_KEY = "canSdRecordings"
AUTO_KEY = "sdAuto"


def can_sd(record: dict[str, Any] | None) -> bool:
    """Whether the bridge lists and copies this camera's recordings."""
    return bool((record or {}).get(CAN_KEY))


def sd_recordings_attribute() -> dict[str, str]:
    """Return what a camera tells a dashboard about its recordings."""
    return {"list": LIST_COMMAND, "download": DOWNLOAD_COMMAND}


def thumbnail_path(sn: str, file: str) -> str:
    """Return the path of a recording's thumbnail (`file` as the list names it)."""
    day, name = file.split("/", 1)
    return THUMBNAIL_URL.format(sn=sn, day=day, file=name.removesuffix(".mp4") + ".jpg")


def _camera(hass: HomeAssistant, entity_id: str) -> Any:
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    if entity is None or entity.platform.platform_name != DOMAIN:
        msg = f"{entity_id} is not an eufy camera"
        raise HomeAssistantError(msg)
    if not can_sd(entity.coordinator.data.get(entity._sn)):  # noqa: SLF001
        msg = f"{entity_id}: the bridge keeps no recordings of this camera"
        raise HomeAssistantError(msg)
    return entity


def _client(camera: Any) -> Any:
    """Return the bridge client of a camera entity."""
    return camera.coordinator.config_entry.runtime_data.client


def listing(
    hass: HomeAssistant, sn: str, bridge: dict[str, Any], refresh_token_id: str | None
) -> dict[str, Any]:
    """Return the bridge's list as the dashboard contract has it."""
    updated = bridge.get("updated")
    return {
        "updated": datetime.fromtimestamp(updated, UTC).isoformat()
        if updated
        else None,
        "refreshing": bool(bridge.get("refreshing")),
        "error": bridge.get("error"),
        "recordings": [
            {
                "file": r["file"],
                "start": r["start"],
                "end": r["end"],
                "kinds": r.get("kinds"),
                "size": r.get("size"),
                "local": bool(r.get("local")),
                "downloading": bool(r.get("downloading")),
                "thumbnail": async_sign_path(
                    hass,
                    thumbnail_path(sn, r["file"]),
                    THUMBNAIL_LINK,
                    refresh_token_id=refresh_token_id,
                )
                if r.get("thumbnail")
                else None,
            }
            for r in bridge.get("recordings") or []
        ],
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): LIST_COMMAND,
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("refresh", default=False): bool,
        vol.Optional("thumbnails"): [vol.Match(FILE)],
    }
)
@websocket_api.async_response
async def ws_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer with the recordings of the camera."""
    try:
        camera = _camera(hass, msg["entity_id"])
        args: dict[str, Any] = {"sn": camera._sn, "refresh": msg["refresh"]}  # noqa: SLF001
        if msg.get("thumbnails"):
            args["thumbnails"] = msg["thumbnails"]
        bridge = await _client(camera).rpc("sd_recordings", timeout=RPC_TIMEOUT, **args)
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "sd_recordings_failed", str(err))
        return
    connection.send_result(
        msg["id"],
        listing(hass, camera._sn, bridge, connection.refresh_token_id),  # noqa: SLF001
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): DOWNLOAD_COMMAND,
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("files"): vol.All([vol.Match(FILE)], vol.Length(min=1)),
    }
)
@websocket_api.async_response
async def ws_download(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Copy recordings of the camera to the media folder."""
    try:
        camera = _camera(hass, msg["entity_id"])
        await _client(camera).rpc(
            "sd_sync",
            timeout=RPC_TIMEOUT,
            sn=camera._sn,  # noqa: SLF001
            files=msg["files"],
        )
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "sd_download_failed", str(err))
        return
    connection.send_result(msg["id"], {"started": True})


class EufySdThumbnailView(HomeAssistantView):
    """`GET /api/eufy_sdk/sd_thumbnail/<sn>/<day>/<HHMMSS-HHMMSS>.jpg`."""

    url = THUMBNAIL_URL
    name = "api:eufy_sdk:sd_thumbnail"
    requires_auth = True

    async def get(
        self, request: web.Request, sn: str, day: str, file: str
    ) -> web.Response:
        """Return the cover the bridge keeps, never waking the camera."""
        if not DAY.match(day) or not THUMBNAIL.match(file):
            return web.Response(status=400)
        hass = request.app[KEY_HASS]
        entry = next(
            (
                entry
                for entry in hass.config_entries.async_loaded_entries(DOMAIN)
                if sn in (entry.runtime_data.coordinator.data or {})
            ),
            None,
        )
        if entry is None:
            return web.Response(status=404, text="unknown camera")
        host, port = entry.data[CONF_HOST], int(entry.data[CONF_PORT])
        url = f"http://{host}:{port}/sd_thumbnail/{sn}/{day}/{file}"
        try:
            async with async_get_clientsession(hass).get(url) as response:
                if response.status != 200:  # noqa: PLR2004
                    return web.Response(status=response.status, text="no thumbnail yet")
                jpeg = await response.read()
        except (ClientError, TimeoutError) as err:
            return web.Response(status=502, text=str(err))
        return web.Response(
            body=jpeg,
            content_type="image/jpeg",
            headers={"Cache-Control": "max-age=86400"},
        )


class EufySdAutoSwitch(EufySdkDeviceEntity, SwitchEntity):
    """
    Copy the newest recording to Home Assistant a few minutes after each detection.

    A bridge setting (`sd_auto`), off until switched on; the recordings can be browsed
    and copied by hand either way.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:sd"
    _attr_translation_key = "sd_auto"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a camera serial."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_sd_auto"

    @property
    def suggested_object_id(self) -> str:
        """`switch.<camera>_auto_copy_recordings`, whatever language HA runs in."""
        return "auto_copy_recordings"

    @property
    def is_on(self) -> bool:
        """On when the bridge copies the newest recording after each detection."""
        return bool(self.device.get(AUTO_KEY))

    async def async_turn_on(self, **_: Any) -> None:
        """Copy after each detection from now on."""
        await self._set(on=True)

    async def async_turn_off(self, **_: Any) -> None:
        """Stop copying by itself (copies by hand still work)."""
        await self._set(on=False)

    async def _set(self, *, on: bool) -> None:
        """Ask the bridge and show what it answered."""
        reply = await self.client.rpc("sd_auto", sn=self._sn, on=on)
        self.device[AUTO_KEY] = bool(reply.get("on", on))
        self.async_write_ha_state()


def sd_auto_switches(
    coordinator: EufySdkDataUpdateCoordinator,
) -> list[EufySdAutoSwitch]:
    """Return an "Auto-copy recordings" switch per camera with kept recordings."""
    return [
        EufySdAutoSwitch(coordinator, sn)
        for sn, record in coordinator.data.items()
        if can_sd(record)
    ]


@callback
def async_register_sd_card(hass: HomeAssistant) -> None:
    """Register the commands and the view once."""
    key = f"{DOMAIN}_sd_card"
    if hass.data.get(key):
        return
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_download)
    hass.http.register_view(EufySdThumbnailView())
    hass.data[key] = True
