"""`loglens issues`, `loglens anomalies` and `loglens compare`."""

import json
import re
from pathlib import Path

import pytest
from tests.analysis_helpers import build_store, two_hour_lines
from typer.testing import CliRunner

from loglens.cli import app

runner = CliRunner()
WIDE = {"COLUMNS": "200"}
SPLIT = "2026-03-01T01:00:00Z"


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_store(tmp_path_factory.mktemp("cli"), {"app.log": two_hour_lines(True, True)})


def test_issues_table(store: Path) -> None:
    result = runner.invoke(app, ["issues", str(store)], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "Connection to <IP> refused by peer" in result.output
    assert "account for" in result.output
    assert "Payment gateway timeout" in result.output


def test_issues_top_and_json(store: Path) -> None:
    result = runner.invoke(app, ["issues", str(store), "--top", "2", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data["issues"]) == 2
    first = data["issues"][0]
    assert {"issue_id", "title", "impact", "share", "vital_few", "evidence"} <= set(first)
    assert re.search(r"app\.log:\d+$", first["evidence"][0]["location"])
    assert data["total_events"] > 0


def test_issues_window(store: Path) -> None:
    result = runner.invoke(app, ["issues", str(store), "--since", SPLIT, "--json"])
    data = json.loads(result.output)
    assert not any(i["title"].startswith("Cache warm-up") for i in data["issues"])


def test_anomalies_table_and_json(store: Path) -> None:
    result = runner.invoke(app, ["anomalies", str(store)], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "new_pattern" in result.output or "New pattern" in result.output
    assert "Disk quota exceeded" in result.output
    data = json.loads(runner.invoke(app, ["anomalies", str(store), "--json"]).output)
    assert {"anomalies", "notes", "grid", "span"} <= set(data)
    kinds = {a["kind"] for a in data["anomalies"]}
    assert "new_pattern" in kinds


def test_anomalies_options(store: Path) -> None:
    result = runner.invoke(
        app,
        ["anomalies", str(store), "--kind", "new_pattern", "--bucket", "5m", "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {a["kind"] for a in data["anomalies"]} == {"new_pattern"}
    assert data["grid"]["bucket"] == 300.0


def test_compare_split(store: Path) -> None:
    result = runner.invoke(app, ["compare", str(store), "--split", SPLIT], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "Disk quota exceeded" in result.output
    assert "Cache warm-up failed" in result.output
    assert "Payment gateway timeout" in result.output
    data = json.loads(
        runner.invoke(app, ["compare", str(store), "--split", SPLIT, "--json"]).output
    )
    assert [c["title"].split(" ")[0] for c in data["new"]] == ["Disk"]
    assert data["changed"][0]["ratio"] > 2


def test_compare_two_stores(store: Path) -> None:
    result = runner.invoke(app, ["compare", str(store), str(store), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["new"] == [] and data["gone"] == [] and data["changed"] == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["issues", "{missing}"], "not a LogLens output directory"),
        (["anomalies", "{missing}"], "not a LogLens output directory"),
        (["compare", "{missing}", "--split", SPLIT], "not a LogLens output directory"),
        (["compare", "{store}"], "TARGET or --split"),
        (["compare", "{store}", "{store}", "--split", SPLIT], "TARGET or --split"),
        (["issues", "{store}", "--since", "yesterday"], "invalid time"),
        (["anomalies", "{store}", "--kind", "weird"], "unknown anomaly kind"),
        (["anomalies", "{store}", "--bucket", "5 minutes"], "invalid bucket"),
        (["issues", "{store}", "--signals", "{missing}"], "cannot read signals"),
    ],
)
def test_errors_exit_1_with_one_line(
    store: Path, tmp_path: Path, args: list[str], message: str
) -> None:
    filled = [a.format(store=store, missing=tmp_path / "missing") for a in args]
    result = runner.invoke(app, filled, env=WIDE)
    assert result.exit_code == 1, result.output
    assert message in result.output


def test_empty_store_reports_no_events(tmp_path: Path) -> None:
    store = build_store(tmp_path, {"empty.log": []})
    result = runner.invoke(app, ["issues", str(store)])
    assert result.exit_code == 0
    assert "no events" in result.output
    result = runner.invoke(app, ["anomalies", str(store)])
    assert result.exit_code == 0
    assert "No anomalies" in result.output
