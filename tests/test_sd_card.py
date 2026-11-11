# ruff: noqa: D100, D102, INP001, PT009, PT027, SLF001

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk import sd_card
from custom_components.eufy_sdk.sd_card import (
    EufySdAutoSwitch,
    can_sd,
    listing,
    sd_auto_switches,
    sd_recordings_attribute,
    thumbnail_path,
)
from custom_components.eufy_sdk.switch import async_setup_entry as switch_setup

CAN = {
    "stream": "/stream/CAM1",
    "power": "battery",
    "canSdRecordings": True,
    "sdAuto": False,
}
OLD_BRIDGE = {"stream": "/stream/CAM2", "power": "battery"}

RECORDINGS = [
    {
        "file": "2026-11-10/080102-080132.mp4",
        "start": "2026-11-10T08:01:02",
        "end": "2026-11-10T08:01:32",
        "kinds": "person",
        "size": 4200000,
        "local": False,
        "downloading": True,
        "thumbnail": True,
    },
    {
        "file": "2026-11-11/120000-120015.mp4",
        "start": "2026-11-11T12:00:00",
        "end": "2026-11-11T12:00:15",
        "kinds": "motion",
        "size": None,
        "local": True,
        "downloading": False,
        "thumbnail": False,
    },
]


def _coordinator(devices: dict[str, dict[str, Any]]) -> SimpleNamespace:
    runtime_data = SimpleNamespace(properties={sn: [] for sn in devices}, client=Mock())
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(runtime_data=runtime_data),
        data=devices,
        solix_devices={},
        recording_enabled=frozenset(),
        recording_active=frozenset(),
    )
    runtime_data.coordinator = coordinator
    return coordinator


class ContractTests(unittest.TestCase):
    """The same contract as the Neolink integration's, for one dashboard window."""

    def test_attribute_and_paths(self) -> None:
        self.assertEqual(
            sd_recordings_attribute(),
            {"list": "eufy_sdk/sd_recordings", "download": "eufy_sdk/sd_download"},
        )
        self.assertEqual(
            thumbnail_path("T8410P2", "2026-11-10/080102-080132.mp4"),
            "/api/eufy_sdk/sd_thumbnail/T8410P2/2026-11-10/080102-080132.jpg",
        )
        self.assertTrue(can_sd(CAN))
        self.assertFalse(can_sd(OLD_BRIDGE))
        self.assertFalse(can_sd(None))

    def test_the_list_signs_the_thumbnails_it_has(self) -> None:
        with patch.object(
            sd_card,
            "async_sign_path",
            side_effect=lambda _h, path, *_a, **_k: f"{path}?authSig=x",
        ):
            result = listing(
                Mock(),
                "CAM1",
                {"updated": 1794406699, "refreshing": True, "recordings": RECORDINGS},
                None,
            )
        self.assertEqual(result["updated"], "2026-11-11T14:18:19+00:00")
        self.assertTrue(result["refreshing"])
        first, second = result["recordings"]
        self.assertEqual(
            first["thumbnail"],
            "/api/eufy_sdk/sd_thumbnail/CAM1/2026-11-10/080102-080132.jpg?authSig=x",
        )
        self.assertTrue(first["downloading"])
        self.assertIsNone(second["thumbnail"])
        self.assertTrue(second["local"])
        self.assertIsNone(listing(Mock(), "CAM1", {}, None)["updated"])

    def test_only_files_named_like_the_list(self) -> None:
        self.assertTrue(sd_card.FILE.match("2026-11-10/080102-080132.mp4"))
        self.assertFalse(sd_card.FILE.match("../../etc/passwd"))
        self.assertFalse(sd_card.THUMBNAIL.match("x/../y.jpg"))


class CameraLookupTests(unittest.TestCase):
    """Only eufy cameras the bridge keeps recordings of."""

    def test_a_camera_the_bridge_keeps_no_recordings_of_is_refused(self) -> None:
        coordinator = _coordinator({"CAM2": OLD_BRIDGE})
        entity = SimpleNamespace(
            platform=SimpleNamespace(platform_name="eufy_sdk"),
            coordinator=coordinator,
            _sn="CAM2",
        )
        hass = Mock()
        hass.data = {sd_card.DATA_COMPONENT: Mock(get_entity=Mock(return_value=entity))}
        with self.assertRaises(HomeAssistantError):
            sd_card._camera(hass, "camera.cam2")
        hass.data[sd_card.DATA_COMPONENT].get_entity.return_value = None
        with self.assertRaises(HomeAssistantError):
            sd_card._camera(hass, "camera.other")


class SdAutoSwitchTests(unittest.IsolatedAsyncioTestCase):
    """The automatic copy, a bridge setting."""

    async def test_only_cameras_the_bridge_keeps_recordings_of_get_the_switch(
        self,
    ) -> None:
        coordinator = _coordinator({"CAM1": dict(CAN), "CAM2": OLD_BRIDGE})
        self.assertEqual([s._sn for s in sd_auto_switches(coordinator)], ["CAM1"])
        entry = SimpleNamespace(runtime_data=coordinator.config_entry.runtime_data)
        added: list[Any] = []
        await switch_setup(Mock(), entry, added.extend)
        self.assertEqual(
            [e._sn for e in added if isinstance(e, EufySdAutoSwitch)], ["CAM1"]
        )

    async def test_the_switch_writes_through_the_bridge(self) -> None:
        coordinator = _coordinator({"CAM1": dict(CAN)})
        client = coordinator.config_entry.runtime_data.client
        client.rpc = AsyncMock(
            side_effect=lambda _cmd, **a: {"ok": True, "on": a["on"]}
        )
        switch = EufySdAutoSwitch(coordinator, "CAM1")
        switch.async_write_ha_state = Mock()
        self.assertEqual(switch.unique_id, "CAM1_sd_auto")
        self.assertEqual(switch.entity_category, EntityCategory.CONFIG)
        self.assertEqual(switch.suggested_object_id, "auto_copy_recordings")
        self.assertFalse(switch.is_on)

        await switch.async_turn_on()
        client.rpc.assert_awaited_with("sd_auto", sn="CAM1", on=True)
        self.assertTrue(switch.is_on)
        await switch.async_turn_off()
        client.rpc.assert_awaited_with("sd_auto", sn="CAM1", on=False)
        self.assertFalse(switch.is_on)
