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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (CONNECT_GRACE, CTRL_CHAR, DOMAIN, EVENT_BUTTON, FINDPHONE_GAP,
                    FRESH_WINDOW, KEEPALIVE, NOTIFY_CHAR, POLL_EVERY, PROXY_ROOM_OVERRIDES,
                    ROOM_SWITCH_MARGIN, RSSI_AGE_PENALTY, SERVICE_UUID,
                    UPDATE_INTERVAL_SECONDS)
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
        self._bt_unsub = None  # real-time advertisement callback (responsive room tracking)
        self._advert_count = 0  # diagnostic: how many adverts the callback has delivered

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
        # App-level auth/bind (0x01) — the vendor app sends this on connect; GB doesn't.
        # This ring may gate activity/step tracking behind it. Best-effort (ring may not ack).
        try:
            await self._command(commands.CMD_AUTH_BIND)
            await asyncio.sleep(0.3)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("auth-bind on connect failed (non-fatal)")
        # NOTE: do NOT set the clock on every connect — on this firmware setting the time
        # (0x04) appears to reset the current day's step counter, so re-setting it on each
        # keepalive/poll/sync reconnect was zeroing steps right before we read them. Set the
        # clock only via the explicit set_time service (call it once; it persists).
        try:
            if self._camera_armed:  # re-arm the shake-for-selfie button after a reconnect
                await self._command(commands.CMD_REMOTE_CAMERA, commands.camera_mode_payload(True))
        except Exception:  # noqa: BLE001 — best effort
            _LOGGER.debug("camera re-arm on connect failed (non-fatal)")
        await asyncio.sleep(1.0)  # GB waits ~1s before a multi-fetch or the ring may not respond

    async def async_disconnect(self) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    async def _release(self) -> None:
        """Drop the BLE link after an operation (unless KEEPALIVE holds it deliberately).

        A connected ring can't advertise, and the proxy-side link otherwise lingers ~10 min
        after a poll — observed blinding room tracking (away 01:45->01:56) until it timed out.
        """
        if KEEPALIVE:
            return
        try:
            await self.async_disconnect()
        except Exception:  # noqa: BLE001 — releasing is best-effort
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
        result = None
        try:
            while not self._notifies.empty():
                self._notifies.get_nowait()
            await self._command(commands.CMD_PPG_START, commands.ppg_start_payload(ppg_type))
            loop = self.hass.loop
            deadline = loop.time() + window
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
                    continue                                # skip the ~1/s heartbeat
                if p[0] == commands.CMD_PPG_RESULT:
                    r = commands.parse_ppg_result(p[1])
                    # only accept a result of the type we asked for — during e.g. a BP measure
                    # the ring also emits HR-type 0x10 frames; grabbing those gave a wrong
                    # "systolic" (an HR value) and no diastolic.
                    if r and r["type_bit"] == (1 << ppg_type) and r["value"]:
                        result = r
                        break
        except Exception as err:  # noqa: BLE001 — ring often drops mid-measure; never crash
            _LOGGER.debug("PPG measure failed: %s", err)
            self._client = None
        return result                                        # dict {value, extra} or None

    # ---------------------------------------------------------------- operations
    async def set_time(self, when: Optional[datetime] = None) -> None:
        async with self._lock:
            try:
                await self._ensure_connected()
                await self._command(commands.CMD_TIME, commands.time_payload(when))  # no ack
            finally:
                await self._release()

    async def find(self) -> None:
        """Flash the ring's LED (green) to locate it — this ring has no vibration motor."""
        async with self._lock:
            try:
                await self._ensure_connected()
                await self._command(commands.CMD_FIND_DEVICE)
            finally:
                await self._release()

    async def set_profile(self, gender: int, height_cm: int, weight_kg: int, age: int) -> None:
        """Set the user profile (0x06) so distance/calories compute from real body metrics."""
        async with self._lock:
            try:
                await self._ensure_connected()
                await self._command(commands.CMD_PROFILE,
                                    commands.profile_payload(gender, height_cm, weight_kg, age))
            finally:
                await self._release()

    async def set_camera_mode(self, enabled: bool) -> None:
        """Arm/disarm 'shake for selfie' mode (0x0D). While armed, a shake fires a 0x0E push
        -> a `lefun_ring_button` event (action=camera). Persists across reconnects."""
        self._camera_armed = enabled
        async with self._lock:
            try:
                await self._ensure_connected()
                await self._command(commands.CMD_REMOTE_CAMERA,
                                    commands.camera_mode_payload(enabled))
            finally:
                await self._release()

    async def measure_heart_rate(self, timeout: float = 45.0) -> Optional[int]:
        async with self._lock:
            try:
                await self._ensure_connected()
                r = await self._measure_ppg(commands.PPG_TYPE_HEART_RATE, window=timeout)
            finally:
                await self._release()
        hr = r["value"] if r else None
        nd = dict(self.data or {})
        if hr:
            nd["heart_rate"] = hr
        self.async_set_updated_data(nd)
        return hr

    async def measure_spo2(self, timeout: float = 45.0) -> Optional[int]:
        async with self._lock:
            try:
                await self._ensure_connected()
                r = await self._measure_ppg(commands.PPG_TYPE_BLOOD_OXYGEN, window=timeout)
            finally:
                await self._release()
        spo2 = r["value"] if r else None
        nd = dict(self.data or {})
        if spo2:
            nd["spo2"] = spo2
        self.async_set_updated_data(nd)
        return spo2

    async def measure_blood_pressure(self, timeout: float = 45.0) -> Optional[dict]:
        """Experimental: cuff-less PPG estimate. This ring reports a SINGLE pressure value
        (~systolic); its 0x10 BP frame has no diastolic byte. Treat as indicative only."""
        async with self._lock:
            try:
                await self._ensure_connected()
                r = await self._measure_ppg(commands.PPG_TYPE_BLOOD_PRESSURE, window=timeout)
            finally:
                await self._release()
        if not r:
            return None
        self.async_set_updated_data({**(self.data or {}), "bp_systolic": r["value"]})
        return {"systolic": r["value"]}

    async def async_sync(self) -> dict:
        """On-demand connected read of battery + steps/activity (manual refresh + diagnosis).

        Steps are stored on the ring, so this works whenever it's connected — no need to
        catch it right after a walk."""
        data = dict(self.data or {})
        async with self._lock:
            try:
                await self._ensure_connected()
                bat = await self._command_with_response(commands.CMD_BATTERY)
                if bat is not None:
                    data["battery"] = commands.parse_battery(bat)
                buckets = await self._command_collect(commands.CMD_ACTIVITY, bytes([0]))
                day = commands.sum_activity(buckets)
                if day is None:
                    summary = await self._command_with_response(commands.CMD_STEPS, bytes([0]))
                    if summary:
                        day = commands.parse_steps(summary)
                if day:
                    data.update({"steps": day["steps"], "distance_m": day["distance_m"],
                                 "calories": day["calories"], "steps_date": day["date"]})
            except Exception as err:  # noqa: BLE001 — never crash the service
                _LOGGER.debug("sync failed: %s", err)
                self._client = None
            finally:
                await self._release()
        self.async_set_updated_data(data)
        return data

    # ---------------------------------------------------------------- room tracking
    def _recompute_location(self, data: dict) -> bool:
        """Fill ``data``'s location fields and return True if the room changed.

        "Nearest room" is chosen only among proxies that have heard the ring within FRESH_WINDOW
        (tracked in real time by ``_on_advertisement``) — so a room you just left ages out fast
        instead of its stale-but-strong reading holding the lights. Falls back to HA's advert
        cache if the real-time callback hasn't delivered anything (keeps working if it's quiet).
        Hysteresis (ROOM_SWITCH_MARGIN) then avoids flip-flopping between adjacent rooms."""
        now = self.hass.loop.time()
        proxies: dict[str, int] = {}   # every proxy with the ring in its cache -> last rssi
        ages: dict[str, float] = {}    # proxy -> seconds since IT last heard the ring
        for sd in bluetooth.async_scanner_devices_by_address(self.hass, self.address, False):
            adv = sd.advertisement
            if adv is None or adv.rssi is None:
                continue
            name = sd.scanner.name or sd.scanner.source
            proxies[name] = adv.rssi
            # habluetooth keeps a per-scanner last-heard monotonic timestamp. This is the ground
            # truth: HA's advert cache retains a proxy's LAST reading for minutes after it stops
            # hearing the ring, and comparing a fresh weak reading against a stale strong one is
            # exactly what left the room "stuck" on the old proxy.
            ts = getattr(sd.scanner, "discovered_device_timestamps", {}).get(self.address)
            if ts is not None:
                ages[name] = now - ts

        fresh = {n: r for n, r in proxies.items() if ages.get(n, 1e9) < FRESH_WINDOW}
        if proxies and not ages:  # habluetooth without timestamps: degrade to cache-only
            fresh = dict(proxies)

        # Score = rssi - penalty*age: the ring's adverts are caught only ~once/min per proxy,
        # so a proxy that heard the ring JUST NOW must beat one that heard it a bit louder 20s
        # ago — that older reading is usually the room being left.
        def score(name: str) -> float:
            return fresh[name] - RSSI_AGE_PENALTY * ages.get(name, 0.0)

        data["advert_count"] = self._advert_count
        data["proxy_ages"] = {n: round(a, 1) for n, a in ages.items()}
        prev_room = data.get("room")
        connected = self._client is not None and self._client.is_connected
        if fresh:
            nearest_name = max(fresh, key=score)
            # Hysteresis: keep the currently-held proxy unless a different one clearly wins
            # (score margin) — but ONLY while the held proxy still hears the ring. A stale
            # proxy can't hold the room, whatever its last reading was.
            chosen_name = nearest_name
            if self._last_proxy in fresh and nearest_name != self._last_proxy:
                if score(nearest_name) - score(self._last_proxy) < ROOM_SWITCH_MARGIN:
                    chosen_name = self._last_proxy
            chosen_rssi = fresh[chosen_name]
            self._last_room = proxy_to_room(chosen_name)
            self._last_proxy, self._last_rssi = chosen_name, chosen_rssi
            data["nearest_proxy"] = chosen_name
            data["room"] = self._last_room or "unknown"
            data["nearest_rssi"] = chosen_rssi
            data["proxies"] = proxies
            data["last_seen"] = dt_util.utcnow()  # heard now -> stamp; carries over when away
        elif proxies or connected:
            # No proxy heard the ring within FRESH_WINDOW — it's between adverts (it advertises
            # ~1/min when idle) or holding a connection (connected devices don't advertise).
            # Hold the last-known room; "away" comes when the caches empty out entirely.
            data["room"] = self._last_room or ("connected" if connected else "unknown")
            data["nearest_proxy"] = self._last_proxy
            data["nearest_rssi"] = self._last_rssi
            data["proxies"] = proxies or (
                {self._last_proxy: self._last_rssi} if self._last_proxy else {})
        else:
            data["nearest_proxy"] = None
            data["room"] = "away"
            data["nearest_rssi"] = None
            data["proxies"] = {}
        return data.get("room") != prev_room

    def start_location_tracking(self) -> None:
        """Update the room in near-real-time on every advertisement the proxies hear, not just
        on the 60s tick — so follow-me lighting reacts within a second or two of a room change."""
        if self._bt_unsub is not None:
            return
        self._bt_unsub = bluetooth.async_register_callback(
            self.hass, self._on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address),
            bluetooth.BluetoothScanningMode.ACTIVE)

    def stop_location_tracking(self) -> None:
        if self._bt_unsub is not None:
            self._bt_unsub()
            self._bt_unsub = None

    @callback
    def _on_advertisement(self, _service_info, _change) -> None:
        """A proxy heard the ring: recompute the room right away (don't wait for the tick).
        NB: HA fires this only for its preferred source, so per-proxy freshness comes from the
        scanners' discovered_device_timestamps in _recompute_location, not from this callback."""
        self._advert_count += 1
        data = dict(self.data or {})
        if self._recompute_location(data):
            self.async_set_updated_data(data)

    # ---------------------------------------------------------------- sensor poll
    async def _async_update_data(self) -> dict:
        """Every tick: recompute location from the advert cache (no connection). Battery/steps/HR
        (which need a connection) are refreshed only every POLL_EVERY ticks."""
        data = dict(self.data or {})

        # --- location: which proxy hears the ring (advert cache, no connection) ---
        self._recompute_location(data)

        # --- battery: needs a connection; poll every POLL_EVERY ticks. Until the FIRST battery
        # read succeeds, retry only every 4th tick (~1 min) — retrying every 15s tick kept a
        # slow/failing connect attempt in flight almost continuously after a restart, starving
        # the room updates. Steps/activity/sleep reads were REMOVED from the poll: this ring
        # has no working accelerometer, so they always came back empty and only lengthened the
        # connected window (during which the ring can't advertise -> tracking goes dark).
        # HR/SpO2 stay on-demand only (each is a ~30s PPG measure).
        self._poll_count += 1
        if ((data.get("battery") is None and self._poll_count % 4 == 1)
                or self._poll_count % POLL_EVERY == 0):
            async with self._lock:
                try:
                    await self._ensure_connected()
                    bat = await self._command_with_response(commands.CMD_BATTERY)
                    if bat is not None:
                        data["battery"] = commands.parse_battery(bat)
                    if data.get("firmware") is None:
                        info = await self._command_with_response(commands.CMD_DEVICE_INFO)
                        di = commands.parse_device_info(info) if info else None
                        if di:
                            data["firmware"] = di["software_version"]
                            data["model_code"] = di["type_code"]
                            data["vendor_code"] = di["vendor_code"]
                except Exception as err:  # noqa: BLE001 - a flaky/failed BLE poll must never
                    # fail the coordinator; location still updates and sensors keep last value.
                    _LOGGER.debug("connected poll skipped: %s", err)
                    self._client = None
                finally:
                    await self._release()
        return data
