"""How much each beacon should be trusted as a fixed reference.

Beacons are not interchangeable. A mains-powered light fitting bolted to a
ceiling is a far better landmark than a tracker in someone's pocket, and the
solver has no business weighting them equally. This works out that weight.

The obvious signal is the wrong one. Per-link RSSI *spread* looks like a
mobility detector and is not: measured across a real house, devices that move
averaged 2.7 dB of spread while fixed ones averaged 3.7 dB, and the single
noisiest "fixed" device was a mains-powered smart bulb at 7.1 dB. Spread
correlates with signal strength (Spearman +0.53) and with how many radios hear
a beacon, because it was taken as the maximum across radios and the maximum of k
samples grows with k. Gating on it therefore rejected beacons for being *well
observed* -- exactly the ones worth keeping.

So mobility is measured directly instead, and the two cheap proxies are kept
only for the cold-start window before there is enough history to measure it:

  motion       The gap between a fast and a slow average of the same RSSI, taken
               across every radio hearing the beacon. A real displacement shows
               as a sustained offset between the two; noise jitters the fast
               average around the slow one and averages out. Taken as a mean
               over radios rather than a maximum, so it does not grow just
               because more radios can hear the beacon.
  persistence  What fraction of the recording the beacon was actually present
               for. This is the measurable half of "has a wired power supply":
               every mains-powered fixture in the reference house was present
               for the whole window, while transients sat at 3-5% of it.
  identity     Whether Home Assistant already knows the device, and whether the
               address is a privacy-rotating one.

Note what persistence does *not* mean. It says always-on, not immobile: a
headset and a camera were both present for the entire window and both move.
Motion is the term that catches those, which is why it is the primary and these
are only priors.

This module is deliberately free of Home Assistant imports so it can be tested
on its own.
"""

from __future__ import annotations

from .const import (
    BEACON_MIN_WEIGHT,
    BEACON_MOTION_SCALE_DB,
)

# A Bluetooth address carries its own type in the top two bits of its most
# significant octet. Devices that rotate their address for privacy -- phones,
# and trackers of the Chipolo/Tile/AirTag kind -- use a resolvable private
# address, so they churn identity and can never build up a long baseline. In the
# reference house 227 of 443 addresses were rotating, and only 3% of those were
# present for most of the recording, against 23% of the fixed ones.
_ROTATING = 0x40
_STATIC_RANDOM = 0xC0

# How far an unknown device is trusted relative to one Home Assistant already
# manages. A registry match is a strong hint that something is installed
# infrastructure rather than passing through.
KNOWN_DEVICE = 1.0
UNKNOWN_DEVICE = 0.8
UNKNOWN_ROTATING = 0.4


def address_kind(address: str) -> str:
    """'rotating', 'static-random' or 'public' for a Bluetooth address."""
    try:
        top = int(address.split(":")[0], 16) & 0xC0
    except (ValueError, IndexError):
        return "public"
    if top == _ROTATING:
        return "rotating"
    if top == _STATIC_RANDOM:
        return "static-random"
    return "public"


def identity_weight(address: str, known: bool) -> float:
    """Trust from what the address and the device registry say about a beacon."""
    if known:
        # Home Assistant manages it, so it is installed kit. That outranks the
        # address type: plenty of fixed devices rotate their address anyway.
        return KNOWN_DEVICE
    if address_kind(address) == "rotating":
        return UNKNOWN_ROTATING
    return UNKNOWN_DEVICE


def beacon_weight(
    motion_db: float, persistence: float, address: str, known: bool
) -> float:
    """Combine the three signals into one multiplier on a beacon's springs.

    Multiplicative because the signals are independent reasons to distrust a
    beacon and any one of them should be able to dominate: a device that is
    visibly moving is a bad landmark however well Home Assistant knows it.
    """
    # Lorentzian rather than exponential: it falls off promptly around the
    # scale but keeps a long tail, so a beacon that moved once is demoted
    # rather than silenced.
    moving = 1.0 / (1.0 + (max(0.0, motion_db) / BEACON_MOTION_SCALE_DB) ** 2)
    present = min(1.0, max(0.0, persistence))
    weight = moving * present * identity_weight(address, known)
    return max(BEACON_MIN_WEIGHT, weight)
