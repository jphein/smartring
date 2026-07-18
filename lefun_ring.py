"""lefun_ring — talk to a Lefun-protocol BLE smart ring.

Drop-in for Home Assistant's Bluetooth stack: the :class:`LefunRing` class takes a
``bleak.backends.device.BLEDevice`` (which HA supplies from its adapter *or* an
ESPHome BLE proxy transparently) and connects via ``bleak_retry_connector.
establish_connection`` — the same robust connect path HA uses. It also runs
standalone as a CLI for testing.

Protocol reference: Gadgetbridge's Lefun driver (service 0x18D0, write 0x2D01,
notify 0x2D00; request preamble 0xAB, response preamble 0x5A; bit-wise checksum).

    HA usage:
        ring = LefunRing(ble_device)
        state = await ring.async_poll()      # battery, steps, hr, ...
        await ring.async_disconnect()

    CLI usage:
        python lefun_ring.py --address <RING_MAC> poll
        python lefun_ring.py --address <RING_MAC> hr
        python lefun_ring.py --address <RING_MAC> raw 0x00
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import asdict, dataclass, field

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections,
    establish_connection,
)

_LOGGER = logging.getLogger(__name__)

# ---- Lefun protocol constants -------------------------------------------------
SERVICE_UUID = "000018d0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "00002d01-0000-1000-8000-00805f9b34fb"   # write-without-response
NOTIFY_UUID = "00002d00-0000-1000-8000-00805f9b34fb"  # notify

REQUEST_PREAMBLE = 0xAB
RESPONSE_PREAMBLES = (0x5A, 0x5B)  # spec says 0x5A; some units answer 0x5B
HEADER_LEN = 4  # preamble + length + command + checksum

CMD_DEVICE_INFO = 0x00
CMD_BATTERY = 0x03
CMD_TIME = 0x04
CMD_PROFILE = 0x06
CMD_FIND_PHONE = 0x0A
CMD_REMOTE_CAMERA = 0x0D
CMD_REMOTE_CAMERA_TRIGGERED = 0x0E
GENDER_FEMALE = 0
GENDER_MALE = 1
CMD_HR_START = 0x0F      # PPG start; param = ppgType bitmask (1 << type). Response = start-ack.
CMD_HR_RESULT = 0x10     # PPG result <ppgType><value>, arrives ~15-30s after start
CMD_STEPS = 0x12
CMD_ACTIVITY = 0x13
CMD_SLEEP = 0x15

PPG_TYPE_HEART_RATE = 0
PPG_TYPE_BLOOD_OXYGEN = 1
PPG_TYPE_BLOOD_PRESSURE = 2

CMD_NAMES = {v: k for k, v in globals().items() if k.startswith("CMD_")}


def checksum(data: bytes) -> int:
    """Lefun bit-wise checksum (ported verbatim from Gadgetbridge)."""
    c = 0
    for byte in data:
        b = byte & 0xFF
        for _ in range(8):
            if ((b ^ c) & 1) == 0:
                c >>= 1
            else:
                c = ((c ^ 0x18) >> 1) | 0x80
            b >>= 1
    return c & 0xFF


def build_packet(cmd: int, params: bytes = b"") -> bytes:
    """Assemble a request: AB | len | cmd | params… | checksum."""
    body = bytes([REQUEST_PREAMBLE, HEADER_LEN + len(params), cmd]) + params
    return body + bytes([checksum(body)])


def parse_packet(pkt: bytes) -> tuple[int, bytes] | None:
    """Validate a response frame; return (command, params) or None if malformed."""
    if len(pkt) < HEADER_LEN or pkt[0] not in RESPONSE_PREAMBLES:
        return None
    length = pkt[1]
    if length != len(pkt):  # tolerate; some firmwares report 0 — fall back to len
        length = len(pkt)
    cmd = pkt[2]
    params = pkt[3:length - 1]
    return cmd, params


@dataclass
class RingState:
    address: str | None = None
    battery: int | None = None
    steps: int | None = None
    calories: int | None = None
    distance_m: int | None = None
    steps_date: str | None = None
    heart_rate: int | None = None
    spo2: int | None = None
    bp_systolic: int | None = None
    bp_diastolic: int | None = None
    sleep_total_min: int | None = None
    sleep_deep_min: int | None = None
    sleep_light_min: int | None = None
    sleep_date: str | None = None
    firmware: str | None = None
    model: str | None = None
    raw: dict[str, str] = field(default_factory=dict)


class LefunRing:
    """A connected Lefun ring. Give it a BLEDevice; call the async_* methods."""

    def __init__(self, ble_device: BLEDevice, connect_timeout: float = 20.0):
        self._device = ble_device
        self._connect_timeout = connect_timeout
        self._client: BleakClientWithServiceCache | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._time_synced = False  # clock set once per connection (see _ensure_time_synced)

    # -- connection ------------------------------------------------------------
    async def async_connect(self) -> None:
        if self._client and self._client.is_connected:
            return
        # A bonded (esp. trusted) ring gets auto-reconnected by BlueZ with no
        # bleak owner, so establish_connection fails with "already connected".
        # close_stale_connections handles clients bleak knows about; for a
        # foreign/ghost ACL, force a BlueZ-level disconnect and retry once.
        await close_stale_connections(self._device)
        self._time_synced = False  # fresh connection -> re-sync the clock on first read
        for attempt in range(2):
            try:
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._device,
                    self._device.address,
                    timeout=self._connect_timeout,
                )
                break
            except Exception:
                if attempt == 1:
                    raise
                try:  # force-drop the ghost connection, then retry
                    await BleakClient(self._device).disconnect()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(1.0)
        await self._client.start_notify(NOTIFY_UUID, self._on_notify)

    async def async_disconnect(self) -> None:
        if self._client:
            try:
                await self._client.stop_notify(NOTIFY_UUID)
            except Exception:  # noqa: BLE001 — best effort on teardown
                pass
            await self._client.disconnect()
            self._client = None

    async def __aenter__(self) -> "LefunRing":
        await self.async_connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.async_disconnect()

    # -- low level -------------------------------------------------------------
    def _on_notify(self, _char, data: bytearray) -> None:
        _LOGGER.debug("notify %s", bytes(data).hex(" "))
        self._queue.put_nowait(bytes(data))

    async def async_command(
        self, cmd: int, params: bytes = b"", collect: float = 2.0
    ) -> list[bytes]:
        """Send a command and collect notification frames for `collect` seconds."""
        assert self._client is not None, "call async_connect() first"
        # drain stale frames
        while not self._queue.empty():
            self._queue.get_nowait()
        pkt = build_packet(cmd, params)
        _LOGGER.debug("write %s (%s)", pkt.hex(" "), CMD_NAMES.get(cmd, hex(cmd)))
        await self._client.write_gatt_char(WRITE_UUID, pkt, response=False)
        frames: list[bytes] = []
        loop = asyncio.get_event_loop()
        deadline = loop.time() + collect
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                frames.append(frame)
            except asyncio.TimeoutError:
                break
        return frames

    # -- high level ------------------------------------------------------------
    async def _ensure_time_synced(self) -> None:
        """Set the ring clock once per connection, then let it settle before a multi-fetch.

        Gadgetbridge sets time (0x04) on *every* connect — this fixes step-day attribution —
        and inserts a ~1s wait before any multi-fetch read (``MultiFetchRequest.prePerform``:
        "device sometimes won't respond") or the ring stays silent. Best-effort: the ring
        never ACKs the SET, but the clock takes.
        """
        if self._time_synced:
            return
        try:
            await self.async_set_time()
        except Exception:  # noqa: BLE001 — best effort; ring doesn't ack anyway
            pass
        await asyncio.sleep(1.2)
        self._time_synced = True

    async def async_poll(self) -> RingState:
        """One-shot read of the common metrics.

        Today's steps come from the **0x13 activity buckets (summed)**, not a 0x12 poll:
        0x12 is a finalized daily summary the firmware doesn't keep live, so a 0x12 daysAgo=0
        poll reads 0 mid-day (Gadgetbridge never polls 0x12 — it sums the 0x13 buckets).
        """
        state = RingState(address=self._device.address)
        self._apply(state, CMD_DEVICE_INFO, await self.async_command(CMD_DEVICE_INFO))
        self._apply(state, CMD_BATTERY, await self.async_command(CMD_BATTERY))
        await self._ensure_time_synced()
        # today's steps/distance/calories = sum of the 0x13 intraday buckets
        self._apply(state, CMD_ACTIVITY,
                    await self.async_command(CMD_ACTIVITY, bytes([0]), collect=6.0))
        if state.steps is None:  # no 0x13 buckets today -> fall back to 0x12 for date + zero
            self._apply(state, CMD_STEPS, await self.async_command(CMD_STEPS, bytes([0])))
        return state

    async def async_set_time(self, when=None) -> bool | None:
        """Set the ring's clock (fixes step-day attribution). Returns success flag."""
        import datetime

        t = when or datetime.datetime.now()
        params = bytes([1, t.year % 100, t.month, t.day, t.hour, t.minute, t.second])
        for f in await self.async_command(CMD_TIME, params):
            p = parse_packet(f)
            if p and p[0] == CMD_TIME and len(p[1]) >= 2:
                return p[1][1] == 1
        return None

    async def async_steps(self, days_ago: int = 0) -> RingState:
        """Read the 0x12 *finalized daily summary* for a day (0 = today … 6).

        NOTE: for daysAgo=0 (today) this reads 0 until the day's summary is finalized —
        use :meth:`async_activity` for today's running total. Kept for finalized PAST days.
        """
        state = RingState(address=self._device.address)
        frames = await self.async_command(CMD_STEPS, bytes([days_ago & 0xFF]))
        self._apply(state, CMD_STEPS, frames)
        return state

    async def async_activity(self, days_ago: int = 0) -> RingState:
        """Read + SUM the 0x13 intraday activity buckets for a day (0 = today … 6).

        This is the live/stored step total on the Lefun protocol (Gadgetbridge sums these
        buckets for its daily figure). Multi-record: the ring streams one frame per bucket,
        which we collect over a window and accumulate.
        """
        state = RingState(address=self._device.address)
        await self._ensure_time_synced()
        frames = await self.async_command(CMD_ACTIVITY, bytes([days_ago & 0xFF]), collect=8.0)
        self._apply(state, CMD_ACTIVITY, frames)
        if state.steps is None and days_ago == 0:  # no buckets -> 0x12 fallback for date + zero
            self._apply(state, CMD_STEPS, await self.async_command(CMD_STEPS, bytes([0])))
        return state

    async def async_heart_rate(self, timeout: float = 30.0) -> RingState:
        """Start a typed HR measurement (0x0F + ppgType) and wait for the 0x10 result.

        The 0x0F response is only a start-ack; the BPM arrives ~15-30s later in a 0x10
        frame. Requires the ring to be worn. Sending 0x0F WITHOUT the ppgType byte returns
        success=0 and never measures (the old 'always 77' bug)."""
        state = RingState(address=self._device.address)
        frames = await self.async_command(
            CMD_HR_START, bytes([1 << PPG_TYPE_HEART_RATE]), collect=timeout)
        self._apply(state, CMD_HR_RESULT, frames)
        return state

    async def async_spo2(self, timeout: float = 30.0) -> RingState:
        """Start a typed SpO2 measurement and wait for the 0x10 result. Requires the ring worn."""
        state = RingState(address=self._device.address)
        frames = await self.async_command(
            CMD_HR_START, bytes([1 << PPG_TYPE_BLOOD_OXYGEN]), collect=timeout)
        self._apply(state, CMD_HR_RESULT, frames)
        return state

    async def async_blood_pressure(self, timeout: float = 30.0) -> RingState:
        """Experimental cuff-less BP estimate (systolic/diastolic). Indicative only."""
        state = RingState(address=self._device.address)
        frames = await self.async_command(
            CMD_HR_START, bytes([1 << PPG_TYPE_BLOOD_PRESSURE]), collect=timeout)
        self._apply(state, CMD_HR_RESULT, frames)
        return state

    async def async_sleep(self, days_ago: int = 0) -> RingState:
        """Read a night's sleep (0x15): total/deep/light minutes from segment timestamps."""
        import datetime
        state = RingState(address=self._device.address)
        frames = await self.async_command(CMD_SLEEP, bytes([days_ago & 0xFF]), collect=4.0)
        recs = []
        for f in frames:
            p = parse_packet(f)
            if not p or p[0] != CMD_SLEEP or len(p[1]) < 11:
                continue
            b = p[1]
            if int.from_bytes(b[1:3], "big") == 0 or b[5] == 0xFF:  # empty day / no data
                continue
            recs.append(b)
        if len(recs) >= 2:
            def dt(b):
                return datetime.datetime(2000 + b[5], b[6], b[7], b[8], b[9])
            recs.sort(key=dt)
            mins = {1: 0, 2: 0, 3: 0}  # awake / light / deep
            for a, nxt in zip(recs, recs[1:]):
                mins[a[10]] = mins.get(a[10], 0) + max(int((dt(nxt) - dt(a)).total_seconds() // 60), 0)
            state.sleep_deep_min = mins[3]
            state.sleep_light_min = mins[2]
            state.sleep_total_min = mins[2] + mins[3]
            state.sleep_date = f"20{recs[0][5]:02d}-{recs[0][6]:02d}-{recs[0][7]:02d}"
        return state

    async def async_set_profile(self, sex: str, height_cm: int, weight_kg: int,
                                age: int) -> bool | None:
        """Set body metrics (0x06) so distance/calories compute from real data, not defaults."""
        gender = GENDER_MALE if sex == "male" else GENDER_FEMALE
        params = bytes([1, gender, height_cm & 0xFF, weight_kg & 0xFF, age & 0xFF])
        for f in await self.async_command(CMD_PROFILE, params):
            p = parse_packet(f)
            if p and p[0] == CMD_PROFILE and len(p[1]) >= 2:
                return p[1][1] == 1
        return None

    async def async_set_camera_mode(self, enabled: bool) -> bool | None:
        """Arm/disarm 'shake for selfie' mode (0x0D). While armed, a shake -> a 0x0E push."""
        for f in await self.async_command(CMD_REMOTE_CAMERA, bytes([1 if enabled else 0])):
            p = parse_packet(f)
            if p and p[0] == CMD_REMOTE_CAMERA and p[1]:
                return p[1][0] == 1
        return None

    async def async_listen(self, seconds: float = 30.0, arm_camera: bool = False) -> None:
        """Print every notify frame for N seconds — do the find-phone / camera shake now to
        see exactly what the ring sends (calibrates the ring-as-button detection)."""
        assert self._client is not None, "call async_connect() first"
        if arm_camera:
            await self.async_command(CMD_REMOTE_CAMERA, bytes([1]), collect=0.5)
            print("[camera mode armed — shake to trigger the shutter push (0x0e)]")
        while not self._queue.empty():
            self._queue.get_nowait()
        print(f"[listening {seconds:.0f}s — do the shake gesture(s) now]")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + seconds
        while True:
            rem = deadline - loop.time()
            if rem <= 0:
                break
            try:
                f = await asyncio.wait_for(self._queue.get(), timeout=rem)
            except asyncio.TimeoutError:
                break
            p = parse_packet(f)
            tag = f"cmd=0x{p[0]:02x}" if p else "unparsed"
            print(f"  {f.hex(' ')}   {tag}")

    def _apply(self, state: RingState, cmd: int, frames: list[bytes]) -> None:
        for f in frames:
            parsed = parse_packet(f)
            if not parsed:
                state.raw[f"unparsed_{f[:1].hex()}"] = f.hex(" ")
                continue
            rcmd, params = parsed
            state.raw[CMD_NAMES.get(rcmd, hex(rcmd))] = params.hex(" ")
            if rcmd == CMD_BATTERY and params:
                state.battery = params[0]
            elif rcmd == CMD_STEPS and len(params) >= 16:
                # 0x12 finalized daily summary: daysAgo|year|month|day|steps(BE32)|dist|cal
                year = params[1]
                if year != 0xFF:  # 0xFF = no data recorded that day
                    state.steps_date = f"20{year:02d}-{params[2]:02d}-{params[3]:02d}"
                # don't clobber a 0x13-derived total with the finalized-0 summary
                if state.steps is None:
                    state.steps = int.from_bytes(params[4:8], "big")
                    state.distance_m = int.from_bytes(params[8:12], "big")
                    state.calories = int.from_bytes(params[12:16], "big")
            elif rcmd == CMD_ACTIVITY and len(params) >= 14:
                # 0x13 intraday bucket: daysAgo|totalRecords|currentRecord|Y|M|D|h|m|
                # steps(BE16)|dist(BE16)|cal(BE16). Sum across all buckets for the day (GB's
                # approach); skip empty-day frames (totalRecords == 0).
                if params[1] != 0:
                    year = params[3]
                    if year != 0xFF and state.steps_date is None:
                        state.steps_date = f"20{year:02d}-{params[4]:02d}-{params[5]:02d}"
                    state.steps = (state.steps or 0) + int.from_bytes(params[8:10], "big")
                    state.distance_m = (state.distance_m or 0) + int.from_bytes(params[10:12], "big")
                    state.calories = (state.calories or 0) + int.from_bytes(params[12:14], "big")
            elif rcmd == CMD_HR_RESULT and len(params) >= 2:
                # 0x10 PPG result: <ppgType><value[...]>. Route by type; 0x0F is only a start-ack.
                ptype, val = params[0], params[1]
                if ptype == (1 << PPG_TYPE_HEART_RATE):
                    state.heart_rate = val or None
                elif ptype == (1 << PPG_TYPE_BLOOD_OXYGEN):
                    state.spo2 = val or None
                elif ptype == (1 << PPG_TYPE_BLOOD_PRESSURE):
                    state.bp_systolic = val or None
                    state.bp_diastolic = params[2] if len(params) > 2 else None
            elif rcmd == CMD_DEVICE_INFO and len(params) >= 16:
                pr = lambda b: "".join(ch for ch in b.decode("ascii", "replace") if ch.isprintable()).strip()
                state.model = pr(params[4:8])
                sw = int.from_bytes(params[10:12], "big")
                state.firmware = f"{sw >> 8}.{sw & 0xFF}"
                state.raw["vendor_code"] = pr(params[12:16])


# ---- standalone CLI -----------------------------------------------------------
async def _resolve(address: str) -> BLEDevice:
    # Prefer BlueZ's cache (populated by any recent scan, incl. an ESPHome proxy
    # in HA) — more reliable on adapters whose adverts bleak's scanner misses.
    from bleak_retry_connector import get_device

    dev = await get_device(address)
    if dev:
        return dev
    from bleak import BleakScanner

    for attempt in range(1, 4):
        _LOGGER.info("scanning for %s (%d/3)…", address, attempt)
        dev = await BleakScanner.find_device_by_address(address, timeout=12.0)
        if dev:
            return dev
    raise SystemExit(
        f"device {address} not found. Wake the ring and pre-populate the cache:\n"
        f"  bluetoothctl --timeout 10 scan on   (then re-run)"
    )


async def _cli_main(args: argparse.Namespace) -> int:
    dev = await _resolve(args.address)
    async with LefunRing(dev) as ring:
        if args.cmd == "poll":
            state = await ring.async_poll()
        elif args.cmd == "hr":
            state = await ring.async_heart_rate(timeout=args.timeout)
        elif args.cmd == "steps":
            # today (day 0) = summed 0x13 buckets (live total); past days = 0x12 finalized summary
            if args.day == 0:
                state = await ring.async_activity(days_ago=0)
            else:
                state = await ring.async_steps(days_ago=args.day)
        elif args.cmd == "activity":
            state = await ring.async_activity(days_ago=args.day)
        elif args.cmd == "spo2":
            state = await ring.async_spo2(timeout=args.timeout)
        elif args.cmd == "bp":
            state = await ring.async_blood_pressure(timeout=args.timeout)
        elif args.cmd == "sleep":
            state = await ring.async_sleep(days_ago=args.day)
        elif args.cmd == "profile":
            ok = await ring.async_set_profile(args.sex, args.height, args.weight, args.age)
            print(f"set profile: {'ok' if ok else 'sent (no ack)'}")
            return 0
        elif args.cmd == "camera":
            ok = await ring.async_set_camera_mode(args.state == "on")
            print(f"camera mode {args.state}: {'ok' if ok else 'sent (no ack)'}")
            return 0
        elif args.cmd == "listen":
            await ring.async_listen(seconds=args.timeout, arm_camera=args.camera)
            return 0
        elif args.cmd == "settime":
            ok = await ring.async_set_time()
            print(f"set time: {'ok' if ok else 'failed/no-ack'}")
            return 0
        elif args.cmd == "raw":
            cmd = int(args.opcode, 0)
            params = bytes(int(x, 0) for x in args.params)
            frames = await ring.async_command(cmd, params, collect=args.timeout)
            print(f"sent 0x{cmd:02x} params={params.hex(' ') or '-'}; {len(frames)} frame(s):")
            for f in frames:
                print("  " + f.hex(" "))
            return 0
        else:  # pragma: no cover
            raise SystemExit(f"unknown command {args.cmd}")
    import json

    print(json.dumps({k: v for k, v in asdict(state).items() if v not in (None, {})}, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Lefun BLE smart-ring client")
    p.add_argument("--address", required=True, help="ring BLE MAC, e.g. FF:2A:35:A7:44:F3")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging (show raw frames)")
    p.add_argument("--timeout", type=float, default=20.0, help="collect window / HR wait seconds")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll", help="read device info + battery + steps")
    sub.add_parser("hr", help="trigger a live heart-rate measurement")
    st = sub.add_parser("steps", help="steps/distance/calories (today via 0x13 buckets, past days via 0x12)")
    st.add_argument("--day", type=int, default=0, help="days ago (0=today … 6)")
    ac = sub.add_parser("activity", help="read+sum the 0x13 intraday step buckets for a day")
    ac.add_argument("--day", type=int, default=0, help="days ago (0=today … 6)")
    sub.add_parser("spo2", help="measure blood oxygen (SpO₂); wear the ring")
    sub.add_parser("bp", help="measure blood pressure (experimental); wear the ring")
    sl = sub.add_parser("sleep", help="read a night's sleep stages (0x15)")
    sl.add_argument("--day", type=int, default=0, help="days ago (0=last night)")
    pf = sub.add_parser("profile", help="set user profile for accurate distance/calories")
    pf.add_argument("--sex", choices=["male", "female"], required=True)
    pf.add_argument("--height", type=int, required=True, help="cm")
    pf.add_argument("--weight", type=int, required=True, help="kg")
    pf.add_argument("--age", type=int, required=True)
    cam = sub.add_parser("camera", help="arm/disarm shake-for-selfie mode (0x0D)")
    cam.add_argument("state", choices=["on", "off"])
    ln = sub.add_parser("listen", help="dump notify frames — do the find-phone/camera shake to calibrate")
    ln.add_argument("--camera", action="store_true", help="arm camera mode first")
    sub.add_parser("settime", help="set the ring's clock to now")
    raw = sub.add_parser("raw", help="send one raw command id (+ optional params) and dump responses")
    raw.add_argument("opcode", help="command id, e.g. 0x12")
    raw.add_argument("params", nargs="*", help="optional param bytes, e.g. 0x00")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # keep our frames visible without bleak/dbus D-Bus firehose
    for noisy in ("bleak", "bleak_retry_connector", "dbus_fast"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return asyncio.run(_cli_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
