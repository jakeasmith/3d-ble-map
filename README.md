# 3D BLE Map

A Home Assistant custom integration that adds a sidebar panel for visualising
Bluetooth Low Energy devices in 3D, using RSSI from every BLE adapter and
ESPHome Bluetooth proxy that Home Assistant already talks to.

The panel lists every adapter and what it hears, then solves a relative 3D
layout: where the radios sit with respect to each other, and where the beacons
they hear sit among them. No coordinates are entered anywhere -- the geometry is
derived from RSSI alone, so it works in a house nobody has surveyed.

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
4. **Orientation, before the refinement rather than after.** MDS returns an
   arbitrary rotation, so the layout is first rotated to put the axis separating
   your Home Assistant floors vertical. This has to happen *before* step 3,
   because step 3 constrains that axis against how tall a storey is, which is
   only meaningful once it points up.
5. **Architectural bounds on height.** RSSI says very little that is trustworthy
   about the vertical axis, so two facts about how houses are built are imposed
   on it after every SMACOF sweep: radios on one storey sit within a ceiling's
   height (2.4 m) of each other, and adjacent storeys sit a floor-to-floor pitch
   apart (2.9 m, plus or minus 0.4). Beacons are held inside the same building
   envelope. See *The vertical axis* below.

Refinement only replaces the pairwise layout if it actually fits the data
better; otherwise the pairwise result stands.

### Beacons

Placing the beacons is not a second pass. The refinement step above has to put
every beacon somewhere in order to use it as a spring, so their positions fall
out of the same solve that positions the radios -- they were simply discarded
before. Any beacon heard by three or more radios is drawn on the map and listed
below it, strongest first.

Three is the fewest that can fix a point in 3D, and it is also a fit with no
redundancy: three ranges and three unknowns pass through the data exactly and
say nothing about whether the answer is right. So every beacon carries an
uncertainty radius, shown as a ring when you hover it and as a column in the
table. On a real house these run from about 2 m to well over 10 m -- frequently
wider than the house itself, which is the honest picture rather than a defect.
The figure is a *lower* bound: it assumes the radios surround the beacon, and
one sitting outside them is worse still.

Two things follow, and the panel is built around them:

- **Trust the nearest radio, not the coordinates.** That column comes from the
  strongest reading, a raw measurement that survives any amount of geometry
  error. "Which room" is reliable; "where in the room" is not.
- **Read the uncertainty before the position.** A beacon whose radius exceeds
  its distance to the nearest radio is located to "somewhere in the house", and
  the table marks it.

This is a property of RSSI, not of the solver. Range error from received signal
strength is a constant *fraction* of distance set by the shadowing in the
building, and no amount of extra radios, extra beacons or better mathematics
reduces it -- only a quieter radio environment or different hardware does.

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

### The vertical axis

A floor between two radios costs signal that the path-loss model books as
distance, so left alone the vertical axis inflates badly: on the house this was
built against, radios on a single storey solved 6.3 m apart *vertically* and the
two storeys 7.4 m apart, both around 2.5x anything a building can do.

Two bounds fix that, and both are generic American construction rather than
facts about any particular house, so they cost nothing in portability:

| bound | value | source |
| --- | --- | --- |
| Within one storey | 2.4 m | floor-to-ceiling, the 8 ft traditional US ceiling |
| Between adjacent storeys | 2.9 m +/- 0.4 | floor-to-floor: ceiling plus the floor assembly. Cross-checked against stair geometry, where a flight's total rise *is* the floor-to-floor height: 14-15 risers at 7-7.75 in (the IRC cap) gives 2.7-2.95 m |

They are applied as **dead-zone projections** after each sweep: they do nothing
until the layout leaves the range a building could occupy, then move it only to
the nearest edge. Nothing is asserted beyond "that answer is impossible".

The horizontal spread a link needs is recovered on its own by the next sweep,
since a spring whose rest length is unchanged but whose vertical component just
shrank must grow horizontally. Projecting between majorization steps this way is
standard constrained SMACOF and keeps the monotone convergence guarantee;
redistributing the excess by hand would not.

On the two live captures this took the within-floor spread from 6.31 m to under
0.1 m and the storey gap from 7.35 m to 2.50 m.

**What it costs:** genuine height differences within a storey are flattened to a
few centimetres. A radio on a shelf and one on the floor come out level. That
detail was never recoverable from RSSI -- it sits far below the noise -- and
giving it up buys a materially better horizontal layout.

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

- A room-level presence entity per beacon, which is the part of this that is
  accurate enough to drive automations
- Hand-entered coordinates for two or three radios, to pin the map to real
  units and orientation instead of a relative frame
- Ordinal (non-metric) MDS, which is invariant to per-radio calibration by
  construction rather than solving it out

## Licence

MIT

## Developing against a live Home Assistant

Frontend and backend iterate very differently, and knowing which is which saves
a lot of waiting.

**Frontend (`frontend/*.js`) needs no restart.** The files are read from disk per
request, so copying a new one over is the whole deploy. The browser is the part
that needs convincing: the panel's module URL carries a cache-busting token
fixed at registration, so it does not change when you edit a file. Clicking
through the sidebar is client-side routing and re-uses the cached module; a plain
reload usually does too. Use **Ctrl/Cmd-Shift-R** to bypass the cache.

**Backend (`*.py`) needs a restart.** Python caches modules in `sys.modules`, and
reloading the config entry re-runs setup without re-importing. There is no
official hot-reload for custom integrations.

Keep a clone on the Home Assistant host and deploy with a pull and a copy:

```bash
git clone https://github.com/jakeasmith/3d-ble-map /config/3d-ble-map

# thereafter, from the Terminal add-on:
git -C /config/3d-ble-map pull && \
  cp -r /config/3d-ble-map/custom_components/threed_ble_map /config/custom_components/
```

**Do not symlink the integration into `custom_components/`.** It looks like it
should work and it breaks the panel: Home Assistant serves the frontend through
aiohttp's static handler, which does not follow a symlink out of the directory it
registered. Every asset 404s, the custom element never defines, and the page
hangs on a blank panel with no obvious cause. Copy the directory.

## Validating against reality

`validation/` scores the solver against approximate anchor positions read off a
floor plan. **That floor plan is a yardstick, never an input.** The integration
has to work in homes with no plan and no surveyed positions, so measured
coordinates must not leak into the solver — they would make it accurate in one
house and useless everywhere else.

```bash
# capture the recorder's raw view from a running Home Assistant
ha ws threed_ble_map/raw_observations '{}' > raw.json
python3 validation/score.py raw.json
```

The layout is relative, so it is Procrustes-aligned onto the truth before
scoring. Scale is reported but deliberately not fitted away — getting the scale
right is part of the job.

Room centres are only good to about +/- 1.5 m, since the hardware sits on a wall
rather than mid-room. A result better than that is not measurably better.

### Beacons have no yardstick, so they are cross-validated instead

The floor plan locates the radios and nothing else, so it cannot say whether a
beacon is in the right place. `validation/beacon_cv.py` answers that without any
ground truth at all: hide one radio's reading of a beacon, fit the beacon from
the radios that remain, and see how well the hidden reading is predicted. A
position that generalises to a radio it was not fitted against means something.

```bash
python3 validation/beacon_cv.py raw.json
```

It is scored against the answer available for free -- the beacon parked at the
centroid of the radios hearing it. On this house the solve beats that null model
by 33-47%, so the positions do carry information. The held-out error is still
around 8 dB, which by the same bound the rest of this integration is built
around is a range error of roughly 70% of the distance. Both things are true at
once: the dots are real, and they are coarse.

Because it needs no ground truth, this one runs in any house -- which matters,
since the shipped integration has no floor plan either.
