# ruff: noqa: D100, D102, INP001, PT009, SLF001

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import EntityCategory

from custom_components.eufy_sdk import coordinator as coord_mod
from custom_components.eufy_sdk.api import EufySdkApiClient, EufySdkApiClientError
from custom_components.eufy_sdk.const import EVENT_TYPE
from custom_components.eufy_sdk.switch import EufyRecordLiveSwitch
from custom_components.eufy_sdk.switch import async_setup_entry as switch_setup

BATTERY = {"stream": "/stream/CAM1", "power": "battery", "canRecordLive": True}
MAINS = {"stream": "/stream/CAM2", "power": "wired", "canRecordLive": True}
OLD_BRIDGE = {"stream": "/stream/CAM3", "power": "battery"}

TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components" / "eufy_sdk" / "translations"
)


class RecordLiveGateTests(unittest.IsolatedAsyncioTestCase):
    """The switch exists for battery cameras on a bridge that can record them."""

    async def test_only_battery_cameras_on_a_recording_bridge_get_the_switch(
        self,
    ) -> None:
        coordinator = _coordinator({"CAM1": BATTERY, "CAM2": MAINS, "CAM3": OLD_BRIDGE})
        entry = SimpleNamespace(runtime_data=coordinator.config_entry.runtime_data)
        added: list[Any] = []

        await switch_setup(Mock(), entry, added.extend)

        recorders = [e for e in added if isinstance(e, EufyRecordLiveSwitch)]
        self.assertEqual([e._sn for e in recorders], ["CAM1"])


class RecordLiveSwitchTests(unittest.IsolatedAsyncioTestCase):
    """State from `recording.list`, writes through `recording.set`."""

    def test_identity(self) -> None:
        switch = EufyRecordLiveSwitch(_coordinator({"CAM1": BATTERY}), "CAM1")
        self.assertEqual(switch.unique_id, "CAM1_record_live")
        self.assertEqual(switch.translation_key, "record_live")
        self.assertEqual(switch.entity_category, EntityCategory.CONFIG)
        self.assertTrue(switch.has_entity_name)
        # English object id in any UI language: switch.<camera>_record_live.
        self.assertEqual(switch.suggested_object_id, "record_live")

    def test_off_until_the_bridge_lists_the_camera(self) -> None:
        coordinator = _coordinator({"CAM1": BATTERY})
        switch = EufyRecordLiveSwitch(coordinator, "CAM1")
        self.assertFalse(switch.is_on)
        self.assertEqual(switch.extra_state_attributes, {"recording": False})

        coordinator.recording_enabled = frozenset({"CAM1"})
        coordinator.recording_active = frozenset({"CAM1"})
        self.assertTrue(switch.is_on)
        self.assertEqual(switch.extra_state_attributes, {"recording": True})

    async def test_turn_on_and_off_go_through_the_bridge(self) -> None:
        coordinator = _coordinator({"CAM1": BATTERY})
        client = coordinator.config_entry.runtime_data.client
        client.set_recording = AsyncMock(side_effect=lambda _sn, *, enabled: enabled)
        switch = _switch(coordinator)

        await switch.async_turn_on()
        client.set_recording.assert_awaited_with("CAM1", enabled=True)
        self.assertTrue(switch.is_on)

        await switch.async_turn_off()
        client.set_recording.assert_awaited_with("CAM1", enabled=False)
        self.assertFalse(switch.is_on)
        self.assertEqual(switch.async_write_ha_state.call_count, 2)

    async def test_a_refused_write_leaves_the_state(self) -> None:
        coordinator = _coordinator({"CAM1": BATTERY})
        coordinator.config_entry.runtime_data.client.set_recording = AsyncMock(
            side_effect=EufySdkApiClientError("no")
        )
        switch = _switch(coordinator)
        with pytest.raises(EufySdkApiClientError):
            await switch.async_turn_on()
        self.assertFalse(switch.is_on)

    def test_recording_attribute_follows_this_cameras_events(self) -> None:
        switch = _switch(_coordinator({"CAM1": BATTERY}))
        switch._handle_event(_event("CAM2", active=True))
        switch._handle_event(_event("CAM1", active=True, event="streamState"))
        self.assertEqual(switch.extra_state_attributes, {"recording": False})

        switch._handle_event(_event("CAM1", active=True))
        self.assertEqual(switch.extra_state_attributes, {"recording": True})
        switch._handle_event(_event("CAM1", active=False))
        self.assertEqual(switch.extra_state_attributes, {"recording": False})

    def test_a_coordinator_update_supersedes_an_older_event(self) -> None:
        coordinator = _coordinator({"CAM1": BATTERY})
        switch = _switch(coordinator)
        switch._handle_event(_event("CAM1", active=True))
        coordinator.recording_active = frozenset()
        with patch(
            "custom_components.eufy_sdk.entity.CoordinatorEntity._handle_coordinator_update"
        ):
            switch._handle_coordinator_update()
        self.assertEqual(switch.extra_state_attributes, {"recording": False})


class RecordingPollTests(unittest.IsolatedAsyncioTestCase):
    """The coordinator reads `recording.list` only from a bridge that has it."""

    async def test_reads_the_recording_state_when_a_camera_can_record(self) -> None:
        client = _client([{"sn": "CAM1", **BATTERY}])
        client.list_recordings = AsyncMock(
            return_value={"enabled": ["CAM1"], "active": []}
        )
        c = _poll_coordinator(client)

        await c._async_update_data()

        self.assertEqual(c.recording_enabled, frozenset({"CAM1"}))
        self.assertEqual(c.recording_active, frozenset())

    async def test_an_older_bridge_is_not_asked(self) -> None:
        client = _client([{"sn": "CAM3", **OLD_BRIDGE}])
        client.list_recordings = AsyncMock()
        c = _poll_coordinator(client)

        await c._async_update_data()

        client.list_recordings.assert_not_awaited()
        self.assertEqual(c.recording_enabled, frozenset())

    async def test_a_failed_read_keeps_the_last_state_and_the_poll(self) -> None:
        client = _client([{"sn": "CAM1", **BATTERY}])
        client.list_recordings = AsyncMock(side_effect=EufySdkApiClientError("x"))
        c = _poll_coordinator(client)
        c.recording_enabled = frozenset({"CAM1"})

        devices = await c._async_update_data()

        self.assertIn("CAM1", devices)
        self.assertEqual(c.recording_enabled, frozenset({"CAM1"}))


class RecordingApiTests(unittest.IsolatedAsyncioTestCase):
    """The client speaks the bridge's recording.* commands."""

    async def test_commands_and_payloads(self) -> None:
        client = EufySdkApiClient("example.invalid", 3000, Mock())
        client.rpc = AsyncMock(
            side_effect=[
                {"ok": True, "enabled": ["CAM1"], "active": ["CAM1"]},
                {"ok": True, "deviceSn": "CAM1", "enabled": False},
            ]
        )

        self.assertEqual(
            await client.list_recordings(), {"enabled": ["CAM1"], "active": ["CAM1"]}
        )
        self.assertFalse(await client.set_recording("CAM1", enabled=False))
        self.assertEqual(client.rpc.await_args_list[0].args, ("recording.list",))
        self.assertEqual(
            client.rpc.await_args_list[1].kwargs, {"deviceSn": "CAM1", "enabled": False}
        )


class RecordLiveTranslationTests(unittest.TestCase):
    """The switch is named in English and Polish."""

    def test_names(self) -> None:
        for lang, name in (("en", "Record live view"), ("pl", "Nagrywaj podgląd")):
            strings = json.loads((TRANSLATIONS / f"{lang}.json").read_text("utf-8"))
            self.assertEqual(
                strings["entity"]["switch"]["record_live"]["name"], name, lang
            )


def _event(sn: str, *, active: bool, event: str = "recordingState") -> Any:
    return SimpleNamespace(
        event_type=EVENT_TYPE, data={"event": event, "deviceSn": sn, "active": active}
    )


def _switch(coordinator: SimpleNamespace) -> EufyRecordLiveSwitch:
    switch = EufyRecordLiveSwitch(coordinator, "CAM1")
    switch.async_write_ha_state = Mock()
    return switch


def _coordinator(devices: dict[str, dict[str, Any]]) -> SimpleNamespace:
    runtime_data = SimpleNamespace(
        properties={sn: [] for sn in devices},
        client=Mock(),
    )
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(runtime_data=runtime_data),
        data=devices,
        solix_devices={},
        recording_enabled=frozenset(),
        recording_active=frozenset(),
    )
    runtime_data.coordinator = coordinator
    return coordinator


def _client(devices: list[dict[str, Any]]) -> Mock:
    client = Mock()
    client.connected = True
    client.auth_status = AsyncMock(return_value={"state": "ok"})
    client.list_devices = AsyncMock(return_value=devices)
    client.list_solix_devices = AsyncMock(return_value=[])
    return client


def _poll_coordinator(client: Mock) -> coord_mod.EufySdkDataUpdateCoordinator:
    # Skip DataUpdateCoordinator.__init__: only the poll body is under test.
    c = object.__new__(coord_mod.EufySdkDataUpdateCoordinator)
    c.hass = Mock()
    c.config_entry = Mock()
    c.config_entry.runtime_data.client = client
    c._fast_retry_cancel = None
    return c
