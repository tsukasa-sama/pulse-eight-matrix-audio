"""Media player platform: one media_player per output zone."""

from __future__ import annotations

import logging

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PulseEightConfigEntry
from .const import VOLUME_MAX
from .coordinator import PulseEightCoordinator
from .entity import PulseEightEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PulseEightConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one media_player per output zone."""
    coordinator = entry.runtime_data
    async_add_entities(
        PulseEightZone(coordinator, output)
        for output in range(1, coordinator.outputs + 1)
    )


class PulseEightZone(PulseEightEntity, MediaPlayerEntity):
    """A single output zone as a media_player: source select, volume, mute."""

    _attr_translation_key = "zone"
    _attr_supported_features = (
        MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    def __init__(self, coordinator: PulseEightCoordinator, output: int) -> None:
        super().__init__(coordinator)
        self._output = output
        self._attr_unique_id = f"{coordinator.entry.entry_id}_zone_{output}"
        self._attr_translation_placeholders = {"output": str(output)}
        self._attr_source_list = coordinator.source_names()
        # Optimistic fallbacks: reflect a just-issued command immediately, and
        # keep showing it if the switch's read-back is unavailable. Poll data,
        # when present, always takes precedence.
        self._optimistic_route: int | None = None
        self._optimistic_mute: bool | None = None
        self._optimistic_volume: int | None = None

    # --- current values (poll data first, then optimistic) -----------------

    def _route(self) -> int | None:
        number = self.coordinator.data.routes.get(self._output)
        return number if number is not None else self._optimistic_route

    def _mute(self) -> bool | None:
        muted = self.coordinator.data.mutes.get(self._output)
        return muted if muted is not None else self._optimistic_mute

    def _volume(self) -> int | None:
        vol = self.coordinator.data.volumes.get(self._output)
        return vol if vol is not None else self._optimistic_volume

    @property
    def state(self) -> MediaPlayerState:
        """PLAYING once a real source is routed, otherwise IDLE."""
        return MediaPlayerState.PLAYING if self.source else MediaPlayerState.IDLE

    @property
    def source(self) -> str | None:
        """Currently routed input name (None when disconnected)."""
        number = self._route()
        return self.coordinator.name_for_number(number) if number else None

    @property
    def media_title(self) -> str | None:
        """Show the routed input as the card's title text."""
        return self.source

    @property
    def is_volume_muted(self) -> bool | None:
        """Whether this zone is muted."""
        return self._mute()

    @property
    def volume_level(self) -> float | None:
        """Volume as a 0..1 float."""
        vol = self._volume()
        return vol / VOLUME_MAX if vol is not None else None

    async def async_select_source(self, source: str) -> None:
        """Route an input to this zone."""
        number = self.coordinator.number_for_name(source)
        _LOGGER.debug(
            "zone %d select source %r (source number %s)",
            self._output, source, number,
        )
        if number is None:
            _LOGGER.warning(
                "Zone %d: no source matches %r; options are %s",
                self._output, source, self._attr_source_list,
            )
            return
        self._optimistic_route = number
        self.async_write_ha_state()
        await self.coordinator.client.async_set_route(self._output, number)
        await self.coordinator.async_request_refresh()

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute this zone."""
        _LOGGER.debug("zone %d mute %s", self._output, mute)
        self._optimistic_mute = mute
        self.async_write_ha_state()
        await self.coordinator.client.async_set_mute(self._output, mute)
        await self.coordinator.async_request_refresh()

    async def async_set_volume_level(self, volume: float) -> None:
        """Set zone volume from a 0..1 float."""
        level = round(volume * VOLUME_MAX)
        _LOGGER.debug("zone %d volume %.2f -> %d%%", self._output, volume, level)
        self._optimistic_volume = level
        self.async_write_ha_state()
        await self.coordinator.client.async_set_volume(self._output, level)
        await self.coordinator.async_request_refresh()
