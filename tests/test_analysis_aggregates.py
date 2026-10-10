"""Aggregates: the SQL side of the analysis, on small real stores."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest
from tests.analysis_helpers import T0, build_store, line

from loglens.analysis.aggregates import (
    Grid,
    TemplateStats,
    event_ids_in_cells,
    event_refs,
    first_event_ids,
    level_series,
    make_grid,
    sample_events,
    template_series,
    template_stats,
    time_span,
    total_series,
    transition_counts,
    transition_event_ids,
)
from loglens.analysis.model import TimeWindow
from loglens.store import connect

M = timedelta(minutes=1)


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("aggregates")
    app = [
        line(0 * M, "INFO", "Service started on port 8080"),
        line(1 * M, "ERROR", "Connection to 10.0.0.1 refused by peer"),
        line(2 * M, "ERROR", "Connection to 10.0.0.2 refused by peer"),
        line(3 * M, "WARN", "it's 100% full: disk /data/a"),
        line(10 * M, "ERROR", "Connection to 10.0.0.3 refused by peer"),
        line(20 * M, "INFO", "Service started on port 8081"),
    ]
    other = [
        line(5 * M, "ERROR", "Connection to 10.0.0.9 refused by peer"),
        line(6 * M, "INFO", "Service started on port 9090"),
    ]
    return build_store(root, {"app.log": app, "other.log": other})


@pytest.fixture
def conn(store: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = connect(store)
    yield connection
    connection.close()


ALL = TimeWindow()


def _stats_by_text(
    conn: duckdb.DuckDBPyConnection, window: TimeWindow = ALL
) -> dict[str, TemplateStats]:
    return {s.template: s for s in template_stats(conn, window)}


def _template_id(conn: duckdb.DuckDBPyConnection, needle: str) -> str:
    return next(s.template_id for s in template_stats(conn, ALL) if needle in s.template)


def test_template_stats_counts_levels_times_and_sources(conn: duckdb.DuckDBPyConnection) -> None:
    stats = _stats_by_text(conn)
    refused = next(s for text, s in stats.items() if "refused" in text)
    assert refused.events == 4
    assert refused.by_level == {"ERROR": 4}
    assert refused.first_seen == T0 + 1 * M
    assert refused.last_seen == T0 + 10 * M
    assert refused.sources == 2
    assert refused.timestamped == 4


def test_template_text_with_quote_and_percent_survives(conn: duckdb.DuckDBPyConnection) -> None:
    assert any("it's" in text and "%" in text for text in _stats_by_text(conn))


def test_window_filters_template_stats(conn: duckdb.DuckDBPyConnection) -> None:
    stats = _stats_by_text(conn, TimeWindow(since=T0 + 4 * M, until=T0 + 15 * M))
    refused = next(s for text, s in stats.items() if "refused" in text)
    assert refused.events == 2
    assert sum(s.events for s in stats.values()) == 3


def test_empty_window_gives_nothing(conn: duckdb.DuckDBPyConnection) -> None:
    window = TimeWindow(since=T0 + timedelta(days=5))
    assert template_stats(conn, window) == []
    span = time_span(conn, window)
    assert span.start is None and span.timestamped == 0
    assert make_grid(span) is None


def test_time_span_and_grid(conn: duckdb.DuckDBPyConnection) -> None:
    span = time_span(conn, TimeWindow())
    assert (span.start, span.end) == (T0, T0 + 20 * M)
    assert span.timestamped == 8 and span.untimestamped == 0
    grid = make_grid(span)
    assert grid is not None
    assert grid.bucket == timedelta(seconds=10)  # 20 minutes -> 121 buckets of 10 s
    assert grid.start == T0
    assert grid.size == 121
    assert grid.bucket_start(6) == T0 + M


def test_grid_start_is_aligned_to_the_bucket(conn: duckdb.DuckDBPyConnection) -> None:
    span = time_span(conn, TimeWindow(since=T0 + timedelta(seconds=65)))
    grid = make_grid(span, timedelta(minutes=5))
    assert grid is not None
    assert grid.start == T0  # first event at 2 min, floored to the 5-minute boundary
    assert grid.size == 5  # 00:00 ... 00:20


def test_series_cover_the_grid_and_sum_to_event_counts(conn: duckdb.DuckDBPyConnection) -> None:
    grid = Grid(T0, timedelta(minutes=5), 5)
    total = total_series(conn, grid, TimeWindow())
    assert total == [4, 2, 1, 0, 1]
    assert total_series(conn, grid, TimeWindow(), levels=["ERROR"]) == [2, 1, 1, 0, 0]
    by_level = level_series(conn, grid, TimeWindow())
    assert by_level["INFO"] == [1, 1, 0, 0, 1]
    assert set(by_level) == {"INFO", "ERROR", "WARN"}
    refused = next(
        s.template_id for s in template_stats(conn, TimeWindow()) if "refused" in s.template
    )
    assert template_series(conn, grid, TimeWindow(), [refused]) == {refused: [2, 1, 1, 0, 0]}
    assert template_series(conn, grid, TimeWindow(), []) == {}


def test_transitions_stay_inside_each_file(conn: duckdb.DuckDBPyConnection) -> None:
    ids = {s.template: s.template_id for s in template_stats(conn, TimeWindow())}
    started = next(i for t, i in ids.items() if t.startswith("Service"))
    refused = next(i for t, i in ids.items() if "refused" in t)
    disk = next(i for t, i in ids.items() if "disk" in t)
    counts = {(t.prev, t.next): t.count for t in transition_counts(conn, TimeWindow())}
    assert counts == {
        (started, refused): 1,
        (refused, refused): 1,
        (refused, disk): 1,
        (disk, refused): 1,
        (refused, started): 2,  # app.log line 5 -> 6, other.log line 1 -> 2
    }
    events = transition_event_ids(conn, [(disk, refused)], TimeWindow())
    refs = event_refs(conn, events[(disk, refused)])
    assert [(r.source_file.rsplit("/", 1)[-1], r.line_start) for r in refs] == [("app.log", 5)]


def test_event_refs_point_to_lines(conn: duckdb.DuckDBPyConnection) -> None:
    ids = first_event_ids(
        conn, [s.template_id for s in template_stats(conn, TimeWindow())], TimeWindow()
    )
    refs = event_refs(conn, list(ids.values()))
    assert {r.location.rsplit("/", 1)[-1] for r in refs} == {"app.log:1", "app.log:2", "app.log:4"}


def test_sample_events_include_first_and_last(conn: duckdb.DuckDBPyConnection) -> None:
    refused = next(
        s.template_id for s in template_stats(conn, TimeWindow()) if "refused" in s.template
    )
    refs = sample_events(conn, [refused], TimeWindow(), k=1)
    times = [r.timestamp for r in refs]
    assert times[0] == T0 + 1 * M
    assert times[-1] == T0 + 10 * M
    assert times == sorted(times)
    assert len(refs) == 3


def test_event_ids_in_cells(conn: duckdb.DuckDBPyConnection) -> None:
    grid = Grid(T0, timedelta(minutes=5), 5)
    refused = next(
        s.template_id for s in template_stats(conn, TimeWindow()) if "refused" in s.template
    )
    by_template = event_ids_in_cells(conn, grid, [(0, refused)], TimeWindow())
    assert len(by_template) == 2
    any_template = event_ids_in_cells(conn, grid, [(0, None)], TimeWindow())
    assert len(any_template) == 4
    errors_only = event_ids_in_cells(conn, grid, [(0, None)], TimeWindow(), levels=["ERROR"])
    assert errors_only == by_template
    assert event_ids_in_cells(conn, grid, [], TimeWindow()) == set()


def test_untimestamped_store(tmp_path: Path) -> None:
    store = build_store(
        tmp_path,
        {"plain.log": ["job 1 failed", "job 2 failed", "all good here"]},
    )
    connection = connect(store)
    try:
        span = time_span(connection, TimeWindow())
        assert span.start is None and span.untimestamped == 3
        assert make_grid(span) is None
        assert sum(s.events for s in template_stats(connection, TimeWindow())) == 3
        assert len(transition_counts(connection, TimeWindow())) == 2
    finally:
        connection.close()


def test_empty_store(tmp_path: Path) -> None:
    store = build_store(tmp_path, {"empty.log": []})
    connection = connect(store)
    try:
        assert template_stats(connection, TimeWindow()) == []
        assert transition_counts(connection, TimeWindow()) == []
        assert time_span(connection, TimeWindow()).timestamped == 0
        assert first_event_ids(connection, ["x"], TimeWindow()) == {}
        assert event_refs(connection, []) == []
    finally:
        connection.close()
