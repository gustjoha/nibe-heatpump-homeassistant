"""The Nibe Smart Control Statistics integration.

Polls the nibe_smart_control addon's /api/hastats endpoint and exposes the
values as normal Home Assistant sensor entities with proper device_class /
state_class. This is the recommended, low-maintenance way to get long-term
statistics into HA's recorder database: any entity with state_class =
measurement / total / total_increasing gets short-term (5 min) and long-term
(hourly) statistics automatically, with no custom statistics-API code to
maintain. See sensor.py.

A one-shot `backfill_history` service is also provided for importing the
addon's already-accumulated dry-run/live history (from before this
integration was installed) using the recorder's external-statistics API —
see statistics.py. That path is used only for the one-time backfill; ongoing
collection always goes through the sensor entities above.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import API_PATH, DOMAIN, SERVICE_BACKFILL, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]

BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Optional("days", default=14): vol.All(int, vol.Range(min=1, max=90)),
    }
)


class NibeStatsCoordinator(DataUpdateCoordinator):
    """Polls /api/hastats on the addon."""

    def __init__(self, hass: HomeAssistant, host: str, port: int) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.host = host
        self.port = port
        self._session = async_get_clientsession(hass)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def _async_update_data(self) -> dict:
        url = f"{self.base_url}{API_PATH}"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"HTTP {resp.status} from addon")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Cannot reach addon at {url}: {err}") from err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    coordinator = NibeStatsCoordinator(hass, host, port)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _handle_backfill(call: ServiceCall) -> None:
        from .statistics import async_backfill_history  # local import: only needed for this rare call

        target_id = call.data["entry_id"]
        days = call.data["days"]
        target_coord = hass.data[DOMAIN].get(target_id)
        if target_coord is None:
            _LOGGER.error("No Nibe Smart Control Statistics entry with id %s", target_id)
            return
        await async_backfill_history(hass, target_coord, days)

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        hass.services.async_register(DOMAIN, SERVICE_BACKFILL, _handle_backfill, schema=BACKFILL_SCHEMA)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL)
    return unloaded
