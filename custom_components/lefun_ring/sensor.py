"""Sensors for a Lefun ring: heart rate, battery, steps, distance, calories, location, RSSI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from homeassistant.components.sensor import (SensorDeviceClass, SensorEntity,
                                             SensorStateClass)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                                 EntityCategory, UnitOfLength)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_NAME, DOMAIN, MANUFACTURER, MODEL
from .coordinator import LefunCoordinator


@dataclass(frozen=True)
class LefunSensorDesc:
    key: str
    name: str
    unit: Optional[str] = None
    device_class: Optional[SensorDeviceClass] = None
    state_class: Optional[SensorStateClass] = None
    category: Optional[EntityCategory] = None
    icon: Optional[str] = None
    attrs: Optional[Callable[[dict], dict]] = None


SENSORS = (
    LefunSensorDesc("heart_rate", "Heart rate", "bpm", None,
                    SensorStateClass.MEASUREMENT, None, "mdi:heart-pulse"),
    LefunSensorDesc("battery", "Battery", PERCENTAGE, SensorDeviceClass.BATTERY,
                    SensorStateClass.MEASUREMENT, EntityCategory.DIAGNOSTIC, "mdi:ring"),
    LefunSensorDesc("steps", "Steps", "steps", None,
                    SensorStateClass.TOTAL_INCREASING, None, "mdi:shoe-print",
                    attrs=lambda d: {"date": d.get("steps_date")}),
    LefunSensorDesc("distance_m", "Distance", UnitOfLength.METERS, SensorDeviceClass.DISTANCE,
                    SensorStateClass.TOTAL_INCREASING, None, "mdi:map-marker-distance"),
    LefunSensorDesc("calories", "Calories", "kcal", None,
                    SensorStateClass.TOTAL_INCREASING, None, "mdi:fire"),
    LefunSensorDesc(
        "room", "Location", None, None, None, None, "mdi:map-marker",
        attrs=lambda d: {
            "proxy": d.get("nearest_proxy"),
            "rssi": d.get("nearest_rssi"),
            "proxies": d.get("proxies") or {},
        }),
    LefunSensorDesc("nearest_rssi", "Signal", SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                    SensorDeviceClass.SIGNAL_STRENGTH, SensorStateClass.MEASUREMENT,
                    EntityCategory.DIAGNOSTIC, "mdi:bluetooth"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: LefunCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(LefunSensor(coordinator, entry, desc) for desc in SENSORS)


class LefunSensor(CoordinatorEntity[LefunCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, desc: LefunSensorDesc) -> None:
        super().__init__(coordinator)
        self._desc = desc
        self._attr_name = desc.name
        self._attr_unique_id = f"{coordinator.address}_{desc.key}"
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class = desc.device_class
        self._attr_state_class = desc.state_class
        self._attr_entity_category = desc.category
        self._attr_icon = desc.icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name=entry.title or DEFAULT_NAME,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get(self._desc.key)

    @property
    def extra_state_attributes(self):
        if self._desc.attrs is None:
            return None
        return self._desc.attrs(self.coordinator.data or {})
