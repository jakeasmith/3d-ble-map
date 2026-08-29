# 3D BLE Map

A Home Assistant custom integration that adds a sidebar panel for visualising
Bluetooth Low Energy devices in 3D, using RSSI from every BLE adapter and
ESPHome Bluetooth proxy that Home Assistant already talks to.

This is the first milestone: the panel lists the adapters and what each one is
currently hearing. Positioning comes later.

## Install

### HACS

1. HACS → three-dot menu → **Custom repositories**
2. Add `https://github.com/jakeasmith/3d-ble-map` with type **Integration**
3. Install **3D BLE Map**, then restart Home Assistant
4. **Settings → Devices & Services → Add Integration → 3D BLE Map**

### Manual

Copy `custom_components/threed_ble_map` into your Home Assistant `config/custom_components/`
directory and restart.

## What the panel shows

One row per Bluetooth scanner known to the `bluetooth` integration — local USB or
built-in adapters and remote ESPHome proxies alike:

| Column | Meaning |
| --- | --- |
| Adapter | Device registry name where one matches, otherwise the scanner name |
| Source | The scanner's own MAC address, as the `bluetooth` integration reports it |
| Area | Home Assistant area assigned to the matching device |
| State | Whether the scanner is currently scanning |
| Mode | `connectable` proxies accept connections; the rest only relay advertisements |
| Devices seen | How many BLE devices this scanner currently has in view |
| Last detection | Seconds since this scanner last heard anything |

A scanner whose **Area** is blank has no matching device in the registry. ESPHome
proxies advertise on a MAC a few digits off their network MAC, so the lookup
misses on some hardware; assign the area on the device itself and it will fill in
once the two match.

### Signals

Below the adapters, the top 20 BLE addresses ranked by how many radios currently
hear them, because that count is what decides whether a device can be placed at
all: three radios are enough for a 2D fix, four with vertical separation for 3D.
The **Radios** badge turns green at three.

| Column | Meaning |
| --- | --- |
| Name | Advertised local name, or `unnamed` — most tags advertise no name |
| Address | The device's BLE address |
| Radios | How many scanners currently hear it |
| Best RSSI | Strongest single reading across all scanners, in dBm |
| Heard by | Each scanner hearing it, strongest first, with its RSSI |

Both tables refresh every 5 seconds. The panel bundle is served with a
cache-busting token derived from its modification time, so an update takes
effect on the next page load rather than needing a hard reload.

## Anchor map

The second tab estimates where each radio sits in 3D, relative to the others.
Nothing in Home Assistant records anchor coordinates, so this derives them.

How it works:

1. **Distances between anchors.** Anchors that advertise are heard directly by
   their neighbours; that RSSI, averaged over both directions, goes through a
   log-distance path-loss model. Pairs with no direct link fall back to how
   similarly they hear the beacons they share, scaled against the pairs that do
   have one.
2. **Embedding.** Classical multidimensional scaling places the anchors so their
   separations match those distances as closely as possible.
3. **Orientation.** MDS returns an arbitrary rotation, so the result is rotated
   to put the axis separating your Home Assistant floors vertical.

Solid lines are direct radio-to-radio links; dashed lines are inferred. **Fit
error** is Kruskal stress — the mismatch between the estimated distances and the
layout drawn. Under ~10% is a usable shape.

**What this is not.** RSSI-derived distance is metres-accurate at best, and
walls, floors and furniture all bias it. The origin and the absolute orientation
around the vertical axis are arbitrary: the *shape* is the output, not the
coordinates. Treat it as a starting point to correct by hand, not a survey.

Against synthetic ground truth (5 anchors, two storeys, 120 beacons, 2 dB noise)
the recovered shape had ~0.9 m RMS error across a 9 m house.

## Requirements

- Home Assistant 2025.2.0 or newer
- The `bluetooth` integration set up with at least one adapter or proxy

## Roadmap

- Per-device RSSI history across adapters
- Anchor positions and a solved 3D fix
- The 3D view itself

## Licence

MIT
