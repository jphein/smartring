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

from .const import (CONNECT_GRACE, CTRL_CHAR, DOMAIN, EVENT_BUTTON, FINDPHONE_GAP,
                    KEEPALIVE, NOTIFY_CHAR, POLL_EVERY, PROXY_ROOM_OVERRIDES,
                    SERVICE_UUID, UPDATE_INTERVAL_SECONDS)
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
        self._camera_armed = False
        self._keepalive_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._connected_at = 0.0
        self._last_findphone = 0.0
        self._last_room: Optional[str] = None
        self._last_proxy: Optional[str] = None
        self._last_rssi: Optional[int] = None
        self._ppg_debug: list[str] = []  # last measurement's frames, surfaced for diagnosis

    # ---------------------------------------------------------------- connection
    def _on_disconnect(self, _client: BleakClient) -> None:
        self._client = None

    def _fire_button(self, action: str, raw: bytes) -> None:
        _LOGGER.debug("ring button: %s (%s)", action, raw.hex(" "))
        self.hass.bus.async_fire(
            EVENT_BUTTON, {"address": self.address, "action": action, "raw": raw.hex(" ")})

    def _on_notify(self, _char, data: bytearray) -> None:
        b = bytes(data)
        self._notifies.put_nowait(b)
        # Ring-as-button: fire HA events on shake pushes.
        parsed = commands.parse_packet(b)
        if not parsed:
            return
        cmd, _params = parsed
        now = self.hass.loop.time()
        if cmd == commands.CMD_REMOTE_CAMERA_TRIGGERED:
            self._fire_button("camera", b)                 # discrete, clean (armed via 0x0D)
        elif cmd == commands.CMD_FIND_PHONE:
            # The ring streams 0x0A ~1/s as a heartbeat; only treat a 0x0A that follows a gap
            # (and not right after connect) as a genuine find-phone shake. Calibrate with the
            # `listen` CLI by doing a real find-phone shake and comparing frames.
            if (now - self._connected_at > CONNECT_GRACE
                    and now - self._last_findphone > FINDPHONE_GAP):
                self._fire_button("find_phone", b)
            self._last_findphone = now

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
        self._connected_at = self.hass.loop.time()
        # GB sets the ring clock on every connect (fixes step-day attribution) and waits ~1s
        # before any multi-fetch read, or the ring sometimes won't respond. Ring never ACKs.
        try:
            await self._command(commands.CMD_TIME, commands.time_payload())
            if self._camera_armed:  # re-arm the shake-for-selfie button after a reconnect
                await self._command(commands.CMD_REMOTE_CAMERA, commands.camera_mode_payload(True))
        except Exception:  # noqa: BLE001 — best effort
            _LOGGER.debug("set-time/camera-arm on connect failed (non-fatal)")
        await asyncio.sleep(1.2)

    async def async_disconnect(self) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    # ---------------------------------------------------------------- keepalive (ring-as-button)
    def start_keepalive(self) -> None:
        """Hold a persistent connection so the ring's shake pushes fire HA events.

        Costs one of the BLE proxy's ~3 connection slots + faster ring-battery drain, so it
        only reconnects when a proxy actually hears the ring (no churn while it's away)."""
        if KEEPALIVE and self._keepalive_task is None:
            self._keepalive_task = self.hass.async_create_background_task(
                self._keepalive_loop(), name=f"{DOMAIN}_keepalive")

    async def stop_keepalive(self) -> None:
        self._stopping = True
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    async def _keepalive_loop(self) -> None:
        backoff = 5
        while not self._stopping:
            connected = self._client is not None and self._client.is_connected
            if not connected:
                dev = bluetooth.async_ble_device_from_address(self.hass, self.address, True)
                if dev is not None:  # only try when a proxy hears it — avoid churn when away
                    try:
                        async with self._lock:
                            await self._ensure_connected()
                        backoff = 5
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("keepalive reconnect failed: %s", err)
                        backoff = min(backoff * 2, 120)
            connected = self._client is not None and self._client.is_connected
            await asyncio.sleep(15 if connected else backoff)

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

    async def _command_collect(self, cmd: int, params: bytes = b"",
                               window: float = 6.0) -> list[bytes]:
        """Send a command and collect ALL matching-cmd response frames within ``window``.

        For a multi-record fetch (0x13 activity buckets) the ring streams one frame per
        bucket after a single request, so we accumulate over a time window instead of
        returning on the first frame like :meth:`_command_with_response`."""
        while not self._notifies.empty():
            self._notifies.get_nowait()
        await self._command(cmd, params)
        loop = self.hass.loop
        deadline = loop.time() + window
        frames: list[bytes] = []
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(self._notifies.get(), remaining)
            except asyncio.TimeoutError:
                break
            parsed = commands.parse_packet(frame)
            if parsed and parsed[0] == cmd:
                frames.append(parsed[1])
        return frames

    async def _measure_ppg(self, ppg_type: int, window: float = 30.0) -> Optional[dict]:
        """Start a typed PPG measurement (0x0F + ppgType) and wait for the 0x10 result.

        Returns the parsed result dict ({value, extra}) or None. The 0x0F response is only a
        start-ack; the real reading arrives ~15-30s later in a 0x10 frame (<ppgType><value>).
        Requires the ring to be worn (finger on the sensor)."""
        while not self._notifies.empty():
            self._notifies.get_nowait()
        await self._command(commands.CMD_PPG_START, commands.ppg_start_payload(ppg_type))
        loop = self.hass.loop
        deadline = loop.time() + window
        seen: list[str] = []
        result = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(self._notifies.get(), remaining)
            except asyncio.TimeoutError:
                break
            p = commands.parse_packet(frame)
            if not p or p[0] == commands.CMD_FIND_PHONE:
                continue                                    # skip the ~1/s heartbeat
            seen.append(frame.hex(" "))
            if p[0] == commands.CMD_PPG_RESULT:
                r = commands.parse_ppg_result(p[1])
                if r and r["value"]:
                    result = r
                    break
        self._ppg_debug = seen[-15:]                        # surfaced for diagnosis
        return result["value"]

    # ---------------------------------------------------------------- operations
    async def set_time(self, when: Optional[datetime] = None) -> None:
        async with self._lock:
            await self._ensure_connected()
            await self._command(commands.CMD_TIME, commands.time_payload(when))  # ring doesn't ack

    async def find(self) -> None:
        """Flash the ring's LED (green) to locate it — this ring has no vibration motor."""
        async with self._lock:
            await self._ensure_connected()
            await self._command(commands.CMD_FIND_DEVICE)

    async def set_profile(self, gender: int, height_cm: int, weight_kg: int, age: int) -> None:
        """Set the user profile (0x06) so distance/calories compute from real body metrics."""
        async with self._lock:
            await self._ensure_connected()
            await self._command(commands.CMD_PROFILE,
                                commands.profile_payload(gender, height_cm, weight_kg, age))

    async def set_camera_mode(self, enabled: bool) -> None:
        """Arm/disarm 'shake for selfie' mode (0x0D). While armed, a shake fires a 0x0E push
        -> a `lefun_ring_button` event (action=camera). Persists across reconnects."""
        self._camera_armed = enabled
        async with self._lock:
            await self._ensure_connected()
            await self._command(commands.CMD_REMOTE_CAMERA, commands.camera_mode_payload(enabled))

    async def measure_heart_rate(self, timeout: float = 45.0) -> Optional[int]:
        async with self._lock:
            await self._ensure_connected()
            r = await self._measure_ppg(commands.PPG_TYPE_HEART_RATE, window=timeout)
        hr = r["value"] if r else None
        nd = {**(self.data or {}), "ppg_debug": self._ppg_debug}  # always surface frames
        if hr:
            nd["heart_rate"] = hr
        self.async_set_updated_data(nd)
        return hr

    async def measure_spo2(self, timeout: float = 45.0) -> Optional[int]:
        async with self._lock:
            await self._ensure_connected()
            r = await self._measure_ppg(commands.PPG_TYPE_BLOOD_OXYGEN, window=timeout)
        spo2 = r["value"] if r else None
        nd = {**(self.data or {}), "ppg_debug": self._ppg_debug}
        if spo2:
            nd["spo2"] = spo2
        self.async_set_updated_data(nd)
        return spo2

    async def measure_blood_pressure(self, timeout: float = 45.0) -> Optional[dict]:
        """Experimental: cuff-less PPG estimate — systolic/diastolic. Treat as indicative only."""
        async with self._lock:
            await self._ensure_connected()
            r = await self._measure_ppg(commands.PPG_TYPE_BLOOD_PRESSURE, window=timeout)
        nd = {**(self.data or {}), "ppg_debug": self._ppg_debug}
        if r:
            nd["bp_systolic"], nd["bp_diastolic"] = r["value"], r.get("extra")
        self.async_set_updated_data(nd)
        if not r:
            return None
        bp = {"systolic": r["value"], "diastolic": r.get("extra")}
        return bp

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
        connected = self._client is not None and self._client.is_connected
        if proxies:
            # fresh advertisements — trust them and remember the room
            self._last_room = proxy_to_room(nearest_name)
            self._last_proxy, self._last_rssi = nearest_name, nearest_rssi
            data["nearest_proxy"] = nearest_name
            data["room"] = self._last_room or "unknown"
            data["nearest_rssi"] = nearest_rssi
            data["proxies"] = proxies
        elif connected:
            # A connected BLE device stops advertising, so the advert cache is empty even
            # though we're clearly in range. Keep the last-known room instead of "away".
            data["room"] = self._last_room or "connected"
            data["nearest_proxy"] = self._last_proxy
            data["nearest_rssi"] = self._last_rssi
            data["proxies"] = {self._last_proxy: self._last_rssi} if self._last_proxy else {}
        else:
            data["nearest_proxy"] = None
            data["room"] = "away"
            data["nearest_rssi"] = None
            data["proxies"] = {}

        # --- battery/steps/HR: need a connection; poll on the first tick then every POLL_EVERY ---
        self._poll_count += 1
        if data.get("battery") is None or self._poll_count % POLL_EVERY == 0:
            async with self._lock:
                try:
                    await self._ensure_connected()
                    bat = await self._command_with_response(commands.CMD_BATTERY)
                    if bat is not None:
                        data["battery"] = commands.parse_battery(bat)
                    # today's steps = SUM of the 0x13 intraday activity buckets. 0x12 is only a
                    # finalized daily summary the firmware doesn't keep live (GB never polls it),
                    # so a 0x12 daysAgo=0 poll reads 0 mid-day. Fall back to the 0x12 summary for
                    # date/zero when the ring has no buckets yet today.
                    buckets = await self._command_collect(commands.CMD_ACTIVITY, bytes([0]))
                    dbg = [b.hex(" ") for b in buckets][:15]   # diagnosis: raw activity buckets
                    day = commands.sum_activity(buckets)
                    if day is None:
                        summary = await self._command_with_response(commands.CMD_STEPS, bytes([0]))
                        if summary:
                            dbg.append("0x12:" + summary.hex(" "))
                        day = commands.parse_steps(summary) if summary else None
                    data["steps_debug"] = dbg
                    if day:
                        data.update({"steps": day["steps"],
                                     "distance_m": day["distance_m"],
                                     "calories": day["calories"],
                                     "steps_date": day["date"]})
                    if data.get("firmware") is None:
                        info = await self._command_with_response(commands.CMD_DEVICE_INFO)
                        di = commands.parse_device_info(info) if info else None
                        if di:
                            data["firmware"] = di["software_version"]
                            data["model_code"] = di["type_code"]
                            data["vendor_code"] = di["vendor_code"]
                    hr = await self._measure_ppg(commands.PPG_TYPE_HEART_RATE, window=30.0)
                    if hr:
                        data["heart_rate"] = hr["value"]
                    data["ppg_debug"] = self._ppg_debug     # diagnosis: raw PPG frames
                    spo2 = await self._measure_ppg(commands.PPG_TYPE_BLOOD_OXYGEN, window=30.0)
                    if spo2:
                        data["spo2"] = spo2["value"]
                    night = commands.summarize_sleep(
                        await self._command_collect(commands.CMD_SLEEP, bytes([0])))
                    if night:
                        data.update({"sleep_total_min": night["total_min"],
                                     "sleep_deep_min": night["deep_min"],
                                     "sleep_light_min": night["light_min"],
                                     "sleep_date": night["date"]})
                except Exception as err:  # noqa: BLE001 - a flaky/failed BLE poll must never
                    # fail the coordinator; location still updates and sensors keep last value.
                    _LOGGER.debug("connected poll skipped: %s", err)
                    self._client = None
        return data
