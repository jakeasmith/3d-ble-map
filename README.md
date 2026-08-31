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

6. **Weighting each observation by what it is worth.** A reading's pull is
   scaled by 1/d^2, which makes minimising the sum equivalent to minimising
   squared dB error -- the maximum-likelihood objective, since shadowing is
   Gaussian in dB and so a fixed dB error is a *proportional* distance error.
   That is only the right weight if every observation shares one sigma and each
   is independent, and radio-to-radio links are neither. See *What an
   observation is worth* below.
7. **Starting from last time.** The previous layout is offered back as one more
   starting point, and ties go to not moving. See *Standing still* below.

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

### Scale, and the parameters a fit spends

Inverting the path-loss law on a noisy reading does not give the distance, it
gives a log-normal whose *mean* sits above the true distance by
exp(a^2 sigma^2 / 2). At the 8-ish dB a real house produces that is a 30%
inflation of the entire map, so the sigma has to be estimated and divided out.

The trap is that sigma is not the fit residual, and using the residual is how
this map came to solve 1.74x too large while the correction meant to prevent
that was running and reporting success. A fit spends its parameters absorbing
exactly the deviations it is being asked to measure: every beacon carries three
free coordinates, so a beacon heard by four radios moves to soak up most of its
own shadowing and reports almost none of it back.

Count them. One real capture fits 171 parameters -- three coordinates for each
radio and beacon, less the six rigid motions no data can pin, plus one gain per
radio less the one removed by holding gains to zero mean -- against 308
observations. That leaves 137 degrees of freedom, and the residual understates
sigma by sqrt(308/137) = 1.5x. The textbook estimator sigma^2 = RSS/(n - p) is
the fix. Averaged over 8 perturbed inputs so no single lucky solve decides it:

| | scale | position RMS |
| --- | --- | --- |
| residual as sigma | 1.69x | 4.31 m |
| dof-corrected | 1.37x | 3.06 m |

This is worth internalising before adding anything to the model, because a real
house's fit is *parameter-starved* and most additions make it worse. See *Fits
better, locates worse* below.

### What an observation is worth

Direct radio-to-radio links were carrying 5.5% of the pull that reaches the
radios: 18 measurements against 290 beacon readings, and further penalised by
1/d^2 for spanning the whole house. They are also the only measurement in the
system whose *both* endpoints are fixed, mains-powered and of known storey.

Two measured reasons a link is worth more, multiplied, both computed from each
fit rather than fixed in the source:

| factor | this house | why |
| --- | --- | --- |
| scatter | 1.7 | A link has no unknown transmit power and is averaged over both directions. Measured, it scatters 5.8 dB against 8.5 dB for a beacon reading; GLS weights by 1/sigma^2 |
| redundancy | 2.1 | A beacon carries three unknown coordinates, so of the k readings it contributes, three are spent pinning the beacon itself and only k - 3 constrain the radios. At k = 5.8 that is 2.8 of 5.8. A link spends nothing |

They derive 3.5 here; an empirical sweep independently liked 3.0, which is the
agreement worth having, since a swept constant would have been tuned to one
house. A house with more radios per beacon derives a smaller number.

| | scale | position RMS |
| --- | --- | --- |
| 1/d^2 only | 1.37x | 3.06 m |
| per-class weights | 1.38x | 2.43 m |

The scatter term alone derives 1.7 and buys nothing. The redundancy term is
where the win is -- the same effect the dof correction addresses one layer up.

### Weak readings are biased, not just noisy

A receiver has a sensitivity limit and does not hear packets below it. So the
weak readings that do arrive are not a fair sample of what was transmitted --
they are the ones a favourable fade lifted over the bar. They read stronger than
the truth, and stronger reads as closer. On the house this was built against,
**38% of all readings sit below -95 dBm**, inside that region.

The tell is in the histogram. Binned by distance, the residual scatter does not
grow with range, it *shrinks* -- 9.3 dB in the nearest band against 5.3 dB in the
farthest. Distant readings are not more precise; their spread is clipped by the
receiver that could not hear the other half of it.

| distance | mean RSSI | sigma |
| --- | --- | --- |
| 0.5 - 6.4 m | -72.6 dBm | 9.28 dB |
| 6.4 - 10.3 m | -84.9 dBm | 5.64 dB |
| 10.3 - 14.4 m | -90.9 dBm | 5.99 dB |
| 14.4 - 18.0 m | -93.3 dBm | 5.47 dB |
| 18.0 - 22.6 m | -96.3 dBm | 5.37 dB |
| 22.7 - 44.8 m | -98.4 dBm | 5.25 dB |

This matters because it is a **bias**, and the 1/d^2 term corrects variance. That
term is right and was never the problem; no weighting scheme fixes a systematic
offset.

Each radio's limit is read from its own RSSI histogram rather than fixed in the
source, because it is a property of the receiver: measured at -94 to -98 dBm
across eight radios of three different kinds, tightly clustered despite entirely
different placements. Readings below it are weighted at `CENSORED_TRUST`.

| trust | real mean | worst | synthetic mean |
| --- | --- | --- | --- |
| 1.0 | 3.91 m | 5.15 m | 1.97 m |
| **0.5** | **3.47 m** | **4.07 m** | **1.82 m** |
| 0.25 | 3.11 m | 4.03 m | 2.34 m |
| 0.1 | 3.12 m | 4.36 m | 2.98 m |

0.5 improves both, with a tighter spread and a better worst case in each. 0.25 is
tempting and wrong: better on the real house, clearly worse on synthetic, which
models no receiver floor at all -- there the histogram mode lands mid
distribution and the rule downweights 44% of perfectly good readings. A constant
that only helps where the effect it assumes is present is a constant tuned to one
house.

They are downweighted rather than dropped. Cutting them out of the input takes
beacons below `MIN_RADIOS_PER_BEACON` and starves a solver already short of
constraint: over 24 runs the median improved to 2.59 m while the worst case went
from 5.15 m to **11.70 m**. Same lesson as *Fits better, locates worse* above.

### The radios are calibrated, not re-solved

A solve is evidence, not the answer. Radios are infrastructure -- they sit on
shelves and do not move between one solve and the next -- but the solver returns
a slightly different answer every time, and consecutive solves were moving the
map **2.02 m RMS with nothing in the house having changed**.

So a calibrated layout is kept and each new solve is blended into it at
`CALIBRATION_RATE`. Noise averages away; a radio that genuinely moved is followed
over minutes instead of instantly.

Two things happen in that order, and the first is easy to miss. The solver has no
preferred rotation about the vertical axis and no preferred handedness, so
consecutive solves come back arbitrarily spun and mirrored. **Averaging two
layouts in different frames is meaningless** -- it would shrink the map toward
its own centroid. Each candidate is therefore rotated onto the calibrated frame
first, and only then blended. The beacons are moved by the identical transform,
since they are solved in the same frame and would otherwise be left floating in
the previous solve's orientation.

Only yaw and handedness are free. The vertical axis is pinned by the storey
constraints and oriented before refinement, so it is already consistent between
solves. That keeps the alignment a closed form and avoids taking on an SVD.

Measured over 150 solves of one static capture, against the exact step response
of the same filter:

| rate | resting jitter | 63% of a real move | 95% |
| --- | --- | --- | --- |
| today | 2.023 m | immediate | immediate |
| 0.20 | 0.332 m | 1.8 min | 5.1 min |
| 0.10 | 0.163 m | 3.7 min | 10.6 min |
| **0.03** | **0.049 m** | **12.1 min** | **36.3 min** |
| 0.01 | 0.056 m | 36.3 min | 109.6 min |

0.03 is where the curve stops paying. Below it the jitter does not improve --
0.01 is slightly *worse*, because what remains is not per-solve noise but the
occasional solve landing in a different basin, which averaging cannot remove --
while the time to notice a real move triples.

The early solves are a running mean (1, 1/2, 1/3 ...) before the rate stiffens to
0.03, so a fresh install establishes a map in a few solves rather than creeping
toward one. A radio the layout has never seen is adopted outright; a radio
missing from one solve keeps its place, so a proxy dropping off Wi-Fi for a
minute does not erase it.

**What this does not fix.** The alignment is rigid, so when one radio genuinely
moves, the best fit onto the *old* layout explains part of that away as a global
shift and rotation. The shape converges exactly on the new truth; the frame it
settles in is its own. Since the origin and orientation of this map carry no
meaning anyway, that costs nothing -- but it does mean absolute coordinates are
the wrong thing to measure convergence with.

### Standing still

SMACOF is a local method, and the search used to start cold on every poll. Two
minima a hundredth of a dB apart are indistinguishable as fits but metres apart
as layouts, so a best-of-six pick was free to alternate and the house visibly
jumped between polls. Nudging the smoothed RSSI by a quarter of a dB -- less
than the recorder smooths away between polls -- moved pairwise distances 1.03 m
RMS, and the response was not monotone in the size of the nudge. That is
minimum-hopping, not sensitivity to the data.

The previous layout now competes as one more starting point, and ties go to not
moving. Over 10 consecutive solves of perturbed input:

| | movement between solves | position RMS |
| --- | --- | --- |
| cold every solve | 2.21 m | 2.74 m |
| warm, no margin | 2.41 m | 2.58 m |
| warm + 2% margin | 1.01 m | 2.33 m |

Warm-starting on its own buys nothing; the margin is the mechanism, because what
needed fixing was the choice between near-equal minima and not where the search
began. 2% is the smallest value on a plateau running unchanged to 20%.

There is deliberately no release condition. The cold multi-restart search still
runs in full every solve and takes over the moment it genuinely fits better, so
this cannot latch onto a stale layout when a radio actually moves.

### Fits better, locates worse

Two plausible additions were built and measured against the yardstick, and both
lowered the fit residual while moving the radios further from where they are.
Neither is in the code. A real house's fit has little redundancy to spare, so
new parameters get spent memorising shadowing:

| | fit residual | position RMS |
| --- | --- | --- |
| baseline | 7.6 dB | 2.61 m |
| per-beacon transmit power | 5.1 dB | 4.60 m |
| beacon height pinned to its storey | 7.7 dB | 3.60 m |

Per-beacon transmit power is the more tempting of the two, and the effect it
chases is real: -59 dBm at 1 m is a nominal, and the per-beacon offset measures
3.67 dB RMS across -7.7 to +5.7 dB, a 40% systematic range bias. It is
structurally identical to the per-radio gain that *is* solved, and identifiable
for 44 of this house's 50 placed beacons. It still loses, and it still loses
after the degrees-of-freedom count is corrected to include the new parameters.

The lesson generalises: on this problem, judge a change by where it puts the
radios, never by the residual. The residual is what the extra parameters eat.

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
the recovered shape had ~1.2 m RMS error across a 9 m house. With the radios
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

**Frontend (`frontend/*.js`) needs a restart too, despite appearances.** The
files are read from disk per request, so the copy really is the whole deploy on
the server side. The browser is the problem. The panel's module URL carries a
cache-busting token computed in `_module_version` when the panel is *registered*,
which happens once during setup -- so editing a bundle afterwards leaves the URL
byte-for-byte identical and the browser keeps serving the module it already has.
Ctrl/Cmd-Shift-R does not reliably shift it, and a config-entry reload does not
help either: it returns `require_restart: true` without re-registering the panel,
so the token stays put. Verified by watching `?v=` stay unchanged across both.

Restarting is what updates the token. Budget for it, or expect to debug a change
that is already on disk and simply is not being loaded.

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

### Not all beacons are worth the same

A mains-powered light fitting bolted to a ceiling is a far better landmark than
a tracker in someone's pocket, so every beacon carries a **trust** score in the
table and the API.

**It is reported, not applied, and that was a measurement rather than a
decision.** Multiplying each beacon's springs by its trust was built and tested,
and it makes the layout monotonically worse. Contaminating 15 of ~120 beacons so
that different radios saw them in different places -- what a smoothed average
holds while something is being carried around:

| spring weight on suspect beacons | shape error |
| --- | --- |
| 1.0 (trust them) | 2.75 m |
| 0.7 | 2.98 m |
| 0.4 | 4.02 m |
| 0.05 | 4.38 m |

At heavier contamination it did not help on a single seed. The reason is that
`_pull` already applies a Huber weight to each reading's own residual -- evidence
from the fit about whether a reading is *actually* consistent, which beats a
prior guess about whether it might be. A prior on top only removes constraint
mass the solver needs. `solve_layout` still accepts weights and the path is
tested, so this is one argument away if better evidence appears.

What the score is good for is reading the map: it tells you which dots are
landmarks and which are someone's headphones.

**The obvious signal is the wrong one.** RSSI *spread* looks like a mobility
detector and is not. Measured across a real house:

| | mean spread |
| --- | --- |
| Devices that move (headset, camera, portable speaker) | 2.7 dB |
| Devices that cannot (bulbs, LED controllers, TVs, a door lock) | 3.7 dB |

Backwards, and the noisiest "fixed" device was a mains-powered smart bulb at
7.1 dB. Spread correlates with *signal strength* (Spearman +0.53) and with how
many radios hear a beacon -- it was taken as the maximum across radios, and the
maximum of k samples grows with k. Gating on it rejected beacons for being well
observed, which is the opposite of what you want.

So mobility is measured directly instead:

| signal | what it is | why |
| --- | --- | --- |
| **motion** | RMS gap between a fast and a slow average of the same RSSI, meaned across radios | A real displacement holds the two averages apart; noise jitters the fast one around the slow one and cancels. Meaned rather than maxed, which *reduces* the radio-count bias but does not remove it -- see the caveat below |
| **persistence** | fraction of the recording the beacon was present for | The measurable half of "has a wired power supply". Every mains-powered fixture in the reference house was present for the whole window; transients sat at 3-5% |
| **identity** | known to Home Assistant, and whether the address rotates | A device in the registry is installed kit. Privacy-rotating addresses -- phones and Tile/Chipolo-style trackers -- churn identity and never build a baseline; 227 of 443 addresses were rotating and only 3% of those persisted |

Persistence means *always-on*, not *immobile* -- a headset and a camera were
both present for the entire window and both move. Motion is what catches those,
which is why it is the primary signal and the other two are priors for the
cold-start window before there is enough history to measure movement.

Identity uses Home Assistant's device registry, which ships with every install,
rather than a list of vendor name prefixes. A hardcoded "Govee/ELK-BLEDOM" list
would work in one house and nowhere else, which is the portability rule this
project is built around.

Two caveats, both measured:

- **Motion is still somewhat confounded with proximity.** Beacons heard by four
  or more radios average 2.08 dB of motion against 0.90 dB for single-radio
  ones. That is better than the spread metric it replaced (3.36 vs 0.98 dB) but
  it is not gone, because beacons heard by many radios are generally closer, and
  strong signals genuinely swing more in dB. Well-observed beacons therefore
  score a little worse than they deserve.
- **Neither signal means anything for the first few minutes.** The slow average
  has a time constant of about 165 seconds, so motion reads near zero until it
  has diverged, and persistence reads ~1.0 for everything until the recording is
  long enough for transients to drop out. Both need hours, not minutes.

Since the score is reported rather than applied, neither costs any accuracy.

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
