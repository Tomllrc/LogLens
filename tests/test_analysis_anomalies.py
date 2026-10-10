"""Anomaly detectors on a small store with planted anomalies."""

import random
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest
from tests.analysis_helpers import T0, build_store, line

from loglens.analysis.aggregates import event_refs
from loglens.analysis.anomalies import KINDS, find_anomalies, predicted_event_ids
from loglens.analysis.model import TimeWindow
from loglens.store import connect

M = timedelta(minutes=1)
S = timedelta(seconds=1)
INFO_MESSAGES = (
    "GET /api/items/{n} served in {n}ms",
    "User u{n} logged in from 10.1.0.{n}",
    "Cache hit for key k{n}",
    "Scheduled task t{n} completed",
)


def _app_lines(drop: bool = True, burst: bool = True, new: bool = True) -> list[str]:
    rng = random.Random(7)
    lines = []
    for minute in range(240):
        count = 40 + minute % 3  # worker.log adds a steady 15 per minute to the total
        if drop and minute >= 160:
            count = 3 + minute % 3
        for i in range(count):
            text = rng.choice(INFO_MESSAGES).format(n=rng.randint(1, 250))
            lines.append(line(minute * M + i * S, "INFO", text))
        if minute % 30 == 0:
            lines.append(
                line(
                    minute * M + 30 * S,
                    "ERROR",
                    f"Payment gateway timeout after {minute + 3000} ms",
                )
            )
        if burst and minute == 100:
            for i in range(80):
                lines.append(
                    line(
                        minute * M + 31 * S, "ERROR", f"Payment gateway timeout after {i + 5000} ms"
                    )
                )
        if new and minute >= 200 and minute % 5 == 0:
            lines.append(
                line(minute * M + 40 * S, "ERROR", f"Disk quota exceeded on volume /data/v{minute}")
            )
    return lines


def _job_messages(job: int, deviate: bool) -> list[str]:
    # Distinct token counts: Drain only merges messages of the same length.
    step = "step load skipped from cache" if deviate else "step load done"
    return [f"Job {job} started on worker pool", f"Job {job} {step}", f"Job {job} finished"]


def _worker_lines(deviation: bool = True) -> list[str]:
    lines = []
    for job in range(1200):
        at = job * 12 * S
        messages = _job_messages(job, deviation and job == 900)
        lines += [line(at + i * S, "INFO", text) for i, text in enumerate(messages)]
    return lines


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("anomalies")
    return build_store(root, {"app.log": _app_lines(), "worker.log": _worker_lines()})


@pytest.fixture
def conn(store: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = connect(store)
    yield connection
    connection.close()


def test_burst_is_a_spike_in_template_errors_and_total(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_anomalies(conn)
    assert report.grid is not None and report.grid.bucket == M
    spikes = report.by_kind("spike")
    series = {a.series for a in spikes}
    assert {"template", "errors", "total"} <= series
    template_spike = next(a for a in spikes if a.series == "template")
    assert template_spike.start == T0 + 100 * M
    assert template_spike.end == T0 + 101 * M
    assert template_spike.buckets == (100,)
    assert template_spike.issue_id is not None
    assert "Payment gateway timeout" in template_spike.summary
    assert all(
        ref.timestamp is not None and ref.timestamp >= T0 + 100 * M
        for ref in template_spike.evidence
    )
    assert max(a.score for a in spikes) == 1.0
    assert template_spike.raw_score >= 75  # excess events over the median, not a capped z


def test_spike_scores_rank_bigger_bursts_higher(conn: duckdb.DuckDBPyConnection) -> None:
    spikes = find_anomalies(conn).by_kind("spike")
    by_series = {a.series: a for a in spikes}
    assert by_series["total"].raw_score >= by_series["template"].raw_score


def test_drop_is_a_level_shift(conn: duckdb.DuckDBPyConnection) -> None:
    shifts = find_anomalies(conn).by_kind("level_shift")
    total = [a for a in shifts if a.series == "total"]
    assert len(total) == 1
    assert abs((total[0].start - T0) / M - 160) <= 1
    assert "down" in total[0].summary


def test_new_error_pattern_is_found(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_anomalies(conn)
    new = report.by_kind("new_pattern")
    assert new[0].start == T0 + 200 * M + 40 * S  # the error pattern ranks first
    assert "Disk quota exceeded" in new[0].summary
    assert new[0].evidence[0].message.startswith("Disk quota exceeded on volume /data/v200")
    assert not any("Payment" in a.summary for a in new)
    assert any("skipped from cache" in a.summary for a in new)  # INFO, ranked after
    assert new[0].score == 1.0 and new[-1].score < 1.0


def test_broken_sequence_is_a_rare_transition(conn: duckdb.DuckDBPyConnection) -> None:
    rare = find_anomalies(conn).by_kind("rare_transition")
    assert len(rare) == 1
    assert "skipped from cache" in rare[0].summary
    (ref,) = rare[0].evidence
    assert ref.location.endswith(f"worker.log:{900 * 3 + 2}")


def test_kinds_filter_and_validation(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_anomalies(conn, kinds=["new_pattern"])
    assert {a.kind for a in report.anomalies} == {"new_pattern"}
    with pytest.raises(ValueError, match="unknown anomaly kind"):
        find_anomalies(conn, kinds=["weird"])
    assert KINDS == ("spike", "level_shift", "new_pattern", "rare_transition")


def test_anomalies_are_sorted_by_kind_then_score(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_anomalies(conn)
    order = [KINDS.index(a.kind) for a in report.anomalies]
    assert order == sorted(order)
    for kind in KINDS:
        scores = [a.score for a in report.by_kind(kind)]
        assert scores == sorted(scores, reverse=True)


def test_predicted_events(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_anomalies(conn)
    predicted = predicted_event_ids(conn, report)
    refs = event_refs(conn, sorted(predicted))
    messages = [r.message for r in refs]
    assert sum("Payment gateway timeout" in m for m in messages) >= 80
    assert sum("Disk quota" in m for m in messages) == 1  # first occurrence only
    assert any("skipped from cache" in m for m in messages)
    assert not any(m.startswith(("GET ", "User ", "Cache hit")) for m in messages)


def test_steady_store_has_no_spikes_shifts_or_rare_transitions(tmp_path: Path) -> None:
    store = build_store(
        tmp_path,
        {
            "app.log": _app_lines(drop=False, burst=False, new=False),
            "worker.log": _worker_lines(deviation=False),
        },
    )
    connection = connect(store)
    try:
        report = find_anomalies(connection)
    finally:
        connection.close()
    assert report.by_kind("spike") == []
    assert report.by_kind("level_shift") == []
    assert report.by_kind("rare_transition") == []


def test_baseline_until_moves_the_new_pattern_cutoff(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_anomalies(conn, kinds=["new_pattern"], baseline_until=T0 + 230 * M)
    assert report.baseline_until == T0 + 230 * M
    assert report.by_kind("new_pattern") == []


def test_window_restricts_the_analysis(conn: duckdb.DuckDBPyConnection) -> None:
    window = TimeWindow(since=T0 + 150 * M, until=T0 + 190 * M)
    report = find_anomalies(conn, window=window, kinds=["spike"])
    assert report.span.start is not None and report.span.start >= T0 + 150 * M
    assert not any(a.start is not None and a.start < T0 + 150 * M for a in report.anomalies)


def test_untimestamped_store_skips_time_detectors(tmp_path: Path) -> None:
    lines = []
    for job in range(1200):
        lines += _job_messages(job, job == 600)
    store = build_store(tmp_path, {"jobs.log": lines})
    connection = connect(store)
    try:
        report = find_anomalies(connection)
    finally:
        connection.close()
    assert report.grid is None
    assert [a.kind for a in report.anomalies] == ["rare_transition"]
    assert report.anomalies[0].start is None
    notes = " ".join(report.notes)
    assert "spike" in notes and "new_pattern" in notes and "no timestamp" in notes


def test_short_store_skips_spikes_and_shifts(tmp_path: Path) -> None:
    store = build_store(
        tmp_path, {"a.log": [line(i * S, "ERROR", "boom happened now") for i in range(5)]}
    )
    connection = connect(store)
    try:
        report = find_anomalies(connection)
    finally:
        connection.close()
    assert report.anomalies == ()
    assert any("buckets" in note for note in report.notes)


def test_empty_store(tmp_path: Path) -> None:
    store = build_store(tmp_path, {"empty.log": []})
    connection = connect(store)
    try:
        report = find_anomalies(connection)
        assert predicted_event_ids(connection, report) == set()
    finally:
        connection.close()
    assert report.anomalies == ()
