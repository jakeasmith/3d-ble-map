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

## Requirements

- Home Assistant 2025.2.0 or newer
- The `bluetooth` integration set up with at least one adapter or proxy

## Roadmap

- Per-device RSSI history across adapters
- Anchor positions and a solved 3D fix
- The 3D view itself

## Licence

MIT
