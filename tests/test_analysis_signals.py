"""Issue signals: which templates are candidate issues, and how much they weigh."""

from pathlib import Path

import pytest

from loglens.analysis.signals import Signals, SignalsError, load_signals


@pytest.fixture(scope="module")
def signals() -> Signals:
    return load_signals()


@pytest.mark.parametrize(
    "template",
    [
        "Connection timed out after <NUM> ms",
        "Exception in thread <*>",
        "Login FAILED for user <*>",
        "disk corrupted: <PATH>",
        "panic: runtime error",
    ],
)
def test_default_keywords_match_failure_words(signals: Signals, template: str) -> None:
    assert signals.matches_keyword(template)


@pytest.mark.parametrize("template", ["errorless shutdown", "terror alert", "aborting nothing"])
def test_keywords_match_whole_words_only(signals: Signals, template: str) -> None:
    assert not signals.matches_keyword(template)


def test_info_only_template_is_never_a_candidate(signals: Signals) -> None:
    assert not signals.is_candidate({"INFO": 10}, "error count=<NUM>")


def test_warn_error_fatal_templates_are_candidates(signals: Signals) -> None:
    assert signals.is_candidate({"INFO": 3, "WARN": 1}, "cache is cold")
    assert signals.is_candidate({"ERROR": 1}, "something happened")
    assert signals.is_candidate({"FATAL": 1}, "bye")


def test_unknown_level_needs_a_keyword(signals: Signals) -> None:
    assert signals.is_candidate({"UNKNOWN": 2}, "job <NUM> failed")
    assert not signals.is_candidate({"UNKNOWN": 2}, "job <NUM> started")


def test_impact_weighs_levels(signals: Signals) -> None:
    assert signals.impact({"ERROR": 3, "WARN": 2, "INFO": 100}, "x") == 17
    assert signals.impact({"FATAL": 1}, "x") == 10


def test_impact_counts_unknown_only_with_a_keyword(signals: Signals) -> None:
    assert signals.impact({"UNKNOWN": 4}, "upload failed") == 8
    assert signals.impact({"UNKNOWN": 4}, "upload done") == 0


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "signals.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_override_file_replaces_defaults(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "failure_keywords: [kaput]\nlevel_weights: {ERROR: 2}\nunknown_keyword_weight: 1\n",
    )
    custom = load_signals(path)
    assert custom.matches_keyword("engine kaput")
    assert not custom.matches_keyword("engine failed")
    assert custom.impact({"ERROR": 1, "WARN": 5}, "x") == 2


@pytest.mark.parametrize(
    "text",
    [
        "failure_keywords: [a\n",  # invalid YAML
        "level_weights: {ERROR: 1}\nunknown_keyword_weight: 1\n",  # no keywords
        "failure_keywords: [a]\nlevel_weights: {ERROR: high}\nunknown_keyword_weight: 1\n",
        "failure_keywords: [a]\nlevel_weights: {SEVERE: 1}\nunknown_keyword_weight: 1\n",
        "failure_keywords: ['(']\nlevel_weights: {ERROR: 1}\nunknown_keyword_weight: 1\n",
        "failure_keywords: [a]\nlevel_weights: {ERROR: 1}\nunknown_keyword_weight: -1\n",
        "- just a list\n",
    ],
)
def test_invalid_files_raise_signals_error_naming_the_file(tmp_path: Path, text: str) -> None:
    path = _write(tmp_path, text)
    with pytest.raises(SignalsError, match=r"signals.yaml"):
        load_signals(path)


def test_missing_file_raises_signals_error(tmp_path: Path) -> None:
    with pytest.raises(SignalsError, match=r"missing.yaml"):
        load_signals(tmp_path / "missing.yaml")
