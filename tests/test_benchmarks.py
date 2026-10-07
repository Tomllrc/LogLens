"""Tests for the Loghub benchmark's metrics and regression rule (benchmarks/loghub_bench.py)."""

import runpy
from pathlib import Path
from typing import Any

import pytest

BENCH = runpy.run_path(
    str(Path(__file__).parents[1] / "benchmarks" / "loghub_bench.py"), run_name="bench"
)


def test_grouping_accuracy_counts_only_exact_groups() -> None:
    ga = BENCH["grouping_accuracy"]
    assert ga(["a", "a", "b"], ["E1", "E1", "E2"]) == 1.0
    assert ga(["a", "a", "a"], ["E1", "E1", "E2"]) == 0.0  # over-merged: nobody is right
    assert ga(["a", "b", "c"], ["E1", "E1", "E2"]) == pytest.approx(1 / 3)  # split E1
    assert ga(["x", "y"], ["E1", "E2"]) == 1.0  # label names do not matter
    assert ga([], []) == 0.0


def test_normalize_template_maps_typed_placeholders() -> None:
    normalize = BENCH["normalize_template"]
    assert normalize("Connected to <IP>  in <DURATION>") == "Connected to <*> in <*>"
    assert normalize("blk_<*> ok") == "blk_<*> ok"


def _avg(ga: float) -> dict[str, float]:
    return {"grouping_accuracy": ga}


@pytest.mark.parametrize(
    ("previous", "current", "regression"),
    [(0.80, 0.79, False), (0.80, 0.78, False), (0.80, 0.7799, True), (0.70, 0.90, False)],
)
def test_regression_rule(previous: float, current: float, regression: bool) -> None:
    assert BENCH["is_regression"](_avg(previous), _avg(current)) is regression


def test_markdown_shows_change_since_baseline() -> None:
    result_type = BENCH["DatasetResult"]

    def result(ga: float) -> Any:
        return result_type("HDFS", 2000, ga, 0.5, 1.0, 1.0, None, None, 1000.0, 14, 15)

    history = [
        {
            "date": "2026-10-07T00:00:00",
            "commit": "abc",
            "label": "baseline",
            "averages": {"grouping_accuracy": 0.6, "parsing_accuracy": 0.5},
            "datasets": [{"dataset": "HDFS", "grouping_accuracy": 0.6, "parsing_accuracy": 0.5}],
        },
        {
            "date": "2026-10-08T00:00:00",
            "commit": "def",
            "label": "now",
            "averages": {"grouping_accuracy": 0.9, "parsing_accuracy": 0.5},
            "datasets": [],
        },
    ]
    text = BENCH["render_markdown"]([result(0.9)], "now", history)
    assert "| HDFS | 60.0 | 90.0 | 50.0 | 50.0 |" in text
    assert "Change since the baseline (baseline, commit abc)" in text
    assert "| n/a | n/a |" in text  # no level ground truth
