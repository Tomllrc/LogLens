"""Tests for loading and validating YAML format definitions, and their wiring."""

import re
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from loglens.detect import DEFAULT_DETECTORS, detect_format
from loglens.ingest import Line
from loglens.parsers import TEXT_FORMATS, FormatDefinitionError, TextFormatParser
from loglens.parsers.textformats import (
    LevelRule,
    build_text_format,
    load_text_format,
    load_text_formats,
    unquote,
)
from loglens.parsers.timestamps import parse_timestamp
from loglens.split import Record, SplitOptions, iter_records, learn_signature
from loglens.split.records import builtin_declared_starts
from loglens.split.signature import DeclaredStart

MINIMAL: dict[str, Any] = {
    "name": "my_app",
    "description": "Lines of my app.",
    "regex": r"(?P<level>[A-Z]+) (?P<message>.*)",
    "sample_lines": ["INFO started"],
}


def _build(**changes: Any) -> Any:
    data = {**MINIMAL, **changes}
    return build_text_format({k: v for k, v in data.items() if v is not None}, source="t.yaml")


def test_minimal_definition() -> None:
    fmt = _build()
    assert (fmt.name, fmt.priority, fmt.level, fmt.timestamp_formats) == ("my_app", 100, None, ())
    event = TextFormatParser(fmt).parse(Record("f", 1, 1, "WARN disk full"))
    assert (event.level_raw, event.level, event.message) == ("WARN", None, "disk full")


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"colour": "red"}, "unknown keys: colour"),
        ({"name": None}, "name is required"),
        ({"name": "My-App"}, "must be snake_case"),
        ({"description": "  "}, "description is required"),
        ({"priority": "high"}, "priority must be an integer"),
        ({"flags": ["UNICODE"]}, "unknown flag 'UNICODE'"),
        ({"regex": "(?P<a>"}, "regex does not compile"),
        ({"regex": [], "sample_lines": ["x"]}, "regex must be a string or a non-empty list"),
        ({"regex": "(.*)", "sample_lines": ["x"]}, "regex must have named groups"),
        ({"timestamp_format": "YYYY-MM-DD"}, "neither a keyword nor a strptime pattern"),
        ({"level": {"group": "severity", "map": {"INFO": "INFO"}}}, "'severity' is not a group"),
        ({"level": {"map": {"INFO": "INFORMATION"}}}, "must map to one of TRACE"),
        ({"level": {"map": {}}}, "level.map must be a non-empty mapping"),
        ({"level": {"transform": "rot13", "map": {"a": "INFO"}}}, "unknown level transform"),
        ({"level": {"map": {"a": "INFO"}, "colour": 1}}, "unknown level keys: colour"),
        ({"level": {"map": {"a": "INFO"}, "default": "LOUD"}}, "level.default must be one of"),
        ({"unquote": ["nope"]}, "unquote names unknown groups: nope"),
        ({"record_start": 42}, "record_start must be a regex string"),
        ({"extra_fields": {"regex": "(?P<k>.)=(?P<v>.)"}}, "needs 'key' and 'value' groups"),
        ({"extra_fields": "x"}, "extra_fields must be a mapping"),
        ({"sample_lines": []}, "sample_lines is required"),
        ({"sample_lines": ["lowercase does not match"]}, "sample line does not match"),
        ({"sample_lines": [{"expect": {}}]}, "each sample line is a string or a mapping"),
        (
            {"sample_lines": [{"line": "INFO x", "expect": {"a": [1]}}]},
            "must be a string or a number",
        ),
    ],
)
def test_invalid_definitions_are_rejected_with_a_clear_message(
    changes: dict[str, Any], error: str
) -> None:
    with pytest.raises(FormatDefinitionError, match=re.escape(error)) as caught:
        _build(**changes)
    assert str(caught.value).startswith("t.yaml: ")


def test_definition_must_be_a_mapping() -> None:
    with pytest.raises(FormatDefinitionError, match="must be a mapping"):
        build_text_format(["not", "a", "mapping"])


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


YAML = """\
name: {name}
description: My service's plain-text log lines.
priority: {priority}
regex: '(?P<timestamp>\\d{{2}}:\\d{{2}}:\\d{{2}}) \\[(?P<level>[a-z]+)\\] (?P<message>.*)'
timestamp_format: '%H:%M:%S'
level:
  map: {{info: INFO, oops: ERROR}}
sample_lines:
  - line: '09:12:01 [oops] it broke'
    expect: {{level: ERROR, message: it broke}}
"""


def test_a_new_format_needs_only_a_yaml_file(tmp_path: Path) -> None:
    """The whole point: drop a YAML file, get a parser and a detector, no Python."""
    _write(tmp_path, "my_service.yaml", YAML.format(name="my_service", priority=5))
    formats = load_text_formats(tmp_path)
    fmt = formats["my_service"]
    event = TextFormatParser(fmt).parse(Record("f", 1, 1, "09:12:03 [info] ready"))
    assert (event.timestamp_raw, event.level, event.message) == ("09:12:03", "INFO", "ready")
    assert fmt.score(["09:12:03 [info] a", "09:12:04 [oops] b", "garbage"]) == pytest.approx(2 / 3)


def test_formats_are_ordered_by_priority_then_name(tmp_path: Path) -> None:
    _write(tmp_path, "zeta.yaml", YAML.format(name="zeta", priority=1))
    _write(tmp_path, "alpha.yml", YAML.format(name="alpha", priority=9))
    _write(tmp_path, "beta.yaml", YAML.format(name="beta", priority=9))
    _write(tmp_path, "notes.txt", "ignored")
    assert list(load_text_formats(tmp_path)) == ["zeta", "alpha", "beta"]


def test_file_name_must_match_format_name(tmp_path: Path) -> None:
    path = _write(tmp_path, "other.yaml", YAML.format(name="my_service", priority=5))
    with pytest.raises(FormatDefinitionError, match="must match the file name"):
        load_text_format(path)


def test_invalid_yaml_names_the_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "broken.yaml", "name: [unclosed\n")
    with pytest.raises(FormatDefinitionError, match="invalid YAML") as caught:
        load_text_format(path)
    assert caught.value.source == str(path)


def test_duplicate_names_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "dup.yaml", YAML.format(name="dup", priority=1))
    _write(tmp_path, "dup.yml", YAML.format(name="dup", priority=1))
    with pytest.raises(FormatDefinitionError, match="duplicate format name 'dup'"):
        load_text_formats(tmp_path)


# --- Helpers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "raw", "level"),
    [
        (LevelRule("pri", {"3": "ERROR"}, "syslog_priority"), "11", "ERROR"),  # 11 % 8 == 3
        (LevelRule("pri", {"3": "ERROR"}, "syslog_priority"), "x", None),
        (LevelRule("status", {"5xx": "ERROR"}, "http_status"), "503", "ERROR"),
        (LevelRule("status", {"5xx": "ERROR"}, "http_status", default="INFO"), "abc", "INFO"),
        (LevelRule("level", {"warn": "WARN"}), " WARN ", "WARN"),  # case-insensitive, trimmed
        (LevelRule("level", {"warn": "WARN"}, default="UNKNOWN"), "loud", "UNKNOWN"),
        (LevelRule("level", {"warn": "WARN"}), None, None),
    ],
)
def test_level_rule(rule: LevelRule, raw: str | None, level: str | None) -> None:
    assert rule.normalize(raw) == level


@pytest.mark.parametrize(
    ("value", "result"),
    [('"a ""b"" c"', 'a "b" c'), ('"say \\"hi\\""', 'say "hi"'), ('"', '"'), ("plain", "plain")],
)
def test_unquote(value: str, result: str) -> None:
    assert unquote(value) == result


@pytest.mark.parametrize(
    ("raw", "hints", "expected"),
    [
        ("2026-03-14T09:12:01Z", ["iso8601"], datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC)),
        ("1773479521", ["epoch"], datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC)),
        ("1773479521500", ["epoch_ms"], datetime(2026, 3, 14, 9, 12, 1, 500000, tzinfo=UTC)),
        ("1773479521000000", ["epoch_us"], datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC)),
        (
            "14/Mar/2026:09:12:01 +0000",
            ["iso8601", "%d/%b/%Y:%H:%M:%S %z"],
            datetime(2026, 3, 14, 9, 12, 1, tzinfo=UTC),
        ),
        ("Feb 29 09:12:01", ["%b %d %H:%M:%S"], datetime(2000, 2, 29, 9, 12, 1)),  # yearless
        ("nonsense", ["iso8601", "epoch", "%H:%M"], None),
    ],
)
def test_parse_timestamp(raw: str, hints: list[str], expected: datetime | None) -> None:
    assert parse_timestamp(raw, hints) == expected


# --- Wiring into split and detect ------------------------------------------------------------


def test_every_text_format_is_a_detector_in_priority_order() -> None:
    names = [detector.name for detector in DEFAULT_DETECTORS]
    text_names = [name for name in names if name in TEXT_FORMATS]
    assert text_names == list(TEXT_FORMATS)
    assert names.index("xml") < names.index(text_names[0])
    assert names.index(text_names[-1]) < names.index("prefix_json")


def test_windows_csv_export_beats_generic_csv() -> None:
    rows = [
        "Level,Date and Time,Source,Event ID,Task Category",
        "Information,3/14/2026 9:12:01 AM,Service Control Manager,7036,None,Service started.",
        "Error,3/14/2026 9:12:02 AM,Service Control Manager,7031,None,Service crashed.",
    ]
    result = detect_format(rows)
    assert result.format_name == "windows_event_csv"
    assert result.scores_by_format["windows_event_csv"] == 1.0  # the header is ignored


def test_builtin_declared_starts() -> None:
    assert {start.name for start in builtin_declared_starts()} == {
        "journal_export",
        "windows_event_csv",
        "windows_event_text",
    }


def test_declared_start_is_trusted_on_the_first_two_lines() -> None:
    start = DeclaredStart("blocks", re.compile("BEGIN"))
    assert learn_signature(["BEGIN 1", "a=1", "b=2"], [start]).name == "blocks"
    assert learn_signature(["header", "BEGIN 1", "a=1"], [start]).name == "blocks"
    assert learn_signature(["x", "y", "BEGIN 1"], [start]).name != "blocks"


def test_declared_starts_can_be_disabled() -> None:
    lines = [
        Line("j", n, text) for n, text in enumerate(["__CURSOR=1", "MESSAGE=a", "__CURSOR=2"], 1)
    ]
    assert len(list(iter_records(lines))) == 2
    assert len(list(iter_records(lines, options=SplitOptions(declared_starts=())))) == 3


@pytest.mark.parametrize(
    "module", ["loglens.detect", "loglens.parsers", "loglens.split", "loglens.parsers.textformats"]
)
def test_import_order_has_no_cycle(module: str) -> None:
    """detect -> parsers.textformats and parsers -> detect.structured: every entry point works."""
    subprocess.run([sys.executable, "-c", f"import {module}"], check=True, timeout=60)


def test_benchmark_script_runs() -> None:
    script = Path(__file__).parents[1] / "benchmarks" / "bench_text_formats.py"
    namespace = runpy.run_path(str(script), run_name="bench")
    results = namespace["benchmark"](50)
    assert {result.name for result in results} == set(TEXT_FORMATS)
    assert all(result.parse_per_second > 0 for result in results)
