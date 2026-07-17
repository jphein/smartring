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
CMD_PROFILE = 0x06       # user profile (gender/height/weight/age) — drives distance/calories
CMD_FIND_DEVICE = 0x09   # flash the ring's LED (green) to locate it — no vibration motor
CMD_FIND_PHONE = 0x0A    # device->host "find my phone" push (shake gesture, un-armed)
CMD_REMOTE_CAMERA = 0x0D          # host->device: arm/disarm camera ("shake for selfie") mode
CMD_REMOTE_CAMERA_TRIGGERED = 0x0E  # device->host: camera-shutter shake push (while armed)

GENDER_FEMALE = 0
GENDER_MALE = 1
CMD_PPG_START = 0x0F     # start a PPG measurement; param = ppgType bitmask (1 << type)
CMD_PPG_RESULT = 0x10    # result frame: <ppgType><data...> (arrives ~15-30s after start)
CMD_HR_START = CMD_PPG_START  # back-compat aliases
CMD_HR_RESULT = CMD_PPG_RESULT
CMD_STEPS = 0x12         # takes a 1-byte "days ago" param (0 = today)
CMD_ACTIVITY = 0x13
CMD_SLEEP = 0x15

# PPG measurement types (Gadgetbridge LefunConstants.PPG_TYPE_*); request param = 1 << type.
PPG_TYPE_HEART_RATE = 0
PPG_TYPE_BLOOD_OXYGEN = 1
PPG_TYPE_BLOOD_PRESSURE = 2

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


def profile_payload(gender: int, height_cm: int, weight_kg: int, age: int) -> bytes:
    """0x06 SET params: op=1, gender(0=F/1=M), height(cm), weight(kg), age(yr)."""
    return bytes([OP_SET, gender & 1, height_cm & 0xFF, weight_kg & 0xFF, age & 0xFF])


def camera_mode_payload(enabled: bool) -> bytes:
    """0x0D param: 1 = arm 'shake for selfie' mode, 0 = disarm."""
    return bytes([1 if enabled else 0])


def parse_battery(params: bytes) -> Optional[int]:
    return params[0] if params else None


def ppg_start_payload(ppg_type: int = PPG_TYPE_HEART_RATE) -> bytes:
    """0x0F request param: the ppgType BITMASK (1 << type). Without it the ring returns
    a start-ack with success=0 and never measures — that was the old 'always 77' bug."""
    return bytes([1 << ppg_type])


def parse_ppg_result(params: bytes) -> Optional[dict]:
    """Parse a 0x10 CMD_PPG_RESULT frame: <ppgType><data...>. HR/SpO2 data is 1 byte,
    blood-pressure 2. Returns {type_bit, value} where value is the reading (BPM / SpO2 %).

    NB: the 0x0F response is only a start-ack (<ppgType><success>), NOT a reading — parse
    the 0x10 result instead."""
    if len(params) < 2:
        return None
    return {"type_bit": params[0], "value": params[1],
            "extra": params[2] if len(params) > 2 else None}


def parse_hr(params: bytes) -> Optional[int]:
    """Heart-rate BPM from a 0x10 result frame (<ppgType><bpm>). None if still measuring/empty."""
    r = parse_ppg_result(params)
    return (r["value"] or None) if r else None


def parse_device_info(params: bytes) -> Optional[dict]:
    """0x00 firmware info: supportCode(2 LE) | devTypeReserve(2 BE) | typeCode(4 ASCII)
    | hwVer(2 BE) | swVer(2 BE) | vendorCode(4 ASCII)."""
    if len(params) < 16:
        return None

    def ver(v: int) -> str:
        return f"{v >> 8}.{v & 0xFF}"

    def txt(b: bytes) -> str:
        return "".join(ch for ch in b.decode("ascii", "replace") if ch.isprintable()).strip()

    return {
        "type_code": txt(params[4:8]),
        "hardware_version": ver(int.from_bytes(params[8:10], "big")),
        "software_version": ver(int.from_bytes(params[10:12], "big")),
        "vendor_code": txt(params[12:16]),
    }


# Sleep (0x15): each frame is one segment = a start-timestamp + type; the ring streams
# totalRecords of them per day. Duration of a segment = gap to the next segment.
SLEEP_AWAKE, SLEEP_LIGHT, SLEEP_DEEP = 1, 2, 3


def parse_sleep_record(params: bytes) -> Optional[dict]:
    """One 0x15 sleep segment: daysAgo|totalRecords(BE16)|currentRecord(BE16)|Y|M|D|h|m|type."""
    if len(params) < 11:
        return None
    return {
        "days_ago": params[0],
        "total_records": int.from_bytes(params[1:3], "big"),
        "year": params[5], "month": params[6], "day": params[7],
        "hour": params[8], "minute": params[9], "sleep_type": params[10],
    }


def summarize_sleep(frames: list[bytes]) -> Optional[dict]:
    """Sum a night's 0x15 segments into total/deep/light/awake minutes (Gadgetbridge's
    approach: attribute each inter-record gap to the earlier segment's type)."""
    import datetime
    recs = [r for f in frames
            if (r := parse_sleep_record(f)) and r["total_records"] and r["year"] != 0xFF]
    if len(recs) < 2:
        return None

    def dt(r):
        return datetime.datetime(2000 + r["year"], r["month"], r["day"], r["hour"], r["minute"])

    recs.sort(key=dt)
    mins = {SLEEP_AWAKE: 0, SLEEP_LIGHT: 0, SLEEP_DEEP: 0}
    for a, b in zip(recs, recs[1:]):
        mins[a["sleep_type"]] = mins.get(a["sleep_type"], 0) + max(int((dt(b) - dt(a)).total_seconds() // 60), 0)
    r0 = recs[0]
    return {"total_min": mins[SLEEP_LIGHT] + mins[SLEEP_DEEP],
            "deep_min": mins[SLEEP_DEEP], "light_min": mins[SLEEP_LIGHT],
            "awake_min": mins[SLEEP_AWAKE],
            "date": f"20{r0['year']:02d}-{r0['month']:02d}-{r0['day']:02d}"}


def parse_steps(params: bytes) -> Optional[dict]:
    """0x12 CMD_STEPS_DATA: daysAgo|year|month|day|steps(BE32)|distance_m(BE32)|calories(BE32).

    This is a *finalized daily summary* (BE32). Gadgetbridge NEVER polls it for a running
    total — it only consumes 0x12 as an unsolicited live push (broadcastSample), and reads
    stored/today step data from the 0x13 activity buckets instead (see ``parse_activity`` /
    ``sum_activity``). A polled 0x12 daysAgo=0 reads 0 until the day's summary is finalized,
    so use it only for finalized PAST days (daysAgo>=1) or as a date/zero fallback.

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


def parse_activity(params: bytes) -> Optional[dict]:
    """One 0x13 CMD_ACTIVITY_DATA intraday bucket (14 params, BIG-endian):

        daysAgo | totalRecords | currentRecord | year | month | day | hour | minute
        | steps(BE16) | distance_m(BE16) | calories(BE16)

    The ring streams ``totalRecords`` of these per requested day (currentRecord = 1..N).
    ``totalRecords == 0`` means the day has no recorded activity. Ported from Gadgetbridge
    ``GetActivityDataCommand.deserializeParams``.
    """
    if len(params) < 14:
        return None
    year = params[3]
    date = None if year == 0xFF else f"20{year:02d}-{params[4]:02d}-{params[5]:02d}"
    return {
        "days_ago": params[0],
        "total_records": params[1],
        "current_record": params[2],
        "date": date,
        "time": f"{params[6]:02d}:{params[7]:02d}",
        "steps": int.from_bytes(params[8:10], "big"),
        "distance_m": int.from_bytes(params[10:12], "big"),
        "calories": int.from_bytes(params[12:14], "big"),
    }


def sum_activity(frames: list[bytes]) -> Optional[dict]:
    """Sum a day's 0x13 activity buckets into a daily total — this is how Gadgetbridge
    derives "today's steps" (``handleActivityData`` stores per-timestamp samples that the
    UI sums). ``frames`` is the list of 0x13 response *param* payloads collected for one day.

    Skips empty-day frames (``totalRecords == 0``). Returns ``None`` if the day has no real
    buckets, else ``{steps, distance_m, calories, date, buckets}``.
    """
    steps = distance = calories = 0
    date = None
    n = 0
    for params in frames:
        bucket = parse_activity(params)
        if bucket is None or bucket["total_records"] == 0:
            continue
        steps += bucket["steps"]
        distance += bucket["distance_m"]
        calories += bucket["calories"]
        date = date or bucket["date"]
        n += 1
    if n == 0:
        return None
    return {"steps": steps, "distance_m": distance, "calories": calories,
            "date": date, "buckets": n}
