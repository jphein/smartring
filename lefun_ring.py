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
        python lefun_ring.py --address FF:2A:35:A7:44:F3 poll
        python lefun_ring.py --address FF:2A:35:A7:44:F3 hr
        python lefun_ring.py --address FF:2A:35:A7:44:F3 raw 0x00
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import asdict, dataclass, field

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
CMD_HR_START = 0x0F
CMD_HR_RESULT = 0x10
CMD_STEPS = 0x12
CMD_ACTIVITY = 0x13
CMD_SLEEP = 0x15

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
    heart_rate: int | None = None
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

    # -- connection ------------------------------------------------------------
    async def async_connect(self) -> None:
        if self._client and self._client.is_connected:
            return
        # A bonded+trusted ring can be auto-reconnected by BlueZ with no owner,
        # which makes bleak refuse ("already connected"). Drop any such link first.
        await close_stale_connections(self._device)
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            self._device,
            self._device.address,
            timeout=self._connect_timeout,
        )
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
    async def async_poll(self) -> RingState:
        """One-shot read of the common metrics."""
        state = RingState(address=self._device.address)
        for cmd, key in (
            (CMD_DEVICE_INFO, "device_info"),
            (CMD_BATTERY, "battery"),
            (CMD_STEPS, "steps"),
        ):
            frames = await self.async_command(cmd)
            self._apply(state, cmd, frames)
        return state

    async def async_heart_rate(self, timeout: float = 20.0) -> RingState:
        """Trigger a live PPG measurement and wait for the result frame."""
        state = RingState(address=self._device.address)
        frames = await self.async_command(CMD_HR_START, collect=timeout)
        self._apply(state, CMD_HR_START, frames)
        return state

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
            elif rcmd == CMD_STEPS and len(params) >= 4:
                state.steps = int.from_bytes(params[0:4], "little")
                if len(params) >= 8:
                    state.calories = int.from_bytes(params[4:6], "little")
                    state.distance_m = int.from_bytes(params[6:8], "little")
            elif rcmd in (CMD_HR_RESULT, CMD_HR_START) and params:
                state.heart_rate = params[-1] if params[-1] else (params[0] or None)
            elif rcmd == CMD_DEVICE_INFO and params:
                state.raw["device_info"] = params.hex(" ")


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
