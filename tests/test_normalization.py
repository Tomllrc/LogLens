"""Tests for timestamp normalization to UTC and level normalization."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from loglens.parsers.generic import GENERIC_TIMESTAMP_HINTS
from loglens.parsers.timestamps import TimestampNormalizer
from loglens.schema import EVENT_COLUMNS, LEVELS, make_event_id, normalize_level

REFERENCE = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)


def _utc(
    raw: str, tz: str = "UTC", hints: tuple[str, ...] = GENERIC_TIMESTAMP_HINTS
) -> datetime | None:
    return TimestampNormalizer(hints, tz=ZoneInfo(tz), reference=REFERENCE).to_utc(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-14T09:12:01Z", datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC)),
        ("2026-03-14T10:12:01+01:00", datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC)),
        ("2026-03-14 09:12:01,250", datetime(2026, 3, 14, 9, 12, 1, 250000, tzinfo=UTC)),
        ("14/Mar/2026:10:12:01 +0100", datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC)),
        ("081109 203615", datetime(2008, 11, 9, 20, 36, 15, tzinfo=UTC)),
        ("2005-06-03-15.42.50.675872", datetime(2005, 6, 3, 15, 42, 50, 675872, tzinfo=UTC)),
        ("1773479521", datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC)),
        ("1773479521.5", datetime(2026, 3, 14, 9, 12, 1, 500000, tzinfo=UTC)),
        ("1773479521500", datetime(2026, 3, 14, 9, 12, 1, 500000, tzinfo=UTC)),
        ("1773479521000000", datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC)),
    ],
)
def test_layouts_to_utc(raw: str, expected: datetime) -> None:
    assert _utc(raw) == expected


def test_naive_timestamps_use_the_timezone_option() -> None:
    assert _utc("2026-03-14 10:12:01", "Europe/Paris") == datetime(
        2026, 3, 14, 9, 12, 1, tzinfo=UTC
    )
    assert _utc("2026-03-14T10:12:01+00:00", "Europe/Paris") == datetime(
        2026, 3, 14, 10, 12, 1, tzinfo=UTC
    )  # an explicit offset wins over the option


def test_dst_transitions() -> None:
    # Europe/Paris switched to summer time on 2026-03-29 at 02:00 local (01:00 UTC).
    assert _utc("2026-03-29 01:30:00", "Europe/Paris") == datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
    assert _utc("2026-03-29 03:30:00", "Europe/Paris") == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    # Ambiguous autumn hour: the first occurrence (summer time) is used.
    assert _utc("2026-10-25 02:30:00", "Europe/Paris") == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_yearless_timestamps_take_the_reference_year_unless_in_the_future() -> None:
    assert _utc("Mar 14 09:12:01") == datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC)
    assert _utc("Dec 31 23:00:00") == datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
    assert _utc("Feb 29 10:00:00") == datetime(2024, 2, 29, 10, 0, tzinfo=UTC)  # leap year


def test_unplaceable_or_unreadable_timestamps_are_none() -> None:
    assert _utc("09:12:01") is None  # a time without a date
    assert _utc("not a time") is None
    assert _utc("") is None


def test_format_hints_are_tried_first_then_remembered() -> None:
    normalizer = TimestampNormalizer(
        ("%d.%m.%Y %H:%M", "iso8601"), tz=ZoneInfo("UTC"), reference=REFERENCE
    )
    assert normalizer.to_utc("14.03.2026 09:12") == datetime(2026, 3, 14, 9, 12, tzinfo=UTC)
    assert normalizer.to_utc("2026-03-14T09:13:00") == datetime(2026, 3, 14, 9, 13, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "level"),
    [
        ("INFO", "INFO"),
        ("Warning", "WARN"),
        ("err", "ERROR"),
        ("[notice]", "INFO"),
        ("CRITICAL", "FATAL"),
        ("severe", "ERROR"),
        ("verbose", "DEBUG"),
        ("3", "ERROR"),  # syslog severity
        ("50", "ERROR"),  # pino
        ("30", "INFO"),
        ("15", "UNKNOWN"),
        ("loud", "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_normalize_level(raw: str | None, level: str) -> None:
    assert normalize_level(raw) == level
    assert level in LEVELS


def test_event_schema_is_exactly_the_documented_one() -> None:
    assert EVENT_COLUMNS == (
        "event_id", "timestamp", "timestamp_raw", "source_file", "line_start", "line_end",
        "level", "level_raw", "component", "message", "template_id", "template", "params",
        "format_name", "parser_confidence", "raw",
    )  # fmt: skip


def test_event_ids_are_stable_and_distinct() -> None:
    assert make_event_id("a.log", 1, 1) == make_event_id("a.log", 1, 1)
    assert len({make_event_id("a.log", n, n) for n in range(1000)}) == 1000
    assert make_event_id("a.log", 1, 2) != make_event_id("b.log", 1, 2)
