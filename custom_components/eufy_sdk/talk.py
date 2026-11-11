"""
Talk through a camera's speaker from the browser.

The browser sends its microphone as raw 16-bit PCM over a WebSocket to Home
Assistant, which needs the user's login (the page asks for a signed path, so the
socket carries no header). Home Assistant streams the samples on to the bridge's
`/talk/<sn>`, which encodes them for the camera. Closing the socket, or sending
"stop", ends the talk.

On the way the page hears how far it got: `{"state": "connecting"}` at once, and
`{"state": "connected"}` once the camera listens (a camera on a battery or on
Wi-Fi takes seconds to wake). The last message, `{"ok": ...}`, ends the talk.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiohttp import ClientSession

TALK_URL = "/api/eufy_sdk/talk/{sn}"
DEFAULT_RATE = 48_000
MIN_RATE = 8_000
MAX_RATE = 96_000
# How long the bridge may take to answer once the talk ended.
ANSWER_TIMEOUT_S = 15
# The bridge's answers, in words the page can show.
BRIDGE_ERRORS = {
    409: "busy",
    404: "no_speaker",
    415: "unsupported",
    502: "unreachable",
    503: "closed",
}


def talk_rate(value: Any) -> int:
    """Return the browser's sample rate, kept within what a microphone gives."""
    try:
        rate = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RATE
    return rate if MIN_RATE <= rate <= MAX_RATE else DEFAULT_RATE


def bridge_outcome(answer: dict[str, Any], status: int) -> dict[str, Any]:
    """Return the page's outcome of a talk from the bridge's last answer."""
    if answer.get("ok") is True:
        return {"ok": True}
    status = int(answer.get("status", status))
    if status == 200:  # noqa: PLR2004 - an older bridge's plain success
        return {"ok": True}
    return {"ok": False, "error": BRIDGE_ERRORS.get(status, "failed"), "status": status}


async def send_state(ws: web.WebSocketResponse, state: str) -> None:
    """Tell the page how far the talk got, when it still listens."""
    if not ws.closed:
        await ws.send_str(json.dumps({"state": state}))


async def relay_talk(
    ws: web.WebSocketResponse, session: ClientSession, bridge_url: str
) -> dict[str, Any]:
    """Stream the frames of `ws` to the bridge until the browser stops."""
    frames: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def body() -> AsyncIterator[bytes]:
        while (frame := await frames.get()) is not None:
            yield frame

    async def post() -> dict[str, Any]:
        async with session.post(bridge_url, data=body()) as response:
            # A bridge that tells its state answers at once with lines of JSON, the
            # last one being the outcome; an older one answers once, at the end.
            answer: dict[str, Any] = {}
            async for raw in response.content:
                line = raw.strip()
                if not line:
                    continue
                try:
                    answer = json.loads(line)
                except ValueError:
                    continue
                if "state" in answer and "ok" not in answer and "error" not in answer:
                    await send_state(ws, str(answer["state"]))
            return bridge_outcome(answer, response.status)

    await send_state(ws, "connecting")
    sending = asyncio.ensure_future(post())
    try:
        async for message in ws:
            if (
                sending.done()
            ):  # the bridge refused or failed while the browser was still talking
                break
            if message.type == WSMsgType.BINARY:
                frames.put_nowait(message.data)
            elif (
                message.type == WSMsgType.TEXT and message.data == "stop"
            ) or message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        frames.put_nowait(None)
    try:
        return await asyncio.wait_for(sending, ANSWER_TIMEOUT_S)
    except Exception as err:  # noqa: BLE001 - any failure reaching the bridge reads the same to the page
        LOGGER.warning("talk: the bridge did not take the sound: %s", err)
        return {"ok": False, "error": "unreachable"}


class EufySdkTalkView(HomeAssistantView):
    """`GET /api/eufy_sdk/talk/<sn>?rate=<Hz>`: a WebSocket of PCM s16le mono."""

    url = TALK_URL
    name = "api:eufy_sdk:talk"
    requires_auth = True

    async def get(self, request: web.Request, sn: str) -> web.StreamResponse:
        """Relay one talk to the camera's bridge."""
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
        rate = talk_rate(request.query.get("rate"))
        host, port = entry.data[CONF_HOST], int(entry.data[CONF_PORT])
        bridge_url = f"http://{host}:{port}/talk/{sn}?format=s16le&rate={rate}"
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        outcome = await relay_talk(ws, async_get_clientsession(hass), bridge_url)
        if not ws.closed:
            await ws.send_str(json.dumps(outcome))
            await ws.close()
        return ws


def async_register_talk(hass: Any) -> None:
    """Register the talk endpoint once, whatever number of entries are set up."""
    key = f"{DOMAIN}_talk_view"
    if hass.data.get(key):
        return
    hass.http.register_view(EufySdkTalkView())
    hass.data[key] = True
