"""Config flow for Nibe Smart Control Statistics."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_PATH, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


async def _test_connection(hass, host: str, port: int) -> str | None:
    """Try /api/hastats. Returns an error code, or None on success."""
    session = async_get_clientsession(hass)
    url = f"http://{host}:{port}{API_PATH}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return "cannot_connect"
            data = await resp.json()
            if "schema" not in data:
                return "invalid_response"
    except aiohttp.ClientError:
        return "cannot_connect"
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Unexpected error testing connection")
        return "unknown"
    return None


class NibeStatsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup: host + port of the nibe_smart_control addon."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            error = await _test_connection(self.hass, host, port)
            if error is None:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Nibe Smart Control ({host})",
                    data={CONF_HOST: host, CONF_PORT: port},
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={
                "hint": (
                    "Find the addon hostname under Settings → Add-ons → "
                    "Nibe Smart Control → Info (usually the slug with "
                    "hyphens, e.g. 32c51978-nibe-smart-control), or use the "
                    "host's LAN IP with port 8099."
                )
            },
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_user(user_input)
