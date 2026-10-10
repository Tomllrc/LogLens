"""Shared analysis types: time windows, event references, JSON conversion."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loglens.analysis.model import EventRef, TimeWindow, jsonable

T0 = datetime(2026, 3, 1, tzinfo=UTC)


def test_inactive_window_matches_everything() -> None:
    window = TimeWindow()
    assert not window.active
    assert window.where() == ("true", [])


def test_window_bounds_become_parameters() -> None:
    window = TimeWindow(since=T0, until=T0 + timedelta(hours=1))
    assert window.active
    sql, params = window.where()
    assert sql == "timestamp >= ? AND timestamp < ?"
    assert params == [T0, T0 + timedelta(hours=1)]


def test_window_with_one_bound_and_custom_column() -> None:
    assert TimeWindow(until=T0).where("ts") == ("ts < ?", [T0])
    assert TimeWindow(since=T0).where() == ("timestamp >= ?", [T0])


def _ref(line_start: int, line_end: int) -> EventRef:
    return EventRef("e1", "app.log", line_start, line_end, T0, "ERROR", "boom")


def test_location_of_single_and_multi_line_events() -> None:
    assert _ref(12, 12).location == "app.log:12"
    assert _ref(12, 14).location == "app.log:12-14"


@dataclass(frozen=True)
class _Nested:
    when: datetime
    every: timedelta
    tags: frozenset[str]
    ref: EventRef
    missing: None = None


def test_jsonable_converts_nested_values() -> None:
    value = _Nested(T0, timedelta(minutes=5), frozenset({"b", "a"}), _ref(1, 1))
    assert jsonable(value) == {
        "when": "2026-03-01T00:00:00+00:00",
        "every": 300.0,
        "tags": ["a", "b"],
        "ref": {
            "event_id": "e1",
            "source_file": "app.log",
            "line_start": 1,
            "line_end": 1,
            "timestamp": "2026-03-01T00:00:00+00:00",
            "level": "ERROR",
            "message": "boom",
            "location": "app.log:1",
        },
        "missing": None,
    }


def test_jsonable_handles_mappings_and_tuples() -> None:
    assert jsonable({"a": (1, 2.5), 3: [T0]}) == {"a": [1, 2.5], "3": ["2026-03-01T00:00:00+00:00"]}


def test_jsonable_turns_infinite_floats_into_strings() -> None:
    assert jsonable(float("inf")) == "inf"
