"""Switch platform: per-output mute toggles."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PulseEightConfigEntry
from .client import PulseEightError
from .entity import PulseEightEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PulseEightConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one mute switch per output."""
    coordinator = entry.runtime_data
    async_add_entities(
        PulseEightMuteSwitch(coordinator, output)
        for output in range(1, coordinator.outputs + 1)
    )


class PulseEightMuteSwitch(PulseEightEntity, SwitchEntity):
    """Mute toggle for a single output zone."""

    _attr_icon = "mdi:volume-mute"

    def __init__(self, coordinator, output: int) -> None:
        super().__init__(coordinator)
        self._output = output
        self._attr_unique_id = f"{coordinator.entry.entry_id}_mute_{output}"
        self._attr_translation_key = "output_mute"
        self._attr_translation_placeholders = {"output": str(output)}

    @property
    def is_on(self) -> bool | None:
        """Return True when the output is muted."""
        return self.coordinator.data.mutes.get(self._output)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Mute the output."""
        await self._async_set_mute(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unmute the output."""
        await self._async_set_mute(False)

    async def _async_set_mute(self, muted: bool) -> None:
        try:
            await self.coordinator.client.async_set_mute(self._output, muted)
        except PulseEightError:
            # Let the next poll reconcile; re-raise for HA to surface.
            raise
        await self.coordinator.async_request_refresh()
