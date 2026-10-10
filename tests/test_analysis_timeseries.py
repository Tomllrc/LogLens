"""Time-series helpers: bucket choice, robust z-scores, spikes, change points."""

import math
import random
from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loglens.analysis.timeseries import (
    ChangePoint,
    bucket_count,
    change_points,
    choose_bucket,
    format_bucket,
    parse_bucket,
    robust_z,
    spikes,
)


@pytest.mark.parametrize(
    ("span", "bucket"),
    [
        (timedelta(0), timedelta(seconds=1)),
        (timedelta(minutes=5), timedelta(seconds=1)),
        (timedelta(minutes=10), timedelta(seconds=10)),
        (timedelta(hours=24), timedelta(minutes=5)),
        (timedelta(days=400), timedelta(days=1)),
        (timedelta(days=3 * 365), timedelta(days=7)),
        (timedelta(days=30 * 365), timedelta(days=30)),
    ],
)
def test_choose_bucket_keeps_at_most_500_buckets(span: timedelta, bucket: timedelta) -> None:
    assert choose_bucket(span) == bucket
    assert bucket_count(span, bucket) <= 500


def test_absurd_spans_use_the_largest_bucket() -> None:
    assert choose_bucket(timedelta(days=100 * 365)) == timedelta(days=30)


def test_bucket_count_includes_the_last_partial_bucket() -> None:
    assert bucket_count(timedelta(0), timedelta(seconds=1)) == 1
    assert bucket_count(timedelta(minutes=10), timedelta(minutes=5)) == 3
    assert bucket_count(timedelta(minutes=9), timedelta(minutes=5)) == 2


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("30s", timedelta(seconds=30)),
        ("5m", timedelta(minutes=5)),
        ("2h", timedelta(hours=2)),
        ("1d", timedelta(days=1)),
        (" 10S ", timedelta(seconds=10)),
    ],
)
def test_parse_bucket(text: str, value: timedelta) -> None:
    assert parse_bucket(text) == value


@pytest.mark.parametrize("text", ["", "5", "m", "0m", "-1m", "5 minutes", "1.5h"])
def test_parse_bucket_rejects_bad_values(text: str) -> None:
    with pytest.raises(ValueError, match="bucket"):
        parse_bucket(text)


def test_robust_z_uses_median_and_mad() -> None:
    # median 3, MAD 1 -> scale 1.4826
    z = robust_z([1, 2, 3, 4, 5])
    assert z[2] == 0
    assert z[4] == pytest.approx(2 / 1.4826)


def test_robust_z_falls_back_to_mean_absolute_deviation() -> None:
    # median 0, MAD 0, mean absolute deviation 10/10 = 1
    values = [0.0] * 9 + [10.0]
    z = robust_z(values)
    assert z[-1] == pytest.approx(10 / 1.2533)
    assert z[0] == 0


def test_robust_z_of_constant_series_is_zero() -> None:
    assert robust_z([4, 4, 4]) == [0, 0, 0]


def test_robust_z_of_empty_series() -> None:
    assert robust_z([]) == []


def _noisy(n: int, level: float, seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    return [level + rng.randint(-5, 5) for _ in range(n)]


def test_spike_found_at_its_index() -> None:
    values = _noisy(288, 100)
    values[78] = 300
    assert spikes(values) == [78]


def test_small_counts_never_spike() -> None:
    values = [0.0] * 100
    values[10] = 2  # z is huge (MAD 0) but 2 < median + 3
    assert spikes(values) == []
    values[10] = 3
    assert spikes(values) == [10]


def test_step_down_is_a_change_point() -> None:
    values = _noisy(144, 100) + _noisy(144, 40, seed=2)
    found = change_points(values)
    assert len(found) == 1
    point = found[0]
    assert abs(point.index - 144) <= 1
    assert point.before == pytest.approx(100, abs=2)
    assert point.after == pytest.approx(40, abs=2)
    assert point.ratio == pytest.approx(2.5, abs=0.15)


def test_step_up_from_silence_is_a_change_point() -> None:
    values = [0.0] * 50 + _noisy(50, 20)
    found = change_points(values)
    assert [p.index for p in found] == [50]
    assert found[0].before == 0


def test_single_bucket_spike_is_not_a_change_point() -> None:
    values = _noisy(288, 100)
    values[100] = 400
    assert change_points(values) == []


def test_small_relative_change_is_ignored() -> None:
    values = _noisy(100, 100) + _noisy(100, 70, seed=3)
    assert change_points(values) == []


def test_short_series_have_no_change_points() -> None:
    assert change_points([1.0] * 10 + [100.0] * 9) == []


def test_ratio_floors_tiny_means() -> None:
    assert ChangePoint(5, 0.0, 3.0).ratio == 6.0
    assert ChangePoint(5, 4.0, 1.0).ratio == 4.0


@given(st.floats(0, 1e6), st.integers(20, 300))
def test_constant_series_have_no_spikes_or_change_points(level: float, n: int) -> None:
    values = [level] * n
    assert spikes(values) == []
    assert change_points(values) == []


@given(
    st.lists(st.integers(0, 1000), min_size=3, max_size=60),
    st.floats(0.1, 100),
    st.floats(-1000, 1000),
)
def test_robust_z_is_invariant_under_positive_affine_maps(
    values: list[int], scale: float, shift: float
) -> None:
    before = robust_z(values)
    after = robust_z([scale * v + shift for v in values])
    for x, y in zip(before, after, strict=True):
        if math.isinf(x):
            assert math.isinf(y) or abs(y) > 1e6
        else:
            assert y == pytest.approx(x, rel=1e-6, abs=1e-6)


@pytest.mark.parametrize(
    ("bucket", "text"),
    [
        (timedelta(seconds=10), "10s"),
        (timedelta(minutes=5), "5m"),
        (timedelta(hours=6), "6h"),
        (timedelta(days=1), "1d"),
        (timedelta(days=7), "1w"),
        (timedelta(days=30), "30d"),
        (timedelta(seconds=90), "90s"),
    ],
)
def test_format_bucket(bucket: timedelta, text: str) -> None:
    assert format_bucket(bucket) == text
