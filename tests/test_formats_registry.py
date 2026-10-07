"""Tests for the format registry (user plugins) and record sampling."""

import zipfile
from pathlib import Path

import yaml
from tests.ai_fixtures import plc_lines, write_plc_log

from loglens.formats import BUILT_IN, cached_registry, load_registry
from loglens.parsers.textformats import TEXT_FORMATS, schema_errors
from loglens.pipeline import ParseOptions, run_parse
from loglens.sampling import line_shape, load_records, select_samples
from loglens.store import query

PLC_FORMAT = {
    "name": "plc_alarm_log",
    "description": "Alarm log of an industrial PLC.",
    "regex": [
        r"\[(?P<component>PLC\d+)\] (?P<timestamp>\d{2}/\d{2}/\d{4} \S+) (?P<level>[A-Z]{3}) "
        r"#(?P<code>\d{4}) (?P<message>.*)"
    ],
    "timestamp_format": ["%d/%m/%Y %H:%M:%S.%f"],
    "level": {"map": {"ALM": "ERROR", "WRN": "WARN", "INF": "INFO", "FLT": "FATAL"}},
    "sample_lines": [plc_lines(1)[0]],
}


def write_format(directory: Path, data: dict[str, object], name: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name or data['name']}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_built_ins_only_by_default(tmp_path: Path) -> None:
    registry = load_registry([tmp_path / "missing"])
    assert set(registry.text_formats) == set(TEXT_FORMATS)
    assert set(registry.origins.values()) == {BUILT_IN}
    assert registry.errors == ()


def test_user_formats_are_added_and_ordered_by_priority(tmp_path: Path) -> None:
    path = write_format(tmp_path, {**PLC_FORMAT, "priority": 1})
    registry = load_registry([tmp_path])
    assert registry.origins["plc_alarm_log"] == path.as_posix()
    assert next(iter(registry.text_formats)) == "plc_alarm_log"
    names = [detector.name for detector in registry.detectors()]
    assert names[:3] == ["json", "xml", "plc_alarm_log"]
    assert names[-1] == "free_text"
    assert registry.create_parser("plc_alarm_log") is not None
    assert registry.create_parser("json") is not None
    assert registry.create_parser("nope") is None


def test_user_formats_override_built_ins_and_later_folders_win(tmp_path: Path) -> None:
    built_in = TEXT_FORMATS["go_log"]
    override = {**PLC_FORMAT, "name": "go_log", "description": "Mine."}
    write_format(tmp_path / "a", override)
    write_format(tmp_path / "b", {**override, "description": "Theirs."})
    registry = load_registry([tmp_path / "a", tmp_path / "b"])
    assert registry.text_formats["go_log"].description == "Theirs."
    assert registry.text_formats["go_log"] is not built_in
    assert registry.origins["go_log"].endswith("b/go_log.yaml")


def test_invalid_plugins_are_reported_and_skipped(tmp_path: Path) -> None:
    write_format(tmp_path, {**PLC_FORMAT, "regex": "no groups here"}, name="bad_regex")
    write_format(tmp_path, {"name": "x"}, name="missing_fields")
    (tmp_path / "broken.yml").write_text("name: [unclosed", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    write_format(tmp_path, PLC_FORMAT)
    registry = load_registry([tmp_path])
    assert "plc_alarm_log" in registry.text_formats
    messages = [str(error) for error in registry.errors]
    assert len(messages) == 3
    assert any("bad_regex.yaml" in m and "named groups" in m for m in messages)
    assert any("missing_fields.yaml" in m and "description" in m for m in messages)
    assert any("broken.yml" in m for m in messages)


def test_schema_errors_name_the_path() -> None:
    errors = schema_errors({**PLC_FORMAT, "priority": "high", "level": {"map": {"A": "LOUD"}}})
    assert any(e.startswith("priority:") for e in errors)
    assert any(e.startswith("level/map/A:") for e in errors)
    assert schema_errors(PLC_FORMAT) == []


def test_declared_starts_come_from_multiline_formats(tmp_path: Path) -> None:
    write_format(tmp_path, {**PLC_FORMAT, "record_start": r"\[PLC\d+\] "})
    starts = load_registry([tmp_path]).declared_starts()
    assert "plc_alarm_log" in [start.name for start in starts]


def test_cached_registry_is_reused(tmp_path: Path) -> None:
    cached_registry.cache_clear()
    assert cached_registry((tmp_path,)) is cached_registry((tmp_path,))
    cached_registry.cache_clear()


def test_parse_uses_user_formats(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    log = write_plc_log(tmp_path / "logs" / "plc.log")
    write_format(tmp_path / "formats", PLC_FORMAT)
    cached_registry.cache_clear()
    out = tmp_path / "out"
    options = ParseOptions(workers=1, formats_dirs=(tmp_path / "formats",))
    summary = run_parse([str(log)], out, options)
    assert summary.formats["plc_alarm_log"] == 1
    result = query(
        out,
        "SELECT count(*), count(timestamp), min(parser_confidence), "
        "count(DISTINCT component) > 1, bool_and(list_contains(params, 'code=' || "
        "regexp_extract(raw, '#(\\d{4})', 1))) FROM events",
    )
    total, with_time, confidence, components, codes = result.rows[0]
    assert (total, with_time) == (200, 200)
    assert confidence >= 0.9
    assert components and codes
    levels = dict(query(out, "SELECT level, count(*) FROM events GROUP BY 1").rows)
    assert set(levels) <= {"ERROR", "WARN", "INFO", "FATAL"}


# --- Sampling --------------------------------------------------------------------------------


def test_line_shape_abstracts_values() -> None:
    assert line_shape("2026-03-14 ERROR disk 93% full") == "9-9-9 a a 9% a"
    assert line_shape("x" * 100 + "\nsecond line") == "a"


def test_select_samples_covers_rare_shapes() -> None:
    records = [f"INFO request {i} ok" for i in range(500)] + ["!! PANIC at core 3 !!"]
    samples = select_samples(records, size=10)
    assert len(samples) == 10
    assert "!! PANIC at core 3 !!" in samples
    assert select_samples(["a", "b"], size=10) == ["a", "b"]
    assert select_samples([]) == []


def test_load_records_reads_files_and_archive_members(tmp_path: Path) -> None:
    log = write_plc_log(tmp_path / "plc.log", count=30)
    assert len(load_records(log)) == 30
    assert len(load_records(log, limit=5)) == 5
    archive = tmp_path / "logs.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(log, "plc.log")
        bundle.writestr("other.log", "2026-01-01 INFO other\n")
    assert len(load_records(f"{archive}!/plc.log")) == 30
    assert load_records(f"{archive}!/other.log") == ["2026-01-01 INFO other"]
    assert load_records(tmp_path / "missing.log") == []
