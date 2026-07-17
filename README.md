# lefun-ring

A small Python client for a **Lefun-protocol BLE smart ring** (the cheap rings that
pair with the *Lefun Health / Life* app). It talks the ring's native GATT protocol
over Bluetooth LE — no cloud, no vendor app — and is built to **drop into Home
Assistant's Bluetooth stack** (works through a local adapter *or* an ESPHome BLE
proxy, transparently).

## Hardware findings (this ring: `FF:2A:35:A7:44:F3`)

Probed live from Linux/BlueZ:

- **SoC:** Nordic nRF-class (exposes the genuine Nordic Secure DFU service `0xFE59`
  with control-point `8ec90001` / packet `8ec90002`). Likely a **PhyPlus** nRF-clone,
  which is what the Gadgetbridge Lefun family uses.
- **OTA:** DFU control point is live — a read-only `Select` returns `SUCCESS`. Custom
  firmware *transport* is reachable via `nrfutil`/`adafruit-nrfutil`; whether it accepts
  unsigned images is untested (that step risks the app on a single-bank device).
- **Data protocol:** Gadgetbridge **Lefun** — service `0x18D0`, write `0x2D01`,
  notify `0x2D00`, request preamble `0xAB`, response `0x5A`, bit-wise checksum.
- **Verified reads:** device info (model `TJDP`), battery, live heart rate.
- **Note:** DIS strings (`0x180A`) return `NotPermitted`; get identity via cmd `0x00`
  instead. The ring gates GATT behind **bonding** and power-saves aggressively.

## Install

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The ring must be **bonded** first (one time):

```
bluetoothctl --timeout 15 scan on        # catch it advertising
bluetoothctl pair FF:2A:35:A7:44:F3
bluetoothctl trust FF:2A:35:A7:44:F3
```

## CLI usage

```
# read device info + battery + steps
.venv/bin/python lefun_ring.py --address FF:2A:35:A7:44:F3 poll

# trigger a live heart-rate measurement
.venv/bin/python lefun_ring.py --address FF:2A:35:A7:44:F3 hr

# send any raw command id and dump the response frames (for RE)
.venv/bin/python lefun_ring.py --address FF:2A:35:A7:44:F3 -v raw 0x12
```

On this BlueZ stack the ring's adverts are best caught by a `bluetoothctl` scan first;
the resolver reads BlueZ's cache (`get_device`) before falling back to its own scan.

## Home Assistant integration

`LefunRing` takes a `bleak.backends.device.BLEDevice` and connects with
`bleak_retry_connector.establish_connection` — the exact pattern HA integrations use,
so an ESPHome BLE proxy works with no code change:

```python
from homeassistant.components import bluetooth
from lefun_ring import LefunRing

ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
ring = LefunRing(ble_device)
state = await ring.async_poll()        # RingState(battery=…, heart_rate=…, …)
await ring.async_disconnect()
```

`async_poll()` / `async_heart_rate()` return a `RingState` dataclass; every frame is
also kept in `state.raw` for debugging. Wrap in a `DataUpdateCoordinator` for a real
integration.

## Protocol reference

Packet: `AB | length | command | params… | checksum` (response `5A | …`).
Checksum + command IDs ported from
[Gadgetbridge's Lefun driver](https://codeberg.org/Freeyourgadget/Gadgetbridge).

**Commands this ring answers** (verified live, `5A` response):
`0x00` firmware/device info · `0x03` battery · `0x0F` PPG/heart-rate start ·
`0x12` steps · `0x13` activity · `0x15` sleep. Steps/activity/sleep take a
one-byte day index (`0` = today), e.g. `raw 0x12 0x00`.

**Commands it ignores** (no response): `0x06` profile, `0x07` UI pages,
`0x08` features, `0x11` PPG data — so there is **no feature-bitmap or SpO₂
command** in this firmware's Lefun set. SpO₂/skin-temp, if the ring measures
them at all, would use an undocumented opcode discoverable only via an Android
btsnoop capture of the Lefun app.

**The unsolicited `5b 00 0a d2 04 0e 21 ef 27 11` push** (once/sec on `2D00`) is a
valid Lefun frame — preamble `0x5b` (device→host push variant), command
`0x0a = CMD_FIND_PHONE`. The ring is repeating its "find my phone" signal because
nothing acknowledges it. Not sensor data.

## Home Assistant integration

`custom_components/lefun_ring/` is a full HA custom component (modelled on the `moyoung`
integration): it runs the Lefun protocol through your **existing** ESPHome Bluetooth proxies
(no new/modified proxy needed) and exposes heart-rate, battery, steps, distance, calories, and
nearest-proxy **location** sensors, plus `set_time` / `find` / `measure_heart_rate` services.
Copy it into HA's `config/custom_components/`, restart, and add the "Lefun Smart Ring" integration.
A ready Lovelace dashboard is in `dashboard/lefun-dashboard.yaml`. See the component's README.

## Status

- [x] Bonded connect via `establish_connection` (+ `close_stale_connections`)
- [x] Device info, battery, live heart rate
- [x] Steps / activity / sleep parse (day-index param, big-endian; date verified live)
- [x] Set-time (`CMD_TIME`); decoded the `5b … 0a` broadcast → `CMD_FIND_PHONE`
- [x] HA custom component (coordinator + sensors + services) and dashboard
- [ ] Validate step *counts* accumulate (ring suspends its pedometer while actively polled)

## Known quirk

The ring is bonded **and trusted**, so BlueZ auto-reconnects it with no owner and
bleak then refuses ("Client is already connected"). `close_stale_connections()`
handles the common case; if the adapter wedges after many cycles, reset it with
`bluetoothctl power off; bluetoothctl power on`.
