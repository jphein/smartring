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
# A short tick keeps room tracking responsive (recomputed from the advert cache each tick, no
# connection). Vitals need a connection, so they're polled only every POLL_EVERY ticks.
# 15s tick x POLL_EVERY 40 -> vitals ~every 10 min, but the room follows within ~15s (or faster
# when the real-time advert callback fires — i.e. when the ring is worn near a proxy).
UPDATE_INTERVAL_SECONDS = 15
POLL_EVERY = 40  # -> battery/steps/HR ~every 10 min at a 15s tick

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
SERVICE_SYNC = "sync"

# Ring-as-button: the ring fires HA events on shake gestures. Camera (0x0E, while armed via
# 0x0D) is a clean discrete push; find-phone (0x0A) is edge-detected out of the ring's constant
# heartbeat stream. Requires holding a connection (keepalive), which uses a proxy slot + battery.
#
# KEEPALIVE is OFF: investigation (2026-07-17) showed this ring emits NO motion data — steps stay
# 0 after real walking and camera-shake (0x0E) never fires across minutes of vigorous shaking, so
# there is no working accelerometer over its firmware and the shake gestures can't work. Holding a
# connection therefore buys nothing; keeping it OFF lets the ring ADVERTISE, which is what the
# nearest-proxy room tracking (follow-me lighting) needs. Vitals still read on-demand.
EVENT_BUTTON = f"{DOMAIN}_button"
KEEPALIVE = False           # advertise for room tracking; no working IMU so shake pushes are moot
FINDPHONE_GAP = 4.0         # secs of 0x0A silence before a new one counts as a fresh press
CONNECT_GRACE = 12.0        # ignore find-phone edges in the first N secs after connecting

# Room tracking hysteresis: only switch the reported room to a different proxy when it is at least
# this many dBm stronger than the currently-held proxy, so the ring doesn't flip-flop (and flicker
# the lights) between two adjacent rooms with similar signal.
ROOM_SWITCH_MARGIN = 8

# Freshness window: a proxy only counts toward "nearest room" if IT has heard the ring within this
# many seconds (per-scanner discovered_device_timestamps). This is what makes follow-me responsive:
# when you leave a room, that proxy goes stale and CANNOT hold the room — without this, its last
# (strong) reading lingers in HA's advert cache for ~3 min and the room sticks. 30s ≈ a couple of
# advert intervals (the ring advertises ~1/min idle, faster when worn/moving); if no proxy is fresh
# the last room is held rather than flapping.
FRESH_WINDOW = 30.0

# Nearest-room scoring: score = rssi - RSSI_AGE_PENALTY * age_seconds. The ring's adverts are
# caught only ~once a minute per proxy (weak, body-shadowed signal), so recency must weigh as
# much as strength — a proxy that heard the ring JUST NOW should beat one that heard it a bit
# louder 20s ago (that's the proxy of the room you just left). 1 dB/s ≈ a 10s-fresher catch
# outweighs a 10 dBm-louder stale one.
RSSI_AGE_PENALTY = 1.0
