"""Recurring issues: candidates, grouping, impact, Pareto, evidence."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest
from tests.analysis_helpers import T0, build_store, line

from loglens.analysis.aggregates import group_source_counts, template_stats
from loglens.analysis.issues import (
    Issue,
    find_issues,
    issue_id_for,
    rank_issues,
    template_issue_map,
)
from loglens.analysis.model import TimeWindow
from loglens.store import connect

M = timedelta(minutes=1)


def _issue(issue_id: str, impact: float, events: int = 1) -> Issue:
    return Issue(
        issue_id=issue_id,
        title=issue_id,
        template_ids=(issue_id,),
        events=events,
        by_level={"ERROR": events},
        first_seen=None,
        last_seen=None,
        sources=1,
        top_components=(),
        persistence=None,
        impact=impact,
    )


def test_issue_ids_are_stable_and_short() -> None:
    assert issue_id_for(["b", "a"]) == issue_id_for(["a"])
    assert issue_id_for(["a"]) != issue_id_for(["b"])
    assert issue_id_for(["a"]).startswith("ISS-")
    assert len(issue_id_for(["a"])) == 12


def test_single_issue_is_the_whole_pareto() -> None:
    (only,) = rank_issues([_issue("x", 5)])
    assert (only.share, only.cumulative_share, only.vital_few) == (1.0, 1.0, True)


def test_pareto_marks_the_shortest_prefix_reaching_80_percent() -> None:
    ranked = rank_issues([_issue("c", 10), _issue("a", 50), _issue("b", 30), _issue("d", 10)])
    assert [i.issue_id for i in ranked] == ["a", "b", "c", "d"]
    assert [i.vital_few for i in ranked] == [True, True, False, False]  # 50% + 30% = 80%
    assert ranked[1].cumulative_share == pytest.approx(0.8)
    assert sum(i.share for i in ranked) == pytest.approx(1.0)


def test_pareto_ties_go_to_events_then_id() -> None:
    ranked = rank_issues([_issue("b", 10, events=1), _issue("a", 10, events=1), _issue("c", 10, 5)])
    assert [i.issue_id for i in ranked] == ["c", "a", "b"]


def test_zero_impact_has_no_vital_few() -> None:
    ranked = rank_issues([_issue("a", 0), _issue("b", 0)])
    assert not any(i.vital_few for i in ranked)
    assert all(i.share == 0 for i in ranked)


def test_rank_of_nothing() -> None:
    assert rank_issues([]) == ()


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("issues")
    app = []
    for minute in range(0, 60):
        app.append(line(minute * M, "INFO", f"GET /api/items/{minute} served in {minute}ms"))
        if minute % 2 == 0:
            app.append(line(minute * M, "ERROR", f"Connection to 10.0.0.{minute} refused by peer"))
        if minute % 3 == 0:
            app.append(
                line(minute * M, "ERROR", f"Connection to 10.0.1.{minute} refused by remote peer")
            )
        if minute % 10 == 0:
            app.append(line(minute * M, "WARN", f"Cache miss rate high for region r{minute}"))
    app.append(line(30 * M, "INFO", "error count=0 after retry"))
    other = [line(5 * M, "ERROR", "Connection to 10.0.9.9 refused by peer")]
    return build_store(root, {"app.log": app, "other.log": other})


@pytest.fixture
def conn(store: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = connect(store)
    yield connection
    connection.close()


def test_split_templates_become_one_issue(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_issues(conn)
    refused = [i for i in report.issues if "refused" in i.title]
    assert len(refused) == 1
    issue = refused[0]
    assert len(issue.template_ids) == 2
    assert issue.events == 30 + 20 + 1
    assert "remote" not in issue.title  # the member template with the most events
    assert issue.sources == 2
    assert issue.impact == 51 * 5


def test_info_only_templates_are_not_issues(conn: duckdb.DuckDBPyConnection) -> None:
    titles = [i.title for i in find_issues(conn).issues]
    assert not any("GET" in t or "error count" in t for t in titles)
    assert len(titles) == 2


def test_report_totals_and_pareto(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_issues(conn)
    assert report.total_events == 60 + 30 + 20 + 6 + 1 + 1
    assert report.candidate_events == 51 + 6
    assert report.issues[0].vital_few
    assert report.vital_few_count == 1  # 255 of 261 impact
    assert report.issues[1].title.startswith("Cache miss")


def test_persistence_and_evidence(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_issues(conn)
    refused = report.issues[0]
    assert refused.persistence is not None and 0 < refused.persistence <= 1
    cache = report.issues[1]
    assert cache.first_seen == T0 and cache.last_seen == T0 + 50 * M
    evidence = refused.evidence
    assert evidence[0].timestamp == T0
    assert evidence[-1].timestamp == refused.last_seen
    assert all("refused" in ref.message for ref in evidence)
    assert all(ref.location.split(":")[-1].isdigit() for ref in evidence)


def test_min_events_and_window(conn: duckdb.DuckDBPyConnection) -> None:
    assert len(find_issues(conn, min_events=10).issues) == 1
    late = find_issues(conn, window=TimeWindow(since=T0 + 45 * M))
    refused = late.issues[0]
    assert refused.events == sum((m % 2 == 0) + (m % 3 == 0) for m in range(45, 60))
    empty = find_issues(conn, window=TimeWindow(since=T0 + timedelta(days=1)))
    assert empty.issues == () and empty.total_events == 0
    assert "no events" in " ".join(empty.notes)


def test_template_issue_map(conn: duckdb.DuckDBPyConnection) -> None:
    report = find_issues(conn)
    mapping = template_issue_map(report)
    assert len(mapping) == 3
    assert set(mapping.values()) == {i.issue_id for i in report.issues}


def test_group_source_counts(conn: duckdb.DuckDBPyConnection) -> None:
    ids = [s.template_id for s in template_stats(conn, TimeWindow()) if "refused" in s.template]
    assert group_source_counts(conn, [ids, ids[:1]], TimeWindow())[0] == 2
    assert group_source_counts(conn, [], TimeWindow()) == []


def test_untimestamped_issues(tmp_path: Path) -> None:
    store = build_store(
        tmp_path, {"plain.log": ["job 1 failed hard", "job 2 failed hard", "all good here"]}
    )
    connection = connect(store)
    try:
        report = find_issues(connection)
    finally:
        connection.close()
    (issue,) = report.issues
    assert issue.by_level == {"UNKNOWN": 2}
    assert issue.impact == 4
    assert issue.persistence is None
    assert any("no timestamp" in note for note in report.notes)
