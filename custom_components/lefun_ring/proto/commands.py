"""Pure Lefun BLE protocol: framing, checksum, command IDs, payload builders + parsers.

Stdlib-only (no bleak). Ported from Gadgetbridge's Lefun driver and validated live against
a real ring. Packet: ``AB | length | command | params… | checksum`` (response preamble ``5A``;
the device also emits ``5B`` for unsolicited pushes). Integers in payloads are BIG-endian.
"""
from __future__ import annotations

import datetime
from typing import Optional

REQUEST_PREAMBLE = 0xAB
RESPONSE_PREAMBLES = (0x5A, 0x5B)  # 0x5A = reply to our request; 0x5B = device push
HEADER_LEN = 4  # preamble + length + command + checksum

# Command IDs (Gadgetbridge LefunConstants)
CMD_DEVICE_INFO = 0x00
CMD_BATTERY = 0x03
CMD_TIME = 0x04
CMD_FIND_DEVICE = 0x09   # buzz the ring
CMD_FIND_PHONE = 0x0A    # device->host "find my phone" (the unsolicited push we see)
CMD_HR_START = 0x0F      # start a PPG/heart-rate measurement
CMD_HR_RESULT = 0x10
CMD_STEPS = 0x12         # takes a 1-byte "days ago" param (0 = today)
CMD_ACTIVITY = 0x13
CMD_SLEEP = 0x15

OP_GET = 0
OP_SET = 1


def checksum(data: bytes) -> int:
    """Lefun bit-wise checksum (ported verbatim from Gadgetbridge BaseCommand)."""
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
    """Assemble a request frame: AB | len | cmd | params… | checksum."""
    body = bytes([REQUEST_PREAMBLE, HEADER_LEN + len(params), cmd]) + params
    return body + bytes([checksum(body)])


def parse_packet(pkt: bytes) -> Optional[tuple[int, bytes]]:
    """Return (command, params) for a valid response frame, else None."""
    if len(pkt) < HEADER_LEN or pkt[0] not in RESPONSE_PREAMBLES:
        return None
    length = pkt[1] if pkt[1] == len(pkt) else len(pkt)  # some pushes send len=0
    return pkt[2], pkt[3:length - 1]


def time_payload(when: Optional[datetime.datetime] = None) -> bytes:
    """SET-time params: op=1, year(2000-based), month, day, hour, minute, second."""
    t = when or datetime.datetime.now()
    return bytes([OP_SET, t.year % 100, t.month, t.day, t.hour, t.minute, t.second])


def parse_battery(params: bytes) -> Optional[int]:
    return params[0] if params else None


def parse_hr(params: bytes) -> Optional[int]:
    """HR-start/result payload; the BPM is the first nonzero byte (0 = still measuring)."""
    if not params:
        return None
    return params[0] or (params[-1] or None)


def parse_steps(params: bytes) -> Optional[dict]:
    """Steps frame: daysAgo|year|month|day|steps(BE32)|distance_m(BE32)|calories(BE32).

    ``year == 0xFF`` marks a day with no recorded data.
    """
    if len(params) < 16:
        return None
    year = params[1]
    date = None if year == 0xFF else f"20{year:02d}-{params[2]:02d}-{params[3]:02d}"
    return {
        "date": date,
        "steps": int.from_bytes(params[4:8], "big"),
        "distance_m": int.from_bytes(params[8:12], "big"),
        "calories": int.from_bytes(params[12:16], "big"),
    }
