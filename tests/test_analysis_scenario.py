"""Planted issues and anomalies are found; the clean twin raises nothing; any format works.

Logs come from ``tests/fixtures/make_analysis_fixtures.py``.
"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest
from tests.fixtures.make_analysis_fixtures import (
    BURST_AT,
    DROP_AT,
    NEW_AT,
    write_store_logs,
)

from loglens.analysis.anomalies import AnomalyReport, find_anomalies
from loglens.analysis.issues import IssuesReport, find_issues
from loglens.pipeline import ParseOptions, run_parse
from loglens.store import connect

FIVE_MINUTES = timedelta(minutes=5)


def _store(root: Path, scenario: bool, fmt: str) -> Path:
    name = f"{'scenario' if scenario else 'background'}-{fmt}"
    logs = write_store_logs(root / f"logs-{name}", scenario, fmt)
    output = root / name
    run_parse([logs], output, ParseOptions(workers=1))
    return output


@pytest.fixture(scope="module")
def stores(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("scenario")
    return {
        f"{'scenario' if s else 'background'}-{fmt}": _store(root, s, fmt)
        for s in (True, False)
        for fmt in ("text", "json")
    }


@pytest.fixture(scope="module")
def reports(stores: dict[str, Path]) -> dict[str, tuple[IssuesReport, AnomalyReport]]:
    result = {}
    for name, store in stores.items():
        connection = connect(store)
        try:
            result[name] = (find_issues(connection), find_anomalies(connection))
        finally:
            connection.close()
    return result


@pytest.fixture
def scenario_conn(stores: dict[str, Path]) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = connect(stores["scenario-text"])
    yield connection
    connection.close()


def test_top_issue_is_the_payment_timeout(reports: dict) -> None:  # type: ignore[type-arg]
    issues, _ = reports["scenario-text"]
    top = issues.issues[0]
    assert top.title.startswith("Payment gateway timeout")
    assert top.events == 24 + 200
    assert top.vital_few
    assert 1 <= issues.vital_few_count < len(issues.issues)


def test_refused_variants_are_one_issue(reports: dict) -> None:  # type: ignore[type-arg]
    issues, _ = reports["scenario-text"]
    refused = [i for i in issues.issues if "refused" in i.title]
    assert len(refused) == 1
    assert len(refused[0].template_ids) == 2
    assert refused[0].events == 120


def test_burst_is_a_spike_at_06_30(reports: dict) -> None:  # type: ignore[type-arg]
    _, anomalies = reports["scenario-text"]
    assert anomalies.grid is not None and anomalies.grid.bucket == FIVE_MINUTES
    spikes = anomalies.by_kind("spike")
    template = [a for a in spikes if a.series == "template"]
    assert len(template) == 1
    assert template[0].start == BURST_AT
    assert "Payment gateway timeout" in template[0].summary
    assert any(a.series == "errors" and a.start == BURST_AT for a in spikes)


def test_traffic_drop_is_a_level_shift_at_noon(reports: dict) -> None:  # type: ignore[type-arg]
    _, anomalies = reports["scenario-text"]
    total = [a for a in anomalies.by_kind("level_shift") if a.series == "total"]
    assert len(total) == 1
    assert total[0].start is not None
    assert abs(total[0].start - DROP_AT) <= FIVE_MINUTES
    assert "down" in total[0].summary


def test_disk_quota_is_a_new_pattern_from_18_00(reports: dict) -> None:  # type: ignore[type-arg]
    _, anomalies = reports["scenario-text"]
    new = anomalies.by_kind("new_pattern")
    assert "Disk quota exceeded" in new[0].summary
    assert new[0].start is not None and NEW_AT <= new[0].start < NEW_AT + FIVE_MINUTES
    assert new[0].issue_id is not None


def test_skipped_step_is_a_rare_transition(reports: dict) -> None:  # type: ignore[type-arg]
    _, anomalies = reports["scenario-text"]
    rare = anomalies.by_kind("rare_transition")
    assert len(rare) == 1
    assert "skipped from cache" in rare[0].summary
    assert rare[0].evidence[0].message.startswith("Job 900 step load skipped")


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_background_twin_raises_no_alarm(reports: dict, fmt: str) -> None:  # type: ignore[type-arg]
    _, anomalies = reports[f"background-{fmt}"]
    assert anomalies.by_kind("spike") == []
    assert anomalies.by_kind("level_shift") == []
    assert anomalies.by_kind("rare_transition") == []


@pytest.mark.parametrize("scenario", ["scenario", "background"])
def test_free_text_and_json_give_the_same_analysis(reports: dict, scenario: str) -> None:  # type: ignore[type-arg]
    text_issues, text_anomalies = reports[f"{scenario}-text"]
    json_issues, json_anomalies = reports[f"{scenario}-json"]
    assert [i.title for i in text_issues.issues] == [i.title for i in json_issues.issues]
    assert [i.events for i in text_issues.issues] == [i.events for i in json_issues.issues]
    assert {(a.kind, a.series, a.start) for a in text_anomalies.anomalies} == {
        (a.kind, a.series, a.start) for a in json_anomalies.anomalies
    }
