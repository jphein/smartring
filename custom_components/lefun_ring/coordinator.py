"""Connection + protocol coordinator for a Lefun ring, over HA's Bluetooth stack.

HA routes ``bleak`` connections through whatever ESPHome Bluetooth-proxy (active connections)
is in radio range, so the HAOS VM needs no local adapter. This coordinator owns one connection
to the ring, runs the vendored Lefun protocol on it, and polls battery/steps/heart-rate.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import close_stale_connections, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (CTRL_CHAR, DOMAIN, NOTIFY_CHAR, POLL_EVERY,
                    PROXY_ROOM_OVERRIDES, SERVICE_UUID, UPDATE_INTERVAL_SECONDS)
from .proto import commands

_LOGGER = logging.getLogger(__name__)


class LefunError(Exception):
    """A recoverable Lefun connection/command error."""


def proxy_to_room(name: Optional[str]) -> Optional[str]:
    """Derive a friendly room from a proxy/scanner name (e.g. ``office-ble-proxy (…)`` -> Office)."""
    if not name:
        return None
    base = name.split(" (")[0].strip()
    if base in PROXY_ROOM_OVERRIDES:
        return PROXY_ROOM_OVERRIDES[base]
    r = base
    for suf in ("-ble-proxy", "_ble_proxy", "bluetooth-proxy"):
        r = r.replace(suf, " ")
    parts = [p for p in r.replace("-", " ").replace("_", " ").split() if p]
    if len(parts) > 1 and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(parts).title() or base


class LefunCoordinator(DataUpdateCoordinator):
    """Owns the BLE connection and exposes Lefun operations. Also polls sensors."""

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN} {address}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS))
        self.address = address.upper()
        self.device_name = name
        self._client: Optional[BleakClient] = None
        self._lock = asyncio.Lock()
        self._notifies: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._poll_count = 0

    # ---------------------------------------------------------------- connection
    def _on_disconnect(self, _client: BleakClient) -> None:
        self._client = None

    def _on_notify(self, _char, data: bytearray) -> None:
        self._notifies.put_nowait(bytes(data))

    async def _ensure_connected(self) -> None:
        if self._client is not None and self._client.is_connected:
            return
        ble_device: BLEDevice | None = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True)
        if ble_device is None:
            raise LefunError(
                f"{self.address} not reachable via any Bluetooth proxy/adapter "
                "(is the ring worn/awake and in range of a proxy with active connections?)")
        await close_stale_connections(ble_device)
        client = await establish_connection(
            BleakClient, ble_device, self.address, disconnected_callback=self._on_disconnect)
        await client.start_notify(NOTIFY_CHAR, self._on_notify)
        self._client = client

    async def async_disconnect(self) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    # ---------------------------------------------------------------- protocol
    async def _command(self, cmd: int, params: bytes = b"") -> None:
        await self._client.write_gatt_char(CTRL_CHAR, commands.build_packet(cmd, params),
                                           response=False)

    async def _command_with_response(self, cmd: int, params: bytes = b"",
                                     timeout: float = 6.0) -> Optional[bytes]:
        """Send a command and return the params of the first matching-cmd response frame."""
        while not self._notifies.empty():
            self._notifies.get_nowait()
        await self._command(cmd, params)
        loop = self.hass.loop
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                frame = await asyncio.wait_for(self._notifies.get(), remaining)
            except asyncio.TimeoutError:
                return None
            parsed = commands.parse_packet(frame)
            if parsed and parsed[0] == cmd:
                return parsed[1]

    # ---------------------------------------------------------------- operations
    async def set_time(self, when: Optional[datetime] = None) -> None:
        async with self._lock:
            await self._ensure_connected()
            await self._command(commands.CMD_TIME, commands.time_payload(when))  # ring doesn't ack

    async def find(self) -> None:
        """Buzz the ring to locate it."""
        async with self._lock:
            await self._ensure_connected()
            await self._command(commands.CMD_FIND_DEVICE)

    async def measure_heart_rate(self, timeout: float = 25.0) -> Optional[int]:
        async with self._lock:
            await self._ensure_connected()
            params = await self._command_with_response(commands.CMD_HR_START, timeout=timeout)
        hr = commands.parse_hr(params) if params else None
        if hr:
            self.async_set_updated_data({**(self.data or {}), "heart_rate": hr})
        return hr

    # ---------------------------------------------------------------- sensor poll
    async def _async_update_data(self) -> dict:
        """Every tick: recompute location from the advert cache (no connection). Battery/steps/HR
        (which need a connection) are refreshed only every POLL_EVERY ticks."""
        data = dict(self.data or {})

        # --- location: which proxy hears the ring, and how strongly ---
        nearest_name: Optional[str] = None
        nearest_rssi: Optional[int] = None
        proxies: dict = {}
        for sd in bluetooth.async_scanner_devices_by_address(self.hass, self.address, False):
            adv = sd.advertisement
            rssi = adv.rssi if adv is not None else None
            if rssi is None:
                continue
            name = sd.scanner.name or sd.scanner.source
            proxies[name] = rssi
            if nearest_rssi is None or rssi > nearest_rssi:
                nearest_rssi, nearest_name = rssi, name
        data["nearest_proxy"] = nearest_name
        data["room"] = proxy_to_room(nearest_name) or "away"
        data["nearest_rssi"] = nearest_rssi
        data["proxies"] = proxies

        # --- battery/steps/HR: need a connection; poll on the first tick then every POLL_EVERY ---
        self._poll_count += 1
        if data.get("battery") is None or self._poll_count % POLL_EVERY == 0:
            async with self._lock:
                try:
                    await self._ensure_connected()
                    bat = await self._command_with_response(commands.CMD_BATTERY)
                    if bat is not None:
                        data["battery"] = commands.parse_battery(bat)
                    steps = await self._command_with_response(commands.CMD_STEPS, bytes([0]))
                    parsed = commands.parse_steps(steps) if steps else None
                    if parsed:
                        data.update({"steps": parsed["steps"],
                                     "distance_m": parsed["distance_m"],
                                     "calories": parsed["calories"],
                                     "steps_date": parsed["date"]})
                    hr = await self._command_with_response(commands.CMD_HR_START, timeout=25.0)
                    hrv = commands.parse_hr(hr) if hr else None
                    if hrv:
                        data["heart_rate"] = hrv
                except LefunError as err:
                    _LOGGER.debug("connected poll skipped: %s", err)
        return data
