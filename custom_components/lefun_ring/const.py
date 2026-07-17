"""Constants for the Lefun Smart Ring integration."""
from __future__ import annotations

DOMAIN = "lefun_ring"

# Identity
DEFAULT_NAME = "Lefun Ring"
MODEL = "Lefun Smart Ring"
MANUFACTURER = "Lefun"

# Lefun command/data service (0x18D0) + the characteristics we use.
SERVICE_UUID = "000018d0-0000-1000-8000-00805f9b34fb"
CTRL_CHAR = "00002d01-0000-1000-8000-00805f9b34fb"    # write-no-resp: commands
NOTIFY_CHAR = "00002d00-0000-1000-8000-00805f9b34fb"  # notify: responses/pushes
# The ring gates the DIS strings (NotPermitted); identity comes from cmd 0x00 instead.

# Coordinator tick: location (which proxy hears the ring) is recomputed every tick from the
# Bluetooth advert cache (cheap, no connection). Battery/steps/HR need a BLE connection, so
# they are polled only every Nth tick to spare the ring's battery / a proxy connection slot.
# (Step counting is always-on in firmware and NOT suspended by a connection — the coordinator
# reads today's steps by summing the 0x13 activity buckets, since the 0x12 daily summary reads
# 0 until finalized; see coordinator._async_update_data and proto.commands.sum_activity.)
UPDATE_INTERVAL_SECONDS = 60
POLL_EVERY = 10  # -> battery/steps/HR ~every 10 min at a 60s tick

# Map a proxy/scanner name to a friendly room. Falls back to a heuristic on the name.
PROXY_ROOM_OVERRIDES: dict[str, str] = {}

# Services
SERVICE_SET_TIME = "set_time"
SERVICE_FIND = "find"
SERVICE_MEASURE_HR = "measure_heart_rate"
SERVICE_MEASURE_SPO2 = "measure_spo2"
SERVICE_MEASURE_BP = "measure_blood_pressure"
SERVICE_SET_PROFILE = "set_profile"
SERVICE_SET_CAMERA = "set_camera_mode"

# Ring-as-button: the ring fires HA events on shake gestures. Camera (0x0E, while armed via
# 0x0D) is a clean discrete push; find-phone (0x0A) is edge-detected out of the ring's constant
# heartbeat stream. Requires holding a connection (keepalive), which uses a proxy slot + battery.
EVENT_BUTTON = f"{DOMAIN}_button"
KEEPALIVE = True             # hold a persistent connection so shake pushes are caught
FINDPHONE_GAP = 4.0         # secs of 0x0A silence before a new one counts as a fresh press
CONNECT_GRACE = 12.0        # ignore find-phone edges in the first N secs after connecting
