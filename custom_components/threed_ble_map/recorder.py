"""Accumulate smoothed RSSI readings so the solver has something stable to use.

A single scan is far too noisy to estimate geometry from: readings swing several
dB between adverts. This keeps an exponentially weighted average per
(anchor, beacon) pair and lets stale beacons expire.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    BEACON_MAX_SPREAD_DB,
    BEACON_MIN_SAMPLES,
    RECORDER_INTERVAL,
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
    """One smoothed RSSI track, with a running measure of how jumpy it is."""

    rssi: float
    samples: int = 0
    spread: float = 0.0
    updated: float = field(default_factory=time.monotonic)

    def update(self, rssi: float, smoothing: float) -> None:
        # Mean absolute deviation, smoothed the same way as the value itself.
        # A beacon sitting still varies by the radio noise floor; one being
        # carried around swings far wider, and it is the wide ones that poison
        # a solve which assumes everything is stationary.
        deviation = abs(rssi - self.rssi)
        self.spread = smoothing * deviation + (1 - smoothing) * self.spread
        self.rssi = smoothing * rssi + (1 - smoothing) * self.rssi
        self.samples += 1
        self.updated = time.monotonic()


class SignalRecorder:
    """Samples every scanner on a timer and keeps a smoothed view."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._readings: dict[str, dict[str, _Reading]] = {}
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
            for address, (_device, adv) in paired.items():
                rssi = getattr(adv, "rssi", None)
                if rssi is None:
                    continue
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

    def observations(
        self, sources: list[str], stable_only: bool = True
    ) -> dict[str, dict[str, float]]:
        """anchor -> {beacon address: smoothed RSSI}, excluding the anchors.

        Beacons whose signal is still settling, or which swing too much to be
        sitting still, are left out: the solver assumes a static world, so a
        beacon in someone's pocket drags the layout around with it.
        """
        anchor_addresses = set(sources)
        excluded = self.unstable(sources) if stable_only else set()
        return {
            source: {
                address: reading.rssi
                for address, reading in self._readings.get(source, {}).items()
                if not _matches_any(address, anchor_addresses)
                and address not in excluded
            }
            for source in sources
        }

    def unstable(self, sources: list[str]) -> set[str]:
        """Beacons too jumpy, or too new, to treat as fixed points."""
        return {
            address
            for address, (spread, samples) in self.stability(sources).items()
            if samples < BEACON_MIN_SAMPLES or spread > BEACON_MAX_SPREAD_DB
        }

    def direct_links(self, sources: list[str]) -> dict[tuple[str, str], list[float]]:
        """(listener, advertiser) -> smoothed RSSI, for anchors hearing anchors."""
        links: dict[tuple[str, str], list[float]] = {}
        for listener in sources:
            for address, reading in self._readings.get(listener, {}).items():
                advertiser = _match_source(address, sources, exclude=listener)
                if advertiser is not None:
                    links.setdefault((listener, advertiser), []).append(reading.rssi)
        return links

    def sample_counts(self, sources: list[str]) -> dict[str, int]:
        """How many beacons each anchor currently has a track for."""
        return {source: len(self._readings.get(source, {})) for source in sources}

    def stability(self, sources: list[str]) -> dict[str, tuple[float, int]]:
        """beacon address -> (worst spread in dB across radios, sample count)."""
        result: dict[str, tuple[float, int]] = {}
        for source in sources:
            for address, reading in self._readings.get(source, {}).items():
                spread, samples = result.get(address, (0.0, 0))
                result[address] = (
                    max(spread, reading.spread),
                    max(samples, reading.samples),
                )
        return result


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
