"""Select platform: per-output input routing."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PulseEightConfigEntry
from .coordinator import PulseEightCoordinator
from .entity import PulseEightEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PulseEightConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one input-selector per output."""
    coordinator = entry.runtime_data
    async_add_entities(
        PulseEightRouteSelect(coordinator, output)
        for output in range(1, coordinator.outputs + 1)
    )


class PulseEightRouteSelect(PulseEightEntity, SelectEntity):
    """Selects which input is routed to a single output."""

    _attr_icon = "mdi:import"

    def __init__(self, coordinator: PulseEightCoordinator, output: int) -> None:
        super().__init__(coordinator)
        self._output = output
        self._attr_unique_id = f"{coordinator.entry.entry_id}_route_{output}"
        self._attr_translation_key = "output_route"
        self._attr_translation_placeholders = {"output": str(output)}
        self._attr_options = coordinator.source_names()

    @property
    def current_option(self) -> str | None:
        """Return the currently routed input label."""
        number = self.coordinator.data.routes.get(self._output)
        if number is None:
            return None
        return self.coordinator.name_for_number(number)

    async def async_select_option(self, option: str) -> None:
        """Route the chosen input to this output."""
        number = self.coordinator.number_for_name(option)
        _LOGGER.debug(
            "select output %d -> %r (source number %s)",
            self._output, option, number,
        )
        if number is None:
            _LOGGER.warning(
                "Output %d: no source matches %r; options are %s",
                self._output, option, self._attr_options,
            )
            return
        await self.coordinator.client.async_set_route(self._output, number)
        await self.coordinator.async_request_refresh()
