"""Switch platform — writable booleans, plus per-bit switches for known bitfields."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .bespoke import BITFIELD_SWITCHES
from .const import EVENT_TYPE
from .entity import (
    EufySdkDeviceEntity,
    EufySdkPropertyEntity,
    EufySolixEntity,
    classify,
    has_capability,
    is_setting,
    solix_devices_with,
)
from .light import LIGHT_OWNED_PROPS
from .lock import LOCK_OWNED_PROPS
from .preload import on_battery_power

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a switch per writable bool, and per-bit switches for known bitfields."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SwitchEntity] = []
    for sn in coordinator.data:
        is_smart_light = has_capability(coordinator.data[sn], "smart_light")
        is_lock = has_capability(coordinator.data[sn], "lock")
        for spec in entry.runtime_data.properties.get(sn, []):
            # The light platform owns lightPower/lightBrightness for smart_light — don't
            # also surface them as a bare switch/number (would double the control).
            if is_smart_light and spec["name"] in LIGHT_OWNED_PROPS:
                continue
            # The lock platform owns the 'locked' — don't also surface it as a switch.
            if is_lock and spec["name"] in LOCK_OWNED_PROPS:
                continue
            kind = classify(spec)
            if kind == "switch":
                entities.append(EufySdkSwitch(coordinator, sn, spec))
            elif kind == "bitfield" and spec["name"] in BITFIELD_SWITCHES:
                bf = BITFIELD_SWITCHES[spec["name"]]
                entities.extend(
                    EufyBitmaskSwitch(
                        coordinator,
                        sn,
                        spec,
                        {"key": key, "bit": bit, "base": bf["base"]},
                    )
                    for key, bit in bf["bits"].items()
                )

    # "Record live view" per battery camera, gated on the bridge's `canRecordLive`: an
    # older bridge has no `recording.set`, so the switch could only fail. A mains camera
    # is left out: it can stream around the clock (an NVR), which is not a viewing.
    entities.extend(
        EufyRecordLiveSwitch(coordinator, sn)
        for sn, dev in coordinator.data.items()
        if dev.get("canRecordLive") and on_battery_power(dev)
    )

    # Anker Solix (separate account): a Solarbank's ambient light — a standalone switch
    # that reflects the device state (ambientLightOn, decoded from the ff09 telemetry).
    entities.extend(
        EufySolixLightSwitch(coordinator, sn)
        for sn, _ in solix_devices_with(coordinator, "battery")
    )

    async_add_entities(entities)


class EufySolixLightSwitch(EufySolixEntity, SwitchEntity):
    """
    A Solarbank's ambient light as a switch — reflects the DEVICE state.

    Toggling issues an encrypted `set_device_attrs` write through the bridge. The state
    is read back from the `ff09` telemetry: the SDK decodes `ambientLightOn` (1/0) from
    tag `0xba` bit 0x20 (inverted), live-confirmed on an AE103 across app + HA toggles.
    So a change made outside HA (the app / physical button) is reflected within a few
    seconds via `solixReading` events, and this is NOT assumed-state. The value seeds
    from the bridge's `solix.devices` snapshot and updates on live events. To avoid a UI
    flicker while the ~seconds-late telemetry catches up, a toggle we issue sets the
    shown state optimistically; the next reading confirms it. Device is `solix:<sn>`.
    """

    _attr_name = "Ambient Light"
    _attr_icon = "mdi:led-strip-variant"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a Solix Solarbank's ambient light."""
        super().__init__(coordinator, sn, "ambient_light", watch=("ambientLightOn",))

    @property
    def is_on(self) -> bool | None:
        """The light's state from telemetry (None until a reading arrives)."""
        v = self.solix_value("ambientLightOn")
        return None if v is None else bool(v)

    async def async_turn_on(self, **_: Any) -> None:
        """Turn the ambient light on (optimistic; telemetry confirms shortly)."""
        await self.client.set_solix_light(self._sn, on=True)
        self._solix_values["ambientLightOn"] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **_: Any) -> None:
        """Turn the ambient light off (optimistic; telemetry confirms shortly)."""
        await self.client.set_solix_light(self._sn, on=False)
        self._solix_values["ambientLightOn"] = False
        self.async_write_ha_state()


class EufyRecordLiveSwitch(EufySdkDeviceEntity, SwitchEntity):
    """
    Record this camera's live view, kept by the bridge as mp4 files.

    A bridge setting (`recording.set`), not a camera property: the bridge copies the
    frames of every live view someone opens anyway (a dashboard, the more-info dialog,
    HomeKit) and never opens a stream for it, so it costs the battery nothing. Off
    until switched on. The state comes from the bridge's `recording.list`, read with
    each coordinator update; the `recording` attribute follows the bridge's
    `recordingState` event, so it shows when a viewing is being recorded.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:record-rec"
    _attr_translation_key = "record_live"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a camera serial."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_record_live"
        self._recording: bool | None = None  # last recordingState event; None → poll

    @property
    def suggested_object_id(self) -> str:
        """`switch.<camera>_record_live`, whatever language Home Assistant runs in."""
        return "record_live"

    @property
    def is_on(self) -> bool:
        """On when the bridge records this camera's live view."""
        return self._sn in self.coordinator.recording_enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Whether a viewing is being recorded right now."""
        recording = self._recording
        if recording is None:
            recording = self._sn in self.coordinator.recording_active
        return {"recording": recording}

    async def async_added_to_hass(self) -> None:
        """Subscribe to the bridge's recordingState events for this camera."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_TYPE, self._handle_event))

    @callback
    def _handle_event(self, event: Event) -> None:
        """Track this camera's recordingState (on/off)."""
        data = event.data
        if data.get("event") != "recordingState" or data.get("deviceSn") != self._sn:
            return
        self._recording = bool(data.get("active"))
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Take a fresh `recording.list` over an older event."""
        self._recording = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **_: Any) -> None:
        """Record this camera's live view from now on."""
        await self._set(enabled=True)

    async def async_turn_off(self, **_: Any) -> None:
        """Stop recording (a recording in progress is kept)."""
        await self._set(enabled=False)

    async def _set(self, *, enabled: bool) -> None:
        """Ask the bridge and show what it answered."""
        enabled = await self.client.set_recording(self._sn, enabled=enabled)
        current = set(self.coordinator.recording_enabled)
        if enabled:
            current.add(self._sn)
        else:
            current.discard(self._sn)
        self.coordinator.recording_enabled = frozenset(current)
        self.async_write_ha_state()


class EufySdkSwitch(EufySdkPropertyEntity, SwitchEntity):
    """A writable boolean property as a switch."""

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        spec: dict[str, Any],
    ) -> None:
        """Put a setting toggle under Configuration; leave a primary control up top."""
        super().__init__(coordinator, sn, spec)
        if is_setting(self._prop):
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool | None:
        """On when the property's live value is truthy."""
        v = self.prop_value
        return None if v is None else bool(v)

    async def async_turn_on(self, **_: Any) -> None:
        """Set the property true."""
        await self.write(value=True)

    async def async_turn_off(self, **_: Any) -> None:
        """Set the property false."""
        await self.write(value=False)


class EufyBitmaskSwitch(EufySdkPropertyEntity, SwitchEntity):
    """One bit of a bitfield property as a switch (writes back the whole mask)."""

    # Each bit is its own control ("Detect human"), not the parent property, so the
    # generic property label must not be applied over the per-bit name.
    _named_by_translation = True

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        spec: dict[str, Any],
        bitdef: dict[str, Any],
    ) -> None:
        """Bind to a single bit of the parent bitfield property ({key, bit, base})."""
        super().__init__(coordinator, sn, spec)
        self._bit: int = bitdef["bit"]
        self._base: int = bitdef["base"]
        self._attr_unique_id = f"{sn}_{self._prop}_{self._bit}"
        self._attr_translation_key = bitdef["key"]
        self._attr_entity_category = EntityCategory.CONFIG

    def _mask(self) -> int:
        """
        Return the current full bitmask value (falls back to the enable base).

        Refuses when the device reports the value in a shape that isn't a bitmask
        (a JSON object on some families): writing a bare number back would replace
        the fields that object carries.
        """
        v = self.prop_value
        if v is None:
            return self._base
        mask = _as_mask(v)
        if mask is None:
            msg = (
                f"{self._prop} is reported as {v!r}, not a bitmask; not overwriting it"
            )
            raise HomeAssistantError(msg)
        return mask

    @property
    def is_on(self) -> bool | None:
        """On when this bit is set in the current mask; unknown if it isn't one."""
        mask = _as_mask(self.prop_value)
        return None if mask is None else bool(mask & self._bit)

    async def async_turn_on(self, **_: Any) -> None:
        """Set this bit while preserving every other reported bit."""
        await self.write(self._mask() | self._bit)

    async def async_turn_off(self, **_: Any) -> None:
        """Clear this bit while preserving every other reported bit."""
        await self.write(self._mask() & ~self._bit)


def _as_mask(value: Any) -> int | None:
    """Return a reported bitmask as an int, or None for a value that isn't one."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None
