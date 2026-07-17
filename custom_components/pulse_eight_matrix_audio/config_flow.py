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
            except PulseEightError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating connection")
                errors["base"] = "unknown"
            else:
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

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show one text field per source, defaulting to its current name."""
        counts = MODELS.get(
            self.config_entry.data.get(CONF_MODEL, DEFAULT_MODEL),
            MODELS[DEFAULT_MODEL],
        )
        sources = build_sources(counts)
        current: dict[str, str] = self.config_entry.options.get(
            CONF_SOURCE_NAMES, {}
        )
        # Field labels come from the schema key, so key by the physical input
        # name ("RCA 1") and map back to the storage key ("analog_1").
        by_label = {src.default_name: src for src in sources}

        if user_input is not None:
            names: dict[str, str] = {}
            for label, value in user_input.items():
                src = by_label.get(label)
                value = value.strip()
                # Store only real overrides so default changes still propagate.
                if src and value and value != src.default_name:
                    names[src.key] = value
            return self.async_create_entry(
                title="", data={CONF_SOURCE_NAMES: names}
            )

        schema = vol.Schema(
            {
                vol.Optional(
                    src.default_name,
                    description={
                        "suggested_value": current.get(src.key, src.default_name)
                    },
                ): str
                for src in sources
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
