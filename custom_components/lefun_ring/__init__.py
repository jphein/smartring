"""The Lefun Smart Ring integration.

Runs the Lefun BLE protocol inside HA via bleak, routed through an ESPHome Bluetooth proxy
(active connections). Exposes battery/steps/heart-rate + location sensors and a few control
services. Shares protocol code with the repo's top-level lefun_ring.py CLI.
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import (DOMAIN, SERVICE_FIND, SERVICE_MEASURE_BP, SERVICE_MEASURE_HR,
                    SERVICE_MEASURE_SPO2, SERVICE_SET_TIME)
from .coordinator import LefunCoordinator, LefunError

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]

# Optional device target on every service; if omitted and only one ring is configured, it's used.
_TARGET = {vol.Optional("device_id"): cv.string}


def _resolve(hass: HomeAssistant, call: ServiceCall) -> LefunCoordinator:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("No Lefun ring is configured")
    device_id = call.data.get("device_id")
    if device_id:
        device = dr.async_get(hass).async_get(device_id)
        if device:
            for identifier in device.identifiers:
                if identifier[0] == DOMAIN:
                    for coord in entries.values():
                        if coord.address == identifier[1]:
                            return coord
        raise HomeAssistantError(f"device_id {device_id} is not a Lefun ring")
    if len(entries) == 1:
        return next(iter(entries.values()))
    raise HomeAssistantError("Multiple Lefun rings configured — pass device_id")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = LefunCoordinator(hass, entry.data[CONF_ADDRESS], entry.title)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: LefunCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_disconnect()
        if not hass.data[DOMAIN]:
            for svc in (SERVICE_SET_TIME, SERVICE_FIND, SERVICE_MEASURE_HR,
                        SERVICE_MEASURE_SPO2, SERVICE_MEASURE_BP):
                hass.services.async_remove(DOMAIN, svc)
    return unloaded


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_TIME):
        return

    async def _guard(coro):
        try:
            return await coro
        except LefunError as err:
            raise HomeAssistantError(str(err)) from err

    async def set_time(call: ServiceCall) -> None:
        await _guard(_resolve(hass, call).set_time())

    async def find(call: ServiceCall) -> None:
        await _guard(_resolve(hass, call).find())

    async def measure_heart_rate(call: ServiceCall) -> None:
        await _guard(_resolve(hass, call).measure_heart_rate())

    async def measure_spo2(call: ServiceCall) -> None:
        await _guard(_resolve(hass, call).measure_spo2())

    async def measure_blood_pressure(call: ServiceCall) -> None:
        await _guard(_resolve(hass, call).measure_blood_pressure())

    reg = hass.services.async_register
    reg(DOMAIN, SERVICE_SET_TIME, set_time, schema=vol.Schema(_TARGET))
    reg(DOMAIN, SERVICE_FIND, find, schema=vol.Schema(_TARGET))
    reg(DOMAIN, SERVICE_MEASURE_HR, measure_heart_rate, schema=vol.Schema(_TARGET))
    reg(DOMAIN, SERVICE_MEASURE_SPO2, measure_spo2, schema=vol.Schema(_TARGET))
    reg(DOMAIN, SERVICE_MEASURE_BP, measure_blood_pressure, schema=vol.Schema(_TARGET))
