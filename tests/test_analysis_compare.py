"""Compare: new, gone and changed issues between two stores or two periods."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest
from tests.analysis_helpers import T0, build_store, line

from loglens.analysis.compare import compare, rate_test
from loglens.analysis.model import TimeWindow
from loglens.store import connect

M = timedelta(minutes=1)
SPLIT = T0 + 60 * M


def test_rate_test() -> None:
    assert rate_test(50, 1.0, 50, 1.0) == pytest.approx(1.0)
    assert rate_test(10, 1.0, 100, 1.0) < 0.01
    assert rate_test(10, 1.0, 20, 2.0) == pytest.approx(1.0)  # same rate per hour
    assert rate_test(0, 1.0, 0, 1.0) == 1.0


def _lines(first_hour: bool, second_hour: bool) -> list[str]:
    lines = []
    for minute in range(120):
        if (minute < 60 and not first_hour) or (minute >= 60 and not second_hour):
            continue
        lines.append(line(minute * M, "INFO", f"GET /api/items/{minute} served in 5ms"))
        lines.append(line(minute * M, "ERROR", f"Connection to 10.0.0.{minute} refused by peer"))
        if minute < 60 and minute % 12 == 0:
            lines.append(line(minute * M, "ERROR", f"Payment gateway timeout after {minute} ms"))
        if minute >= 60:
            lines.append(line(minute * M, "ERROR", f"Payment gateway timeout after {minute} ms"))
        if minute < 60 and minute % 10 == 0:
            lines.append(line(minute * M, "WARN", f"Cache warm-up failed for shard {minute}"))
        if minute >= 60 and minute % 5 == 0:
            lines.append(line(minute * M, "ERROR", f"Disk quota exceeded on volume /data/{minute}"))
    return lines


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_store(tmp_path_factory.mktemp("compare"), {"app.log": _lines(True, True)})


@pytest.fixture
def conn(store: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = connect(store)
    yield connection
    connection.close()


def _titles(changes: tuple) -> list[str]:  # type: ignore[type-arg]
    return [c.title.split(" ")[0] for c in changes]


def test_split_mode(conn: duckdb.DuckDBPyConnection) -> None:
    report = compare(
        conn, conn, baseline_window=TimeWindow(until=SPLIT), target_window=TimeWindow(since=SPLIT)
    )
    assert _titles(report.new) == ["Disk"]
    assert _titles(report.gone) == ["Cache"]
    assert _titles(report.changed) == ["Payment"]
    payment = report.changed[0]
    assert payment.baseline is not None and payment.target is not None
    assert payment.baseline.events == 5 and payment.target.events == 60
    assert payment.ratio == pytest.approx(12, rel=0.1)
    assert payment.p_value is not None and payment.p_value < 0.01
    assert report.new[0].baseline is None and report.new[0].p_value is None
    assert report.baseline.events == 60 + 60 + 5 + 6
    assert report.target.events == 60 + 60 + 60 + 12
    assert report.target.error_rate > report.baseline.error_rate


def test_two_stores_give_the_same_answer(tmp_path: Path) -> None:
    base = build_store(tmp_path, {"app.log": _lines(True, False)}, name="base")
    target = build_store(tmp_path, {"app.log": _lines(False, True)}, name="target")
    base_conn, target_conn = connect(base), connect(target)
    try:
        report = compare(base_conn, target_conn)
    finally:
        base_conn.close()
        target_conn.close()
    assert _titles(report.new) == ["Disk"]
    assert _titles(report.gone) == ["Cache"]
    assert _titles(report.changed) == ["Payment"]


def test_differently_mined_templates_still_match(tmp_path: Path) -> None:
    base = build_store(
        tmp_path,
        {
            "a.log": [
                line(i * M, "ERROR", f"Connection to 10.0.0.{i} refused by peer") for i in range(30)
            ]
        },
        name="base",
    )
    target = build_store(
        tmp_path,
        {
            "a.log": [
                line(i * M, "ERROR", f"Connection to 10.0.0.{i} refused by remote peer")
                for i in range(30)
            ]
        },
        name="target",
    )
    base_conn, target_conn = connect(base), connect(target)
    try:
        report = compare(base_conn, target_conn)
    finally:
        base_conn.close()
        target_conn.close()
    assert report.new == () and report.gone == () and report.changed == ()


def test_empty_target_makes_everything_gone(conn: duckdb.DuckDBPyConnection) -> None:
    report = compare(conn, conn, target_window=TimeWindow(since=T0 + timedelta(days=2)))
    assert report.new == () and report.changed == ()
    assert len(report.gone) == 4
    assert report.target.events == 0 and report.target.error_rate == 0


def test_untimestamped_stores_compare_counts(tmp_path: Path) -> None:
    base = build_store(tmp_path, {"a.log": ["job 1 failed hard"] * 5}, name="base")
    target = build_store(tmp_path, {"a.log": ["job 1 failed hard"] * 50}, name="target")
    base_conn, target_conn = connect(base), connect(target)
    try:
        report = compare(base_conn, target_conn)
    finally:
        base_conn.close()
        target_conn.close()
    assert _titles(report.changed) == ["job"]
    assert any("no timestamp" in note for note in report.notes)
    assert report.baseline.hours is None
