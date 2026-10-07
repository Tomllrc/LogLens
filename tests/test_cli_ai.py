"""CLI tests for `formats`, `doctor`, `learn` and `parse --learn` (FakeClient only)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from tests.ai_fixtures import CORRECT_PLC, WRONG_PLC, write_plc_log
from tests.test_formats_registry import PLC_FORMAT, write_format
from typer.testing import CliRunner

from loglens.ai.llm import FakeClient, LLMResponse, QuotaExhaustedError, ResilientClient, fake_json
from loglens.cli import ai_cmd, app
from loglens.config import Settings
from loglens.formats import cached_registry

runner = CliRunner()
WIDE = {"COLUMNS": "250"}
PLC_REGEX = CORRECT_PLC["regex"][0]


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    cached_registry.cache_clear()
    yield
    cached_registry.cache_clear()


@pytest.fixture
def home(_isolated_environment: Path) -> Path:
    return _isolated_environment


@pytest.fixture
def plc_log(tmp_path: Path) -> Path:
    return write_plc_log(tmp_path / "plc.log")


def use_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """Make every AI command use ``fake`` behind the real resilience layer (no sleeping)."""

    def make(settings: Settings, tracker: object) -> ResilientClient:
        return ResilientClient(fake, tracker=tracker, sleep=lambda _: None)  # type: ignore[arg-type]

    monkeypatch.setattr(ai_cmd, "make_client", make)


# --- formats ----------------------------------------------------------------------------------


def test_formats_list_shows_built_ins_user_formats_and_errors(home: Path) -> None:
    write_format(home / "formats", PLC_FORMAT)
    write_format(home / "formats", {"name": "broken"}, name="broken")
    result = runner.invoke(app, ["formats", "list"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "syslog_rfc3164" in result.output
    assert "built-in" in result.output
    assert "plc_alarm_log" in result.output
    assert "invalid:" in result.output and "broken.yaml" in result.output


def test_formats_dir_option(tmp_path: Path) -> None:
    write_format(tmp_path / "extra", PLC_FORMAT)
    result = runner.invoke(app, ["formats", "list", "-f", str(tmp_path / "extra")], env=WIDE)
    assert "plc_alarm_log" in result.output


def test_formats_test_reports_match_rate_examples_and_misses(tmp_path: Path, plc_log: Path) -> None:
    with plc_log.open("a", encoding="utf-8") as handle:
        handle.write("[garbage] line one\n[garbage] line two\n")  # starts of new records
    path = write_format(tmp_path / "formats", PLC_FORMAT)
    result = runner.invoke(app, ["formats", "test", str(path), str(plc_log)], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "matches 99.0% of 202 records" in result.output
    assert result.output.count("component") == 5
    assert "code" in result.output
    assert "do not match" in result.output and "[garbage] line two" in result.output


def test_formats_test_rejects_invalid_files(tmp_path: Path, plc_log: Path) -> None:
    path = write_format(tmp_path, {**PLC_FORMAT, "regex": ["(?P<x>["]}, name="bad")
    result = runner.invoke(app, ["formats", "test", str(path), str(plc_log)])
    assert result.exit_code == 1
    assert "Invalid format file" in result.output
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    good = write_format(tmp_path, PLC_FORMAT)
    assert runner.invoke(app, ["formats", "test", str(good), str(empty)]).exit_code == 1


def test_formats_new_wizard(home: Path, plc_log: Path) -> None:
    answers = [
        "Bad Name",  # rejected
        "plc_alarm_log",
        "Alarm log of an industrial PLC.",
        "(?P<message>[",  # does not compile
        r"\[PLC",  # no named groups
        PLC_REGEX,
        "y",  # keep it
        "%d/%m/%Y %H:%M:%S.%f",  # no generic hint fits day-first dates
        "error",  # ALM
        "LOUD",  # FLT: rejected
        "fatal",  # FLT
    ]
    result = runner.invoke(
        app, ["formats", "new", "--sample", str(plc_log)], input="\n".join(answers) + "\n"
    )
    assert result.exit_code == 0, result.output
    assert "Does not compile" in result.output
    assert "Add named groups" in result.output
    assert "Matches 100.0%" in result.output
    saved = yaml.safe_load((home / "formats" / "plc_alarm_log.yaml").read_text("utf-8"))
    assert saved["timestamp_format"] == ["%d/%m/%Y %H:%M:%S.%f"]
    assert saved["level"]["map"] == {"WRN": "WARN", "INF": "INFO", "ALM": "ERROR", "FLT": "FATAL"}
    assert "Use one of" in result.output
    assert saved["metadata"] == {"generated_by": "wizard"}
    assert len(saved["sample_lines"]) == 5
    listed = runner.invoke(app, ["formats", "list"], env=WIDE)
    assert "plc_alarm_log" in listed.output


# --- doctor -----------------------------------------------------------------------------------


def test_doctor_without_key_never_calls(home: Path) -> None:
    result = runner.invoke(app, ["doctor", "--no-call"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "GEMINI_API_KEY" in result.output and "not set" in result.output
    assert "gemini-3.8-flash" in result.output
    assert "Privacy" in result.output


def test_doctor_never_prints_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value-123")
    result = runner.invoke(app, ["doctor", "--no-call"], env=WIDE)
    assert "secret-value-123" not in result.output
    assert "set" in result.output


def test_doctor_test_call(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake(monkeypatch, FakeClient([LLMResponse(text="OK", usage=fake_json(0).usage)]))
    result = runner.invoke(app, ["doctor"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "Test call OK" in result.output
    assert "10 input, 5 output (1 call" in result.output


def test_doctor_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake(monkeypatch, FakeClient([QuotaExhaustedError("daily quota is exhausted")]))
    result = runner.invoke(app, ["doctor"], env=WIDE)
    assert result.exit_code == 1
    assert "daily quota is exhausted" in result.output


def test_doctor_with_ai_off_skips_the_call() -> None:
    result = runner.invoke(app, ["--no-ai", "doctor"], env=WIDE)
    assert result.exit_code == 0
    assert "Test call" not in result.output


# --- learn ------------------------------------------------------------------------------------


def test_learn_wrong_then_correct(
    monkeypatch: pytest.MonkeyPatch, home: Path, plc_log: Path
) -> None:
    fake = FakeClient([fake_json(WRONG_PLC), fake_json(CORRECT_PLC)])
    use_fake(monkeypatch, fake)
    result = runner.invoke(app, ["learn", str(plc_log), "--yes"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "Match rate" in result.output and "100.0%" in result.output
    assert "Saved" in result.output
    assert "20 input, 10 output (2 calls, 0 cached)" in result.output
    assert (home / "formats" / "plc_alarm_log.yaml").is_file()


def test_learn_asks_before_sending(monkeypatch: pytest.MonkeyPatch, plc_log: Path) -> None:
    fake = FakeClient([fake_json(CORRECT_PLC)])
    use_fake(monkeypatch, fake)
    result = runner.invoke(app, ["learn", str(plc_log)], input="n\n", env=WIDE)
    assert result.exit_code == 0
    assert "Privacy" in result.output
    assert "50 sample records will be sent" in result.output
    assert "nothing was sent" in result.output
    assert fake.requests == []


def test_learn_failures(monkeypatch: pytest.MonkeyPatch, plc_log: Path) -> None:
    use_fake(monkeypatch, FakeClient([fake_json(WRONG_PLC)] * 4))
    result = runner.invoke(app, ["learn", str(plc_log), "-y"], env=WIDE)
    assert result.exit_code == 1
    assert "nothing was saved" in result.output

    use_fake(monkeypatch, FakeClient([QuotaExhaustedError("daily quota is exhausted")]))
    result = runner.invoke(app, ["learn", str(plc_log), "-y"], env=WIDE)
    assert result.exit_code == 1
    assert "daily quota" in result.output and "AI tokens" in result.output


def test_learn_needs_ai(plc_log: Path) -> None:
    result = runner.invoke(app, ["--no-ai", "learn", str(plc_log), "-y"])
    assert result.exit_code == 1
    assert "AI is disabled" in result.output
    no_key = runner.invoke(app, ["learn", str(plc_log), "-y"])
    assert no_key.exit_code == 1
    assert "GEMINI_API_KEY" in no_key.output


# --- parse and detect with learned formats ----------------------------------------------------


def exotic_log(path: Path) -> Path:
    """Lines the generic parser cannot structure: no timestamp, level or component."""
    path.write_text(
        "".join(f"~~ unit {i % 7} >> valve open {i}% <<\n" for i in range(60)), encoding="utf-8"
    )
    return path


EXOTIC_FORMAT = {
    "name": "valve_log",
    "description": "Valve controller log.",
    "regex": [r"~~ unit (?P<component>\d+) >> (?P<message>.*) <<"],
    "timestamp_format": [],
}


def test_parse_suggests_learning_for_low_confidence_files(tmp_path: Path) -> None:
    log = exotic_log(tmp_path / "valves.log")
    result = runner.invoke(
        app, ["parse", str(log), "-o", str(tmp_path / "out"), "-w", "1"], env=WIDE
    )
    assert result.exit_code == 0, result.output
    assert "Low confidence" in result.output
    assert "loglens learn" in result.output


def test_parse_learn_then_reparse(
    monkeypatch: pytest.MonkeyPatch, home: Path, tmp_path: Path
) -> None:
    log = exotic_log(tmp_path / "valves.log")
    use_fake(monkeypatch, FakeClient([fake_json(EXOTIC_FORMAT)]))
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["parse", str(log), "-o", str(out), "-w", "1", "--learn", "--yes"], env=WIDE
    )
    assert result.exit_code == 0, result.output
    assert "Re-parsing with the learned formats" in result.output
    assert "valve_log" in result.output
    assert (home / "formats" / "valve_log.yaml").is_file()

    detected = runner.invoke(app, ["detect", str(log)], env=WIDE)
    assert "valve_log" in detected.output


def test_parse_learn_without_ai_still_parses(tmp_path: Path) -> None:
    log = exotic_log(tmp_path / "valves.log")
    result = runner.invoke(
        app,
        ["--no-ai", "parse", str(log), "-o", str(tmp_path / "out"), "-w", "1", "--learn"],
        env=WIDE,
    )
    assert result.exit_code == 0, result.output
    assert "Events" in result.output
    assert "AI is disabled" in result.output
