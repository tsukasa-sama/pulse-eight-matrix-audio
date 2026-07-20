"""Media player platform: one media_player per output zone."""

from __future__ import annotations

import logging

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PulseEightConfigEntry
from .const import SOURCE_OFF_LABEL, VOLUME_MAX
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
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: PulseEightCoordinator, output: int) -> None:
        super().__init__(coordinator)
        self._output = output
        self._attr_unique_id = f"{coordinator.entry.entry_id}_zone_{output}"
        self._attr_translation_placeholders = {"output": str(output)}
        # "Off" (disconnect) leads the list, then the enabled inputs.
        self._attr_source_list = [SOURCE_OFF_LABEL, *coordinator.source_names()]
        # Last real (non-disconnected) source, so turn-on can restore it.
        self._last_source: int | None = None
        # Optimistic fallbacks: reflect a just-issued command immediately, and
        # keep showing it if the switch's read-back is unavailable. Poll data,
        # when present, always takes precedence.
        self._optimistic_route: int | None = None
        self._optimistic_mute: bool | None = None
        self._optimistic_volume: int | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Track the last real source seen, so turn-on can restore it."""
        route = self.coordinator.data.routes.get(self._output)
        if route:  # >0: a real source (0 = disconnected)
            self._last_source = route
        super()._handle_coordinator_update()

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
        """PLAYING once a real source is routed, otherwise OFF (disconnected)."""
        return MediaPlayerState.PLAYING if self._route() else MediaPlayerState.OFF

    @property
    def source(self) -> str | None:
        """Routed input name; the Off label when disconnected; None if unknown."""
        number = self._route()
        if number:
            return self.coordinator.name_for_number(number)
        if number == 0:
            return SOURCE_OFF_LABEL
        return None

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
        """Route an input to this zone (fading in), or disconnect if 'Off'."""
        if source == SOURCE_OFF_LABEL:
            await self.async_turn_off()
            return
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
        await self._route_and_fade_in(number)

    async def async_turn_on(self) -> None:
        """Reconnect the last-used source and fade it in."""
        if self._last_source is None:
            _LOGGER.debug(
                "zone %d turn_on: no previous source to restore", self._output
            )
            return
        _LOGGER.debug("zone %d turn_on -> source %d", self._output, self._last_source)
        await self._route_and_fade_in(self._last_source)

    async def async_turn_off(self) -> None:
        """Disconnect this zone: hard-cut the source and mute it."""
        _LOGGER.debug("zone %d turn_off (disconnect)", self._output)
        self._optimistic_route = 0
        self._optimistic_mute = True
        self.async_write_ha_state()
        await self.coordinator.client.async_disconnect(self._output)
        await self.coordinator.async_request_refresh()

    async def _route_and_fade_in(self, number: int) -> None:
        """Route ``number`` to this zone, unmute, and fade in over 3 s."""
        self._last_source = number
        self._optimistic_route = number
        self._optimistic_mute = False
        self.async_write_ha_state()
        await self.coordinator.client.async_route_and_fade_in(self._output, number)
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
