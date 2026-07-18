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
]

type PulseEightConfigEntry = ConfigEntry[PulseEightCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: PulseEightConfigEntry
) -> bool:
    """Set up Pulse-Eight Matrix Audio from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    model = entry.data.get(CONF_MODEL, DEFAULT_MODEL)
    extended_io = entry.data.get(CONF_EXTENDED_IO, DEFAULT_EXTENDED_IO)
    counts = MODELS.get(model, MODELS[DEFAULT_MODEL])
    sources = build_sources(counts)

    _LOGGER.info(
        "Setting up %s at %s:%s (%d zones, %d sources, extended_io=%s)",
        model, host, port, counts["zones"], len(sources), extended_io,
    )

    client = PulseEightClient(host=host, port=port)

    # Normalise control flags (ACK/ECO on, ASY off) and, by default, Extended
    # I/O so source numbering is consistent across models. Fire-and-forget, so
    # this won't fail setup on its own.
    try:
        await client.async_configure(extended_io=extended_io)
    except PulseEightError as err:
        _LOGGER.warning("Could not apply Pulse-Eight control settings: %s", err)

    coordinator = PulseEightCoordinator(
        hass,
        entry,
        client,
        outputs=counts["zones"],
        sources=sources,
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Don't leak the socket on a failed/ retried setup: this switch keeps a
        # TCP connection open for up to 10 minutes, and a leaked one can starve
        # subsequent connection attempts.
        _LOGGER.debug("First refresh failed for %s; closing client", host)
        await client.async_close()
        raise

    info = coordinator.device_info
    if info:
        _LOGGER.info(
            "Connected to %s (reported model %r, firmware %s, serial %s)",
            host, info.model, info.firmware, info.serial,
        )

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _LOGGER.debug("Setup complete for %s", host)
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
    _LOGGER.debug("Unloading %s", entry.data.get(CONF_HOST))
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        # Stop and await any in-flight poll BEFORE closing the client, so a
        # reload (e.g. HACS update) can't leave a poll's socket open while the
        # new setup starts connecting. The switch services only a few sockets and
        # holds them ~10 minutes, so an overlapping stale socket starves the
        # reconnect until the device is power-cycled.
        await coordinator.async_shutdown()
        await coordinator.client.async_close()
    return unloaded
