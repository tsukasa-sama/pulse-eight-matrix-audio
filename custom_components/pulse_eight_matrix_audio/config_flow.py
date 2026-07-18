"""Config flow for the Pulse-Eight Matrix Audio integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .client import PulseEightClient, PulseEightError
from .const import (
    CONF_DISABLED_SOURCES,
    CONF_EXTENDED_IO,
    CONF_MODEL,
    CONF_SOURCE_NAMES,
    DEFAULT_EXTENDED_IO,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    DOMAIN,
    MODELS,
)
from .sources import build_sources

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_MODEL, default=DEFAULT_MODEL): vol.In(list(MODELS)),
        vol.Required(CONF_EXTENDED_IO, default=DEFAULT_EXTENDED_IO): bool,
    }
)


class PulseEightConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pulse-Eight Matrix Audio."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = PulseEightClient(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
            )
            try:
                info = await client.async_test_connection()
            except PulseEightError as err:
                _LOGGER.warning(
                    "Connection to Pulse-Eight matrix at %s:%s failed: %s",
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    err,
                )
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error validating Pulse-Eight matrix at %s:%s",
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                )
                errors["base"] = "unknown"
            else:
                _LOGGER.debug(
                    "Probe of %s:%s ok: %s",
                    user_input[CONF_HOST], user_input[CONF_PORT], info,
                )
                # Prefer the serial number for a stable unique id; fall back to
                # host:port if the switch didn't report one.
                unique_id = info.serial or (
                    f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{user_input[CONF_MODEL]} ({user_input[CONF_HOST]})",
                    data=user_input,
                )
            finally:
                await client.async_close()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> PulseEightOptionsFlow:
        """Return the options flow for renaming inputs."""
        return PulseEightOptionsFlow()


class PulseEightOptionsFlow(OptionsFlow):
    """Let the user assign friendly names to each input."""

    # Prefix for each input's "show" toggle field, so it's distinct from that
    # input's name field (keyed by the plain label, e.g. "RCA 1").
    _SHOW_PREFIX = "Show "

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name each input and toggle whether it appears in the zone dropdown.

        Each input gets two adjacent fields: a name text box, then a "Show …"
        toggle directly beneath it. Unchecking the toggle hides that input from
        every zone's source list.
        """
        counts = MODELS.get(
            self.config_entry.data.get(CONF_MODEL, DEFAULT_MODEL),
            MODELS[DEFAULT_MODEL],
        )
        sources = build_sources(counts)
        current: dict[str, str] = self.config_entry.options.get(
            CONF_SOURCE_NAMES, {}
        )
        disabled: set[str] = set(
            self.config_entry.options.get(CONF_DISABLED_SOURCES, [])
        )
        # Fields are keyed by the physical input label ("RCA 1", or "Show RCA 1"
        # for the toggle); map back to the storage key ("analog_1").
        by_label = {src.default_name: src for src in sources}

        if user_input is not None:
            names, hidden = self._parse_submission(user_input, by_label)
            return self.async_create_entry(
                title="",
                data={CONF_SOURCE_NAMES: names, CONF_DISABLED_SOURCES: hidden},
            )

        # Build name box + show toggle per input, in order, so each toggle sits
        # right under its name field. Toggle defaults on unless already hidden.
        fields: dict[Any, Any] = {}
        for src in sources:
            fields[
                vol.Optional(
                    src.default_name,
                    description={
                        "suggested_value": current.get(src.key, src.default_name)
                    },
                )
            ] = str
            fields[
                vol.Optional(
                    f"{self._SHOW_PREFIX}{src.default_name}",
                    default=src.key not in disabled,
                )
            ] = bool
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))

    def _parse_submission(
        self, user_input: dict[str, Any], by_label: dict[str, Any]
    ) -> tuple[dict[str, str], list[str]]:
        """Split submitted fields into name overrides and hidden-input keys."""
        names: dict[str, str] = {}
        hidden: list[str] = []
        for key, value in user_input.items():
            if key.startswith(self._SHOW_PREFIX):
                src = by_label.get(key[len(self._SHOW_PREFIX):])
                if src and not value:
                    hidden.append(src.key)
                continue
            src = by_label.get(key)
            value = value.strip()
            # Store only real overrides so default changes still propagate.
            if src and value and value != src.default_name:
                names[src.key] = value
        return names, hidden
