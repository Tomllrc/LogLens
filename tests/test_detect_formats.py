"""Tests for detect_format: scoring every candidate and choosing one."""

import re
from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loglens.detect import (
    DEFAULT_DETECTORS,
    FREE_TEXT,
    Detection,
    Detector,
    choose,
    detect_format,
    regex_detector,
    score_candidates,
)
from loglens.detect.text import FREE_TEXT_SCORE

JSON_LINES = [f'{{"level": "info", "n": {i}, "msg": "event {i}"}}' for i in range(10)]
SYSLOG = [f"Jan  1 10:00:0{i} host sshd[{i}]: session opened" for i in range(10)]


def test_every_candidate_is_scored_in_priority_order() -> None:
    detection = detect_format(JSON_LINES)
    assert list(detection.scores) == [d.name for d in DEFAULT_DETECTORS]
    assert all(0.0 <= s <= 1.0 for s in detection.scores.values())


def test_json_beats_csv_on_a_tie() -> None:
    detection = detect_format(JSON_LINES)
    assert detection.scores["csv"] == 1.0  # same keys: consistent commas
    assert detection == Detection("json", 1.0)


def test_known_format_beats_csv_on_a_tie() -> None:
    sample = [
        f"2026/01/01 10:00:0{i} [error] 12#0: *{i} failed, client: 10.0.0.{i}, server: x"
        for i in range(10)
    ]
    detection = detect_format(sample)
    assert detection.scores["csv"] == 1.0
    assert detection.format_name == "nginx_error"


def test_highest_score_wins_over_priority() -> None:
    sample = [*SYSLOG[:8], "a,b,c", "d,e,f"]
    assert detect_format(sample).format_name == "syslog_rfc3164"
    rows = [f"{i},x,y" for i in range(9)] + [SYSLOG[0]]
    assert detect_format(rows) == Detection("csv", 0.9)


def test_below_threshold_is_free_text() -> None:
    mixed = JSON_LINES[:5] + SYSLOG[:5]
    detection = detect_format(mixed)
    assert detection == Detection(FREE_TEXT, FREE_TEXT_SCORE)
    assert detection.scores["json"] == 0.5
    assert detect_format(mixed, min_confidence=0.5).format_name == "json"


def test_mixed_file_with_a_dominant_format() -> None:
    sample = JSON_LINES[:8] + SYSLOG[:2]
    assert detect_format(sample) == Detection("json", 0.8)


def test_empty_sample_is_free_text() -> None:
    assert detect_format([]) == Detection(FREE_TEXT, 0.0)
    assert detect_format(["", "   "]) == Detection(FREE_TEXT, 0.0)


def test_custom_detectors_extend_the_registry() -> None:
    custom = regex_detector("my_app", re.compile(r"MYAPP\|"))
    sample = [f"MYAPP|{i}|ok" for i in range(5)]
    assert detect_format(sample).format_name == "csv"  # pipe-delimited, 3 columns
    detection = detect_format(sample, detectors=(custom, *DEFAULT_DETECTORS))
    assert detection == Detection("my_app", 1.0)


def _boom(sample: Sequence[str]) -> float:
    raise RuntimeError("plugin bug")


def test_a_failing_detector_scores_zero_and_is_reported() -> None:
    errors: list[tuple[str, str]] = []
    detectors = (Detector("broken", _boom), *DEFAULT_DETECTORS)
    detection = detect_format(
        JSON_LINES, detectors=detectors, on_error=lambda name, e: errors.append((name, str(e)))
    )
    assert detection.format_name == "json"
    assert detection.scores["broken"] == 0.0
    assert errors == [("broken", "plugin bug")]
    assert score_candidates(JSON_LINES, detectors)["broken"] == 0.0  # no handler: silent


@pytest.mark.parametrize(
    ("raw", "clamped"), [(1.5, 1.0), (-0.2, 0.0), (float("nan"), 0.0), (0.7, 0.7)]
)
def test_out_of_range_scores_are_clamped(raw: float, clamped: float) -> None:
    detector = Detector("odd", lambda _sample: raw)
    assert score_candidates(["x"], (detector,)) == {"odd": clamped}


def test_non_numeric_score_counts_as_error() -> None:
    detector = Detector("odd", lambda _sample: "high")  # type: ignore[arg-type,return-value]
    errors: list[str] = []
    scores = score_candidates(["x"], (detector,), lambda name, _e: errors.append(name))
    assert scores == {"odd": 0.0}
    assert errors == ["odd"]


def test_choose_ties_go_to_the_first_listed() -> None:
    assert choose({"a": 0.9, "b": 0.9, FREE_TEXT: 0.1}).format_name == "a"
    assert choose({"b": 0.9, "a": 0.9}).format_name == "b"


def test_choose_without_free_text_candidate() -> None:
    assert choose({"a": 0.1}) == Detection(FREE_TEXT, FREE_TEXT_SCORE)
    assert choose({}) == Detection(FREE_TEXT, FREE_TEXT_SCORE)
    assert choose({"a": 0.0}, min_confidence=0.0).format_name == FREE_TEXT


_printable = st.text(st.characters(exclude_categories=("Cc", "Cs")), max_size=80)
_records = st.one_of(
    _printable,
    st.sampled_from([*JSON_LINES[:2], *SYSLOG[:2], "a,b,c", "k=v x=y", "<a/>", "", "  "]),
)


@given(sample=st.lists(_records, max_size=30))
def test_detection_is_bounded_and_deterministic(sample: list[str]) -> None:
    first = detect_format(sample)
    assert 0.0 <= first.confidence <= 1.0
    assert all(0.0 <= s <= 1.0 for s in first.scores.values())
    assert first.format_name in first.scores
    assert detect_format(sample) == first
    assert first.scores == detect_format(list(sample)).scores
