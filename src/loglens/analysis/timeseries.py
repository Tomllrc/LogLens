"""Time-series helpers: bucket sizes, robust z-scores, spikes and change points.

Series are small (at most ``MAX_BUCKETS`` points), so everything here works on plain
sequences; change points use ``ruptures`` (PELT).
"""

import itertools
import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

import numpy as np
import ruptures as rpt

BUCKETS: Final = (
    timedelta(seconds=1),
    timedelta(seconds=10),
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(days=1),
)
MAX_BUCKETS: Final = 500
MIN_POINTS: Final = 20
"""Shorter series are not analysed over time: there is no baseline to compare with."""

SPIKE_Z: Final = 5.0
SPIKE_MIN_EXCESS: Final = 3.0
SHIFT_MIN_RATIO: Final = 2.0
SHIFT_MIN_SIZE: Final = 3

_MAD_SCALE: Final = 1.4826  # MAD -> standard deviation for normal data
_MEAN_AD_SCALE: Final = 1.2533  # mean absolute deviation -> standard deviation
_UNITS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def bucket_count(span: timedelta, bucket: timedelta) -> int:
    """Buckets needed to cover ``span`` (the last one may be partial)."""
    return int(span / bucket) + 1


def choose_bucket(span: timedelta) -> timedelta:
    """The smallest standard bucket covering ``span`` in at most ``MAX_BUCKETS`` buckets."""
    for bucket in BUCKETS:
        if bucket_count(span, bucket) <= MAX_BUCKETS:
            return bucket
    return BUCKETS[-1]


def parse_bucket(text: str) -> timedelta:
    """Parse ``30s``, ``5m``, ``2h`` or ``1d``."""
    match = re.fullmatch(r"([0-9]+)([smhd])", text.strip().lower())
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"invalid bucket {text!r}: use a positive number and s, m, h or d")
    return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])


def robust_z(values: Sequence[float]) -> list[float]:
    """Robust z-scores: distance to the median in robust standard deviations.

    The scale is 1.4826 x MAD; when the MAD is 0 (most values equal), 1.2533 x the mean
    absolute deviation. A constant series scores 0 everywhere.
    """
    if not values:
        return []
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    scale = _MAD_SCALE * statistics.median(deviations)
    if scale == 0:
        scale = _MEAN_AD_SCALE * statistics.fmean(deviations)
    if scale == 0:
        return [0.0 for _ in values]
    return [(v - median) / scale for v in values]


def spikes(
    values: Sequence[float], z: float = SPIKE_Z, min_excess: float = SPIKE_MIN_EXCESS
) -> list[int]:
    """Indexes whose robust z-score is at least ``z`` and value at least median + excess."""
    if not values:
        return []
    median = statistics.median(values)
    return [
        index
        for index, (value, score) in enumerate(zip(values, robust_z(values), strict=True))
        if score >= z and value >= median + min_excess
    ]


@dataclass(frozen=True)
class ChangePoint:
    """A level shift: ``index`` is the first bucket of the new level."""

    index: int
    before: float
    after: float

    @property
    def ratio(self) -> float:
        """How many times larger the higher level is (levels below 0.5 count as 0.5)."""
        return max(self.before, self.after) / max(min(self.before, self.after), 0.5)


def change_points(
    values: Sequence[float],
    min_size: int = SHIFT_MIN_SIZE,
    min_ratio: float = SHIFT_MIN_RATIO,
) -> list[ChangePoint]:
    """Level shifts found by PELT (l2 cost) and kept when the level changes enough.

    The penalty is ``3 log(n) sigma^2`` with sigma estimated from first differences (robust
    to the shifts themselves). Segment levels are medians, so a lone spike carved into a
    short segment does not look like a new level.
    """
    n = len(values)
    if n < MIN_POINTS or min(values) == max(values):
        return []
    sigma = _noise_sigma(values)
    if sigma == 0:
        return []
    signal = np.asarray(values, dtype=float).reshape(-1, 1)
    algo = rpt.Pelt(model="l2", min_size=min_size, jump=1).fit(signal)
    breakpoints: list[int] = algo.predict(pen=3 * math.log(n) * sigma**2)
    bounds = [0, *breakpoints]
    segments = [values[bounds[i] : bounds[i + 1]] for i in range(len(bounds) - 1)]
    found: list[ChangePoint] = []
    for i in range(1, len(segments)):
        before, after = segments[i - 1], segments[i]
        if len(before) < min_size or len(after) < min_size:
            continue
        point = ChangePoint(bounds[i], statistics.median(before), statistics.median(after))
        if point.ratio >= min_ratio:
            found.append(point)
    return found


def _noise_sigma(values: Sequence[float]) -> float:
    diffs = [b - a for a, b in itertools.pairwise(values)]
    mad = statistics.median(abs(d - statistics.median(diffs)) for d in diffs)
    sigma = _MAD_SCALE * mad / math.sqrt(2)
    return sigma if sigma > 0 else statistics.pstdev(values)
