"""Tests for the anomaly evaluation (benchmarks/anomaly_eval.py): metrics, labels, smoke run."""

import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

EVAL = runpy.run_path(
    str(Path(__file__).parents[1] / "benchmarks" / "anomaly_eval.py"), run_name="anomaly_eval"
)


def test_score_counts_true_and_false_positives() -> None:
    labels = [False, True, True, False, False]
    metrics = EVAL["score"]({2, 4}, labels)  # line numbers are 1-based
    assert (metrics.tp, metrics.fp, metrics.fn) == (1, 1, 1)
    assert metrics.precision == 0.5 and metrics.recall == 0.5 and metrics.f1 == 0.5


def test_score_of_no_prediction() -> None:
    metrics = EVAL["score"](set(), [True, False])
    assert (metrics.tp, metrics.fp, metrics.fn) == (0, 0, 1)
    assert metrics.precision == 0.0 and metrics.f1 == 0.0


def test_random_baseline_scores_the_base_rate() -> None:
    metrics = EVAL["random_baseline"]([True, False, False, False])
    assert metrics.precision == pytest.approx(0.25)
    assert metrics.recall == pytest.approx(0.25)
    assert metrics.f1 == pytest.approx(0.25)


def test_strip_labels_removes_the_first_token(tmp_path: Path) -> None:
    src = tmp_path / "in.log"
    src.write_text("- 111 normal line\nKERNDTLB 112 bad line\nlonely\n", encoding="utf-8")
    dst = tmp_path / "out.log"
    labels = EVAL["strip_labels"](src, dst)
    assert labels == [False, True, True]
    assert dst.read_text(encoding="utf-8") == "111 normal line\n112 bad line\n\n"


def _labeled_log(path: Path) -> None:
    """Three hours of synthetic labeled lines: steady INFO, one labeled error burst."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    lines = []
    for minute in range(180):
        at = start + timedelta(minutes=minute)
        stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(3):
            lines.append(f"- {stamp} INFO request {minute * 3 + i} served")
        if minute == 90:
            for i in range(30):
                lines.append(f"APPERR {stamp} ERROR disk {i} failed hard now")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_smoke_run_writes_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "labeled.log"
    _labeled_log(log)
    results_md = tmp_path / "ANOMALY_RESULTS.md"
    history = tmp_path / "anomaly_history.json"
    main: Any = EVAL["main"]
    code = main(
        ["--log", str(log), "--results", str(results_md), "--history", str(history), "--label", "t"]
    )
    assert code == 0
    text = results_md.read_text(encoding="utf-8")
    for row in ("spike", "new_pattern", "rare_transition", "all detectors", "ERROR/FATAL lines"):
        assert row in text
    runs = json.loads(history.read_text(encoding="utf-8"))
    assert runs[-1]["label"] == "t"
    combined = runs[-1]["results"][0]["detectors"]["all detectors"]
    assert combined["tp"] == 30 and combined["recall"] == 1.0
