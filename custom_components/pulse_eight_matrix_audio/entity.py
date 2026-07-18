"""Base entity for the Pulse-Eight Matrix Audio integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import PulseEightCoordinator


class PulseEightEntity(CoordinatorEntity[PulseEightCoordinator]):
    """Common base wiring device info + coordinator for all entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PulseEightCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        info = coordinator.device_info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=info.model if info else MODEL,
            sw_version=info.firmware if info else None,
            serial_number=info.serial if info else None,
        )
