"""Tests for the AI-taught parser, with a scripted FakeClient (no API calls)."""

from pathlib import Path

import pytest
import yaml
from tests.ai_fixtures import CORRECT_PLC, PARTIAL_PLC, WRONG_PLC, plc_lines, write_plc_log

from loglens.ai.format_learner import (
    LEARNED_FORMAT_SCHEMA,
    SAMPLE_LINES_KEPT,
    LearnCancelled,
    build_request,
    learn_format,
    safe_name,
    to_definition,
    validate,
)
from loglens.ai.llm import AIError, FakeClient, LLMResponse, ProviderError, Usage, fake_json
from loglens.formats import load_registry
from loglens.parsers.textformats import load_text_format
from loglens.split import Record


@pytest.fixture
def plc_log(tmp_path: Path) -> Path:
    return write_plc_log(tmp_path / "plc.log")


def test_wrong_regex_then_correct_one(plc_log: Path, tmp_path: Path) -> None:
    fake = FakeClient([fake_json(WRONG_PLC), fake_json(CORRECT_PLC)])
    formats_dir = tmp_path / "formats"

    result = learn_format(plc_log, fake, formats_dir=formats_dir)

    assert result.attempts == 2
    assert result.validation.ok
    assert result.validation.match_rate == 1.0
    assert result.validation.timestamp_rate == 1.0
    assert result.validation.level_rate == 1.0
    assert result.usage == Usage(20, 10)
    retry_prompt = fake.requests[1].messages[0].text or ""
    assert "Your previous definition was" in retry_prompt
    assert "Records it did not match" in retry_prompt
    assert plc_lines()[0] in retry_prompt or "[PLC" in retry_prompt.split("failing", 1)[1]
    assert fake.requests[0].response_schema == LEARNED_FORMAT_SCHEMA

    assert result.path == formats_dir / "plc_alarm_log.yaml"
    saved = yaml.safe_load(result.path.read_text(encoding="utf-8"))
    assert saved["metadata"]["generated_by"] == "ai"
    assert saved["metadata"]["model"] == "fake-model"
    assert saved["metadata"]["match_rate"] == 1.0
    assert saved["metadata"]["generated_at"]
    assert saved["priority"] == 90
    assert 1 <= len(saved["sample_lines"]) <= SAMPLE_LINES_KEPT

    text_format = load_text_format(result.path)
    event = load_registry([formats_dir]).create_parser("plc_alarm_log")
    assert event is not None
    parsed = event.parse(Record("plc.log", 1, 1, plc_lines()[0]))
    assert parsed.error is None
    assert parsed.component is not None and parsed.component.startswith("PLC")
    assert parsed.level in {"ERROR", "WARN", "INFO", "FATAL"}
    assert "code" in parsed.extra
    assert text_format.metadata["generated_by"] == "ai"


def test_first_answer_good_enough_means_one_call(plc_log: Path) -> None:
    fake = FakeClient([fake_json(CORRECT_PLC)])
    result = learn_format(plc_log, fake, formats_dir=None)
    assert result.attempts == 1
    assert result.path is None  # nothing saved without a folder
    assert result.text_format is not None


def test_partial_match_is_retried_and_best_kept(plc_log: Path, tmp_path: Path) -> None:
    fake = FakeClient([fake_json(PARTIAL_PLC), fake_json(WRONG_PLC)])
    result = learn_format(plc_log, fake, formats_dir=tmp_path, max_retries=1)
    assert result.attempts == 2
    assert 0.2 < result.validation.match_rate < 0.95
    assert not result.validation.ok
    assert result.definition is not None and "ALM|WRN" in result.definition["regex"][0]
    assert result.path is not None  # best effort is saved, with its real match rate
    saved = yaml.safe_load(result.path.read_text(encoding="utf-8"))
    assert saved["metadata"]["match_rate"] == round(result.validation.match_rate, 4)


def test_retries_stop_after_the_limit(plc_log: Path, tmp_path: Path) -> None:
    fake = FakeClient([fake_json(WRONG_PLC)] * 4)
    result = learn_format(plc_log, fake, formats_dir=tmp_path)
    assert result.attempts == 4  # one call and 3 retries
    assert result.validation.errors
    assert result.path is None
    assert result.text_format is None


def test_nothing_is_sent_without_confirmation(plc_log: Path) -> None:
    fake = FakeClient([fake_json(CORRECT_PLC)])
    shown: list[list[str]] = []

    def refuse(samples: list[str]) -> bool:
        shown.append(samples)
        return False

    with pytest.raises(LearnCancelled):
        learn_format(plc_log, fake, formats_dir=None, confirm=refuse)
    assert fake.requests == []
    assert len(shown[0]) == 50


def test_errors_from_the_model(plc_log: Path, tmp_path: Path) -> None:
    with pytest.raises(AIError, match="not a format definition"):
        learn_format(plc_log, FakeClient([fake_json(["a list"])]), formats_dir=None)
    with pytest.raises(ProviderError):
        learn_format(plc_log, FakeClient([LLMResponse(text="garbage")]), formats_dir=None)
    empty = tmp_path / "empty.log"
    empty.write_text("\n\n", encoding="utf-8")
    with pytest.raises(AIError, match="no records"):
        learn_format(empty, FakeClient(), formats_dir=None)


def test_name_option_and_built_in_names_are_protected(plc_log: Path, tmp_path: Path) -> None:
    fake = FakeClient([fake_json({**CORRECT_PLC, "name": "syslog_rfc3164"})])
    result = learn_format(plc_log, fake, formats_dir=tmp_path)
    assert result.definition is not None
    assert result.definition["name"] == "syslog_rfc3164_learned"

    fake = FakeClient([fake_json(CORRECT_PLC)])
    named = learn_format(plc_log, fake, formats_dir=tmp_path, name="My PLC!")
    assert named.path == tmp_path / "my_plc.yaml"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Game Server", "game_server"), ("42log", "format_42log"), ("!!!", "learned_format")],
)
def test_safe_name(raw: str, expected: str) -> None:
    assert safe_name(raw) == expected


def test_to_definition_drops_invalid_parts() -> None:
    data = {
        "regex": ["", r"(?P<message>.*)"],
        "timestamp_format": [""],
        "level_map": [{"raw": "X", "level": "LOUD"}, "junk"],
    }
    definition = to_definition(data, ["a line"])
    assert definition["regex"] == [r"(?P<message>.*)"]
    assert "timestamp_format" not in definition
    assert "level" not in definition
    assert definition["name"] == "learned_format"
    assert definition["sample_lines"] == ["a line"]


def test_to_definition_keeps_multiline_start() -> None:
    data = {**CORRECT_PLC, "record_start": r"\[PLC"}
    definition = to_definition(data, plc_lines(3))
    assert definition["record_start"] == r"\[PLC"
    assert definition["level"]["map"]["ALM"] == "ERROR"


def test_validate_reports_unusable_definitions() -> None:
    bad_regex = to_definition({**CORRECT_PLC, "regex": ["(?P<message>[unclosed"]}, ["x"])
    assert validate(bad_regex, ["x"]).errors
    bad_hint = to_definition({**CORRECT_PLC, "timestamp_format": ["no pattern"]}, plc_lines(5))
    validation = validate(bad_hint, plc_lines(5))
    assert validation.errors and not validation.ok


def test_validate_measures_timestamps_and_levels() -> None:
    no_levels = {**CORRECT_PLC, "level_map": []}
    validation = validate(to_definition(no_levels, plc_lines(20)), plc_lines(20))
    assert validation.match_rate == 1.0
    assert validation.level_rate == 0.0
    wrong_hint = {**CORRECT_PLC, "timestamp_format": ["%m/%d/%Y %H:%M:%S.%f"]}
    validation = validate(to_definition(wrong_hint, plc_lines(20)), plc_lines(20))
    assert validation.timestamp_rate == 0.0  # day 14 is no month


def test_build_request_cuts_long_samples() -> None:
    request = build_request(["x" * 5000])
    assert (request.messages[0].text or "").count("x") == 600
    assert request.temperature == 0.0
