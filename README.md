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
3. **Refinement.** The pairwise step assumes every radio converts RSSI to
   distance identically. They do not — antenna gain, shielding and enclosure
   differ per board, so -70 dBm on one radio is not the same distance as -70 dBm
   on another. So the layout is then relaxed against every individual reading as
   a force-directed problem: each reading is a spring between a radio and a
   beacon whose rest length is the distance it implies, solved by stress
   majorization (SMACOF), with each radio's gain offset solved alongside the
   positions. Gains are held to zero mean, since a constant added to all of them
   is indistinguishable from scaling every distance.
4. **Orientation.** MDS returns an arbitrary rotation, so the result is rotated
   to put the axis separating your Home Assistant floors vertical.

Refinement only replaces the pairwise layout if it actually fits the data
better; otherwise the pairwise result stands.

### Radios are not assumed to be fixed

Nothing is persisted. Every solve re-reads the live scanner list, so a radio that
goes away drops out and a new one is picked up with no configuration. A radio
that is moved corrects itself: the smoothed RSSI decays the old position's
influence to about 5% within a minute. Beacons nobody has heard for ten minutes
are forgotten.

Solid lines are direct radio-to-radio links; dashed lines are inferred. **Fit
error** is Kruskal stress — the mismatch between the estimated distances and the
layout drawn. Under ~10% is a usable shape.

**Caveat on the gain figures.** The synthetic tests show the solver recovers a
radio's offset cleanly when the offset is the only thing making it read
differently. In a real house it is not: a radio behind a wall hears everything
weakly, and the solver has limited ability to tell "quiet radio" from "radio with
things in the way". Read the gain column as a correction that measurably improves
the fit, not as a calibrated antenna measurement. On the house this was built
against the spread came out at about 11 dB, which at n=2.5 is a factor of 2.7 in
implied distance — far too large to leave uncorrected either way.

**Known limitation: the vertical axis is stretched.** A floor between two
radios costs signal that the path-loss model books as distance, so cross-floor
pairs read further apart than they are. On the two-storey house this was built
against, storeys about 3 m apart come out roughly 10-13 m apart. The floors
separate in the right order and same-floor distances are plausible, but do not
read the vertical scale as metres.

This is not fixable from signal alone. Fitting a single cross-floor penalty by
minimising stress was tried and does not work: with five anchors, 3D MDS has
enough freedom to fit the inflated distances just as well, so stress is
insensitive to the penalty and the fit returns zero. Correcting it needs ground
truth — a few anchor positions entered by hand — which is the next milestone.

**What this is not.** RSSI-derived distance is metres-accurate at best, and
walls, floors and furniture all bias it. The origin and the absolute orientation
around the vertical axis are arbitrary: the *shape* is the output, not the
coordinates. Treat it as a starting point to correct by hand, not a survey.

Against synthetic ground truth (5 anchors, two storeys, 120 beacons, 2 dB noise)
the recovered shape had ~0.9 m RMS error across a 9 m house. With the radios
deliberately miscalibrated by -6 to +5 dB, the solver recovers each radio's
offset to 0.58 dB mean error and holds shape error to 1.09 m; ignoring
calibration instead gives 3.07 m.

Run the checks with `python3 tests/test_geometry.py`.

## Requirements

- Home Assistant 2025.2.0 or newer
- The `bluetooth` integration set up with at least one adapter or proxy

## Roadmap

- Per-device RSSI history across adapters
- Anchor positions and a solved 3D fix
- The 3D view itself

## Licence

MIT

## Developing against a live Home Assistant

Frontend and backend iterate very differently, and knowing which is which saves
a lot of waiting.

**Frontend (`frontend/*.js`) needs no restart.** The files are read from disk per
request and served with `cache_headers=False`, so a real browser reload picks up
a change. Note that clicking through Home Assistant's sidebar is client-side
routing and does *not* re-fetch modules — press F5 (or Ctrl/Cmd-Shift-R) to force
an actual document load.

**Backend (`*.py`) needs a restart.** Python caches modules in `sys.modules`, and
reloading the config entry re-runs setup without re-importing. There is no
official hot-reload for custom integrations.

To make deploys a single command, clone the repo somewhere persistent on the HA
host and symlink it into place, so `git pull` *is* the deploy:

```bash
git clone https://github.com/jakeasmith/3d-ble-map /config/3d-ble-map
ln -s /config/3d-ble-map/custom_components/threed_ble_map \
      /config/custom_components/threed_ble_map
```

Then from the Terminal add-on: `git -C /config/3d-ble-map pull`, and reload the
browser. Restart Home Assistant only when Python changed.
