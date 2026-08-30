"""Accumulate smoothed RSSI readings so the solver has something stable to use.

A single scan is far too noisy to estimate geometry from: readings swing several
dB between adverts. This keeps an exponentially weighted average per
(anchor, beacon) pair and lets stale beacons expire.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    BEACON_MIN_SAMPLES,
    RECORDER_INTERVAL,
    RECORDER_SLOW_SMOOTHING,
    RECORDER_SMOOTHING,
    RECORDER_STALE_AFTER,
)

_LOGGER = logging.getLogger(__name__)

# An ESPHome node's BLE MAC sits within a few digits of its network MAC, so the
# same tolerance that matches scanners to devices also spots one anchor hearing
# another.
_MAC_TOLERANCE = 4


@dataclass
class _Reading:
    """One RSSI track, averaged over two timescales.

    The fast average is the value the solver uses. The slow one exists only to
    be compared against it: a beacon that has actually moved holds the two
    apart, while noise pushes the fast average either side of the slow one and
    cancels. That difference is the motion signal, and it costs one float.
    """

    rssi: float
    slow: float = 0.0
    samples: int = 0
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not self.slow:
            self.slow = self.rssi

    def update(self, rssi: float, smoothing: float) -> None:
        self.rssi = smoothing * rssi + (1 - smoothing) * self.rssi
        self.slow = (
            RECORDER_SLOW_SMOOTHING * rssi
            + (1 - RECORDER_SLOW_SMOOTHING) * self.slow
        )
        self.samples += 1
        self.updated = time.monotonic()

    @property
    def drift(self) -> float:
        """How far the recent average has moved away from the long-run one."""
        return self.rssi - self.slow


class SignalRecorder:
    """Samples every scanner on a timer and keeps a smoothed view."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._readings: dict[str, dict[str, _Reading]] = {}
        self._names: dict[str, str] = {}
        self._unsub: Any = None
        self.started: float | None = None

    @callback
    def async_start(self) -> None:
        """Begin sampling."""
        if self._unsub is not None:
            return
        self.started = time.monotonic()
        self._async_sample(None)
        self._unsub = async_track_time_interval(
            self.hass, self._async_sample, RECORDER_INTERVAL
        )
        _LOGGER.debug("Signal recorder started")

    @callback
    def async_stop(self) -> None:
        """Stop sampling and drop what we have."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._readings.clear()
        self._names.clear()
        self.started = None

    @callback
    def _async_sample(self, _now: Any) -> None:
        """Fold one scan of every scanner into the smoothed view."""
        for scanner in bluetooth.async_current_scanners(self.hass):
            source = getattr(scanner, "source", None)
            if not source:
                continue
            paired = getattr(
                scanner, "discovered_devices_and_advertisement_data", None
            )
            if not paired:
                continue

            track = self._readings.setdefault(source, {})
            for address, (device, adv) in paired.items():
                rssi = getattr(adv, "rssi", None)
                if rssi is None:
                    continue
                # Most beacons never advertise a name, and the ones that do only
                # include it in some adverts, so keep the first one seen rather
                # than whatever the latest packet happened to carry.
                if address not in self._names:
                    name = getattr(adv, "local_name", None) or getattr(
                        device, "name", None
                    )
                    if name and name != address:
                        self._names[address] = name
                if (reading := track.get(address)) is None:
                    track[address] = _Reading(rssi=float(rssi), samples=1)
                else:
                    reading.update(float(rssi), RECORDER_SMOOTHING)

        self._async_expire()

    @callback
    def _async_expire(self) -> None:
        """Drop beacons nobody has heard for a while."""
        cutoff = time.monotonic() - RECORDER_STALE_AFTER.total_seconds()
        for track in self._readings.values():
            for address in [a for a, r in track.items() if r.updated < cutoff]:
                del track[address]

    @property
    def elapsed(self) -> float:
        """Seconds of data collected so far."""
        return 0.0 if self.started is None else time.monotonic() - self.started

    def observations(self, sources: list[str]) -> dict[str, dict[str, float]]:
        """anchor -> {beacon address: smoothed RSSI}, excluding the anchors.

        Everything with enough readings to mean anything is returned. Beacons
        are no longer filtered on how jumpy they look, because that measure
        turned out to track signal strength rather than movement and threw away
        the best-observed references. Which beacons to trust is decided by
        weight instead, in quality.py.
        """
        anchor_addresses = set(sources)
        return {
            source: {
                address: reading.rssi
                for address, reading in self._readings.get(source, {}).items()
                if reading.samples >= BEACON_MIN_SAMPLES
                and not _matches_any(address, anchor_addresses)
            }
            for source in sources
        }

    def beacon_quality(self, sources: list[str]) -> dict[str, dict[str, float]]:
        """beacon address -> the raw evidence about how good a landmark it is.

        `motion` is the RMS gap between the fast and slow averages across every
        radio hearing the beacon. Averaging rather than taking the worst matters:
        the previous measure took a maximum across radios, so a beacon heard by
        six radios scored worse than one heard by a single radio purely because
        the maximum of more samples is larger.

        `persistence` is how much of the recording the beacon was present for,
        which is what separates installed kit from things passing through.
        """
        expected = max(1.0, self.elapsed / RECORDER_INTERVAL.total_seconds())
        gathered: dict[str, list[_Reading]] = {}
        anchor_addresses = set(sources)
        for source in sources:
            for address, reading in self._readings.get(source, {}).items():
                if not _matches_any(address, anchor_addresses):
                    gathered.setdefault(address, []).append(reading)

        quality = {}
        for address, readings in gathered.items():
            drifts = [r.drift for r in readings]
            motion = math.sqrt(sum(d * d for d in drifts) / len(drifts))
            samples = max(r.samples for r in readings)
            quality[address] = {
                "motion": round(motion, 2),
                "persistence": round(min(1.0, samples / expected), 3),
                "samples": samples,
                "radios": len(readings),
            }
        return quality

    def direct_links(self, sources: list[str]) -> dict[tuple[str, str], list[float]]:
        """(listener, advertiser) -> smoothed RSSI, for anchors hearing anchors."""
        links: dict[tuple[str, str], list[float]] = {}
        for listener in sources:
            for address, reading in self._readings.get(listener, {}).items():
                advertiser = _match_source(address, sources, exclude=listener)
                if advertiser is not None:
                    links.setdefault((listener, advertiser), []).append(reading.rssi)
        return links

    def names(self) -> dict[str, str]:
        """beacon address -> the friendliest name it has ever advertised."""
        return dict(self._names)

    def sample_counts(self, sources: list[str]) -> dict[str, int]:
        """How many beacons each anchor currently has a track for."""
        return {source: len(self._readings.get(source, {})) for source in sources}


def _split_mac(value: str) -> tuple[str, int] | None:
    parts = value.upper().split(":")
    if len(parts) != 6:
        return None
    try:
        return ":".join(parts[:5]), int(parts[5], 16)
    except ValueError:
        return None


def _is_near(first: str, second: str) -> bool:
    """True if two MACs are the same radio advertising under a nearby address."""
    a, b = _split_mac(first), _split_mac(second)
    if a is None or b is None:
        return False
    return a[0] == b[0] and abs(a[1] - b[1]) <= _MAC_TOLERANCE


def _matches_any(address: str, candidates: set[str]) -> bool:
    return any(_is_near(address, candidate) for candidate in candidates)


def _match_source(
    address: str, sources: list[str], exclude: str
) -> str | None:
    for source in sources:
        if source != exclude and _is_near(address, source):
            return source
    return None
