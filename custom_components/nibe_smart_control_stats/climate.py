"""Climate entity for Nibe Smart Control.

Shows indoor temperature and the controller's target setpoint, and lets you
change that setpoint from a thermostat card. It does NOT write to the heat
pump directly: setting a temperature calls the addon's /api/setpoint, and the
addon's normal control loop (indoor P-term, gate, slew limit, rate limit,
whole-step quantisation, dry run) decides what — if anything — reaches the
curve offset register. The card is a front-end to the addon, not a bypass.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SETPOINT_PATH

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NibeSmartClimate(coordinator, entry)])


class NibeSmartClimate(CoordinatorEntity, ClimateEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "indoor_climate"
    _attr_name = "Indoor climate"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_precision = 0.1
    # Only HEAT: there is deliberately no OFF mode. Turning the controller
    # off from a dashboard card is too easy to do by accident; dry run and
    # the indoor-control toggle stay in the addon's own Settings.
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nibe Smart Control",
            manufacturer="gustjoha/nibe-heatpump-homeassistant",
            model="F1245 curve controller (addon)",
            configuration_url=coordinator.base_url,
        )

    @property
    def _data(self) -> dict:
        return self.coordinator.data or {}

    @property
    def current_temperature(self) -> float | None:
        return self._data.get("indoor_temp_c")

    @property
    def target_temperature(self) -> float | None:
        return self._data.get("indoor_setpoint_c")

    @property
    def min_temp(self) -> float:
        return float(self._data.get("setpoint_min_c") or 15.0)

    @property
    def max_temp(self) -> float:
        return float(self._data.get("setpoint_max_c") or 25.0)

    @property
    def hvac_action(self) -> HVACAction | None:
        comp = self._data.get("compressor_on")
        if comp is None:
            return None
        if not comp:
            return HVACAction.IDLE
        prio = (self._data.get("prio") or "").lower()
        # Compressor running for hot water is not space heating.
        if "hot water" in prio:
            return HVACAction.IDLE
        return HVACAction.HEATING

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._data
        return {
            "curve_offset_steps": d.get("combined_offset_c"),
            "outdoor_temperature": d.get("outdoor_temp_c"),
            "price_level": d.get("price_level"),
            "priority": d.get("prio"),
            "dry_run": d.get("dry_run"),
            "indoor_control_enabled": d.get("indoor_control_enabled"),
            "setpoint_source": d.get("setpoint_source"),
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode != HVACMode.HEAT:
            raise HomeAssistantError("Only HEAT mode is supported; use the addon Settings to disable control")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        session = async_get_clientsession(self.hass)
        url = f"{self.coordinator.base_url}{SETPOINT_PATH}"
        try:
            async with session.post(url, json={"temperature": float(temp)},
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200 or not body.get("ok"):
                    raise HomeAssistantError(
                        f"Nibe Smart Control rejected setpoint {temp}: {body.get('error', resp.status)}")
        except aiohttp.ClientError as err:
            raise HomeAssistantError(f"Cannot reach Nibe Smart Control addon: {err}") from err
        await self.coordinator.async_request_refresh()
