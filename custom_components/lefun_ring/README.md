# Lefun Smart Ring — Home Assistant integration

A local, cloudless HA integration for a Lefun-protocol BLE smart ring, modelled on the
sibling `moyoung` component. It runs the Lefun protocol inside HA via `bleak`, routed through
your **existing ESPHome Bluetooth proxies** (active connections) — so the ring is reachable from
wherever a proxy is, and HAOS needs no local adapter.

No new or modified proxy is required: any standard ESPHome `bluetooth_proxy` you already run
(the default is active-capable) handles the ring, exactly like the `moyoung` integration. The
"Location" sensor just needs the proxies named per-room.

## Install

Copy `custom_components/lefun_ring/` into your HA `config/custom_components/`, restart HA, then
**Settings → Devices & Services → Add Integration → “Lefun Smart Ring”**. It auto-discovers a
ring advertising the `18d0` service (or named `Smart Ring`); you can also enter the MAC by hand
(needed when a proxy forwards the advert without service UUIDs).

## Entities

`sensor.<ring>_heart_rate`, `_battery`, `_steps` (+ `date` attr), `_distance`, `_calories`,
`_location` (nearest proxy → room; `proxies`/`rssi` attrs), `_signal` (RSSI).

## Services

- `lefun_ring.set_time` — sync the clock (the ring doesn't ACK, but it takes).
- `lefun_ring.find` — buzz the ring.
- `lefun_ring.measure_heart_rate` — on-demand HR measurement (~20s), updates the sensor.

## Notes

- Battery/steps/HR need a connection, so they're polled every ~10 min (`POLL_EVERY`); location
  is recomputed every tick from the advert cache (no connection).
- **Steps come from the `0x13` activity buckets (summed), not the `0x12` daily summary.** On the
  Lefun protocol `0x12` (daysAgo=0) is a *finalized* daily summary that reads 0 mid-day, so
  Gadgetbridge never polls it — it sums the `0x13` intraday buckets (and treats `0x12` only as a
  live push). The coordinator sets the clock on connect + settles ~1s before the multi-fetch,
  matching GB. Step counting is always-on firmware behavior; a connection does not suspend it.
- Protocol lives in `proto/commands.py` (stdlib-only), vendored from the repo's top-level
  `lefun_ring.py` CLI which validated it live. Service `0x18D0`, write `0x2D01`, notify `0x2D00`,
  request preamble `0xAB` / response `0x5A`, big-endian payload ints.
- No SpO₂/temperature: the firmware exposes no such command (see the top-level README).
