"""DataUpdateCoordinator for the Pulse-Eight matrix."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import DeviceInfo, MatrixState, PulseEightClient, PulseEightError
from .const import CONF_SOURCE_NAMES, DOMAIN, SCAN_INTERVAL_SECONDS
from .sources import Source

_LOGGER = logging.getLogger(__name__)


class PulseEightCoordinator(DataUpdateCoordinator[MatrixState]):
    """Polls the matrix and shares state with all entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PulseEightClient,
        outputs: int,
        sources: list[Source],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.client = client
        self.outputs = outputs
        self.sources = sources
        self._by_number = {s.number: s for s in sources}
        # Populated from '^V ?$' during first refresh for device_info.
        self.device_info: DeviceInfo | None = None

    # --- Source name resolution -------------------------------------------

    def source_name(self, source: Source) -> str:
        """User-assigned name for a source, or its default label."""
        names: dict[str, str] = self.entry.options.get(CONF_SOURCE_NAMES, {})
        return names.get(source.key) or source.default_name

    def source_names(self) -> list[str]:
        """Ordered list of source labels for a select/source_list."""
        return [self.source_name(s) for s in self.sources]

    def name_for_number(self, number: int) -> str | None:
        """Label for a routed source number, or None if unknown/disconnected."""
        source = self._by_number.get(number)
        return self.source_name(source) if source else None

    def number_for_name(self, name: str) -> int | None:
        """Extended I/O source number for a chosen label, or None."""
        for source in self.sources:
            if self.source_name(source) == name:
                return source.number
        return None

    async def _async_update_data(self) -> MatrixState:
        try:
            if self.device_info is None:
                self.device_info = await self.client.async_get_version()
            return await self.client.async_get_state(self.outputs)
        except PulseEightError as err:
            raise UpdateFailed(str(err)) from err
