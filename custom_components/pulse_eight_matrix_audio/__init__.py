"""The Pulse-Eight Matrix Audio integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant

from .client import PulseEightClient, PulseEightError
from .const import (
    CONF_EXTENDED_IO,
    CONF_MODEL,
    DEFAULT_EXTENDED_IO,
    DEFAULT_MODEL,
    MODELS,
)
from .coordinator import PulseEightCoordinator
from .sources import build_sources

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
    Platform.SWITCH,
]

type PulseEightConfigEntry = ConfigEntry[PulseEightCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: PulseEightConfigEntry
) -> bool:
    """Set up Pulse-Eight Matrix Audio from a config entry."""
    client = PulseEightClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
    )

    # Normalise control flags (ACK/ECO on, ASY off) and, by default, Extended
    # I/O so source numbering is consistent across models. Fire-and-forget, so
    # this won't fail setup on its own.
    try:
        await client.async_configure(
            extended_io=entry.data.get(CONF_EXTENDED_IO, DEFAULT_EXTENDED_IO)
        )
    except PulseEightError as err:
        _LOGGER.warning("Could not apply Pulse-Eight control settings: %s", err)

    counts = MODELS.get(entry.data.get(CONF_MODEL, DEFAULT_MODEL), MODELS[DEFAULT_MODEL])
    coordinator = PulseEightCoordinator(
        hass,
        entry,
        client,
        outputs=counts["zones"],
        sources=build_sources(counts),
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Don't leak the socket on a failed/ retried setup: this switch keeps a
        # TCP connection open for up to 10 minutes, and a leaked one can starve
        # subsequent connection attempts.
        await client.async_close()
        raise

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(
    hass: HomeAssistant, entry: PulseEightConfigEntry
) -> None:
    """Reload when options (e.g. custom source names) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: PulseEightConfigEntry
) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.async_close()
    return unloaded
