"""Sensor entities for Nibe Smart Control Statistics.

Every sensor here declares a state_class (measurement / total_increasing).
That single attribute is what tells HA's recorder to start keeping 5-minute
short-term statistics and hourly long-term statistics for the entity — the
same mechanism used by every other integration's power/energy/temperature
sensors. No custom statistics-writing code is needed for ongoing collection;
this is deliberately the "boring", low-maintenance path so it keeps working
across HA core upgrades. (The one-time historical backfill in statistics.py
is the only place that touches the lower-level external-statistics API.)
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


@dataclass(frozen=True, kw_only=True)
class NibeStatSensorDescription(SensorEntityDescription):
    value_fn_key: str = ""


SENSORS: tuple[NibeStatSensorDescription, ...] = (
    NibeStatSensorDescription(
        key="combined_offset",
        translation_key="combined_offset",
        name="Curve offset (combined)",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn_key="combined_offset_c",
    ),
    NibeStatSensorDescription(
        key="weather_offset",
        translation_key="weather_offset",
        name="Weather offset",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn_key="weather_offset_c",
    ),
    NibeStatSensorDescription(
        key="indoor_offset",
        translation_key="indoor_offset",
        name="Indoor offset",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn_key="indoor_offset_c",
    ),
    NibeStatSensorDescription(
        key="price_offset",
        translation_key="price_offset",
        name="Price offset",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn_key="price_offset_c",
    ),
    NibeStatSensorDescription(
        key="indoor_temp",
        translation_key="indoor_temp",
        name="Indoor temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn_key="indoor_temp_c",
    ),
    NibeStatSensorDescription(
        key="outdoor_temp",
        translation_key="outdoor_temp",
        name="Outdoor temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn_key="outdoor_temp_c",
    ),
    NibeStatSensorDescription(
        key="electricity_price",
        translation_key="electricity_price",
        name="Electricity price",
        native_unit_of_measurement="EUR/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn_key="price_eur_kwh",
    ),
    NibeStatSensorDescription(
        key="price_level",
        translation_key="price_level",
        name="Price level",
        device_class=SensorDeviceClass.ENUM,
        options=["VERY_CHEAP", "CHEAP", "NORMAL", "EXPENSIVE", "VERY_EXPENSIVE"],
        value_fn_key="price_level",
    ),
    NibeStatSensorDescription(
        key="estimated_power",
        translation_key="estimated_power",
        name="Estimated power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn_key="est_power_kw",
    ),
    NibeStatSensorDescription(
        key="lifetime_energy",
        translation_key="lifetime_energy",
        name="Estimated lifetime energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # total_increasing: monotonic counter, never decreases except on a
        # genuine reset (addon restart with fresh state). This is the class
        # HA's Energy Dashboard expects for a device's consumption.
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn_key="lifetime_energy_kwh",
    ),
    NibeStatSensorDescription(
        key="compressor_runtime_24h",
        translation_key="compressor_runtime_24h",
        name="Compressor runtime (24h)",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn_key="comp_runtime_24h_h",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NibeStatSensor(coordinator, entry, desc) for desc in SENSORS
    )


class NibeStatSensor(CoordinatorEntity, SensorEntity):
    entity_description: NibeStatSensorDescription

    def __init__(self, coordinator, entry: ConfigEntry, description: NibeStatSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nibe Smart Control",
            manufacturer="gustjoha/nibe-heatpump-homeassistant",
            model="F1245 curve controller (addon)",
            configuration_url=f"{coordinator.base_url}",
        )

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self.entity_description.value_fn_key)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None
