"""End-to-end tests of the parse pipeline and the event store."""

import io
import shutil
import zipfile
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq
import pytest
import yaml

from loglens.pipeline import ParseOptions, ParseSummary, run_parse
from loglens.pipeline import worker as worker_module
from loglens.schema import EVENT_SCHEMA, LEVELS
from loglens.store import DB_FILE, EVENTS_DIR, StoreError, query

FIXTURES = Path(__file__).parent / "fixtures"


def build_mixed(root: Path) -> Path:
    """A folder mixing JSON, syslog, free text with stack traces, a zip and a binary file."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "api.jsonl").write_text(
        "".join(
            f'{{"ts":"2026-03-14T09:12:{i:02d}Z","level":"{"error" if i % 5 == 0 else "info"}",'
            f'"logger":"api","msg":"request {i} served in {i * 3}ms from 10.0.0.{i}"}}\n'
            for i in range(20)
        ),
        encoding="utf-8",
    )
    (root / "auth.log").write_text(
        "".join(
            f"Mar 14 09:13:{i:02d} web-1 sshd[{4000 + i}]: Accepted publickey for user{i} "
            f"from 10.0.0.{i} port {50000 + i} ssh2\n"
            for i in range(10)
        ),
        encoding="utf-8",
    )
    (root / "worker.txt").write_text(
        "2026-03-14 09:14:00 ERROR worker crashed\n"
        "Traceback (most recent call last):\n"
        '  File "worker.py", line 3, in run\n'
        "ValueError: boom\n"
        "2026-03-14 09:14:05 INFO worker restarted after 5 s\n"
        "2026-03-14 09:14:09 ERROR java side failed\n"
        "java.lang.IllegalStateException: card declined\n"
        "\tat com.acme.Pay.charge(Pay.java:88)\n"
        "2026-03-14 09:14:12 INFO worker restarted after 2 s\n",
        encoding="utf-8",
    )
    with zipfile.ZipFile(root / "archive.zip", "w") as archive:
        archive.writestr(
            "rotated/app.log",
            "".join(
                f"2026-03-13 23:59:{i:02d},123 WARN  [main] com.acme.Disk - Disk /dev/sda{i % 3} "
                f"at {80 + i}% capacity\n"
                for i in range(12)
            ),
        )
        archive.writestr(
            "nested/events.json", '{"time":"2026-03-13T22:00:00Z","msg":"backup ok"}\n'
        )
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)))
    return root


@pytest.fixture(scope="module")
def mixed_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, ParseSummary]:
    base = tmp_path_factory.mktemp("mixed")
    source = build_mixed(base / "in")
    output = base / "out"
    return output, run_parse([source], output, ParseOptions(workers=1))


def _rows(output: Path, sql: str) -> list[tuple[Any, ...]]:
    return query(output, sql, limit=10_000).rows


def test_summary(mixed_run: tuple[Path, ParseSummary]) -> None:
    output, summary = mixed_run
    assert summary.inputs == 5  # api.jsonl, auth.log, worker.txt, archive.zip, image.png
    assert summary.events == 20 + 10 + 4 + 12 + 1
    assert summary.records == summary.events
    assert [issue.reason for issue in summary.skipped] == ["binary content"]
    assert summary.failed_inputs == []
    assert dict(summary.formats) == {
        "json": 2,
        "syslog_rfc3164": 1,
        "free_text": 1,
        "log4j": 1,
    }
    assert summary.levels["ERROR"] == 4 + 2  # 4 JSON errors, 2 in worker.txt
    assert summary.top_templates[0] == ("request <NUM> served in <DURATION> from <IP>", 20)
    assert ("worker crashed", 1) in summary.top_error_templates
    assert 0.0 <= summary.low_confidence_share < 0.1
    assert summary.templates == len(_rows(output, "SELECT * FROM templates"))


def test_parquet_files_have_exactly_the_unified_schema(
    mixed_run: tuple[Path, ParseSummary],
) -> None:
    output, _ = mixed_run
    files = sorted((output / EVENTS_DIR).glob("*/*.parquet"))
    assert files
    for path in files:
        assert pq.read_schema(path).remove_metadata().equals(EVENT_SCHEMA.remove_metadata())


def test_partitioned_by_source_file(mixed_run: tuple[Path, ParseSummary]) -> None:
    output, _ = mixed_run
    folders = {path.parent for path in (output / EVENTS_DIR).glob("*/*.parquet")}
    sources = _rows(output, "SELECT DISTINCT source_file FROM events")
    assert len(folders) == len(sources) == 5
    for folder in folders:  # every file holds a single source
        table = pq.read_table(next(folder.glob("*.parquet")), columns=["source_file"])
        assert len(set(table.column("source_file").to_pylist())) == 1


def test_events_content(mixed_run: tuple[Path, ParseSummary]) -> None:
    output, _ = mixed_run
    sources = {row[0] for row in _rows(output, "SELECT DISTINCT source_file FROM events")}
    assert any(s.endswith("archive.zip!/rotated/app.log") for s in sources)
    assert any(s.endswith("archive.zip!/nested/events.json") for s in sources)

    (traceback,) = _rows(
        output,
        "SELECT line_start, line_end, level, template, message, format_name FROM events "
        "WHERE message LIKE 'worker crashed%'",
    )
    assert traceback[:4] == (1, 4, "ERROR", "worker crashed")
    assert traceback[4].endswith("ValueError: boom")
    assert traceback[5] == "free_text"

    (java,) = _rows(output, "SELECT line_end - line_start FROM events WHERE message LIKE 'java%'")
    assert java == (2,)

    (disk,) = _rows(
        output,
        "SELECT component, template, params, level FROM events "
        "WHERE source_file LIKE '%rotated/app.log' AND line_start = 2",
    )
    assert disk == (
        "com.acme.Disk",
        "Disk <PATH> at <NUM>% capacity",
        ["/dev/sda1", "81", "thread=main"],
        "WARN",
    )


def test_invariants(mixed_run: tuple[Path, ParseSummary]) -> None:
    output, _ = mixed_run
    ((total, distinct_ids),) = _rows(
        output, "SELECT count(*), count(DISTINCT event_id) FROM events"
    )
    assert total == distinct_ids
    levels = {row[0] for row in _rows(output, "SELECT DISTINCT level FROM events")}
    assert levels <= set(LEVELS)
    ((bad_conf,),) = _rows(
        output, "SELECT count(*) FROM events WHERE parser_confidence < 0 OR parser_confidence > 1"
    )
    assert bad_conf == 0
    ((null_ts,),) = _rows(output, "SELECT count(*) FROM events WHERE timestamp IS NULL")
    assert null_ts == 0
    ((first,),) = _rows(output, "SELECT min(timestamp) FROM events")
    assert str(first).startswith("2026-03-13 22:00:00")  # stored and shown in UTC


def test_catalog_tables(mixed_run: tuple[Path, ParseSummary]) -> None:
    output, _ = mixed_run
    assert (output / DB_FILE).is_file()
    files = _rows(output, "SELECT format_name, events FROM files ORDER BY events DESC")
    assert files[0] == ("json", 20)
    warnings = _rows(output, "SELECT stage, reason FROM warnings")
    assert ("ingest", "skipped: binary content") in warnings
    (template,) = _rows(
        output,
        "SELECT events, error_events, sources FROM templates WHERE template = 'worker crashed'",
    )
    assert template == (1, 1, 1)
    info = dict(_rows(output, "SELECT key, value FROM run_info"))
    assert info["events"] == "47"


def test_known_templates() -> None:
    """Same template for lines differing by variables, different ones otherwise."""
    expected = yaml.safe_load((FIXTURES / "templates" / "known_templates.yaml").read_text("utf-8"))
    output = Path(__file__).parent / "_known_out"
    shutil.rmtree(output, ignore_errors=True)
    try:
        run_parse([FIXTURES / "templates" / "known.log"], output, ParseOptions(workers=1))
        rows = _rows(output, "SELECT line_start, template, params, template_id FROM events")
    finally:
        shutil.rmtree(output, ignore_errors=True)
    actual = {line: {"template": template, "params": params} for line, template, params, _ in rows}
    assert actual == expected
    ids = {template: {tid for _, t, _, tid in rows if t == template} for _, template, _, _ in rows}
    assert all(len(group) == 1 for group in ids.values())  # one id per template
    assert len({tid for *_, tid in rows}) == len({e["template"] for e in expected.values()})


def test_parallel_and_batched_runs_give_identical_events(
    tmp_path: Path, mixed_run: tuple[Path, ParseSummary]
) -> None:
    reference_output, _ = mixed_run
    source = build_mixed(tmp_path / "in")
    # Runs use different temp folders (so paths and event ids differ): compare by the path
    # relative to the input folder.
    sql = (
        "SELECT regexp_replace(source_file, '^.*/in/', '') AS rel, line_start, template, "
        "params, level, timestamp, parser_confidence FROM events ORDER BY rel, line_start"
    )
    expected = _rows(reference_output, sql)
    for workers, batch_rows in ((2, 10_000), (1, 7)):
        output = tmp_path / f"out-{workers}-{batch_rows}"
        run_parse([source], output, ParseOptions(workers=workers, batch_rows=batch_rows))
        assert _rows(output, sql) == expected


def test_templates_are_stable_across_runs(tmp_path: Path) -> None:
    source = tmp_path / "a.log"
    source.write_text(
        "".join(f"2026-03-14 09:00:{i:02d} INFO Job {i} done in {i}s\n" for i in range(5)),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    run_parse([source], output, ParseOptions(workers=1))
    before = _rows(output, "SELECT DISTINCT template_id, template FROM events")
    source.write_text(
        "".join(f"2026-03-15 10:00:{i:02d} INFO Job {i + 100} done in {i}ms\n" for i in range(3))
        + "2026-03-15 10:01:00 ERROR Cache corrupted\n",
        encoding="utf-8",
    )
    run_parse([source], output, ParseOptions(workers=1))
    after = dict(_rows(output, "SELECT DISTINCT template, template_id FROM events"))
    assert after[before[0][1]] == before[0][0]  # same template text, same id
    assert "Cache corrupted" in after


def test_stdin(tmp_path: Path) -> None:
    data = b'{"level":"warn","msg":"low disk on /dev/sda1"}\n{"level":"info","msg":"ok"}\n'
    summary = run_parse(["-"], tmp_path / "out", ParseOptions(workers=1), stdin=io.BytesIO(data))
    assert summary.events == 2
    rows = _rows(
        tmp_path / "out", "SELECT source_file, level, template FROM events ORDER BY line_start"
    )
    assert rows == [("<stdin>", "WARN", "low disk on <PATH>"), ("<stdin>", "INFO", "ok")]


def test_include_exclude_and_record_limit(tmp_path: Path) -> None:
    source = build_mixed(tmp_path / "in")
    output = tmp_path / "out"
    summary = run_parse(
        [source],
        output,
        ParseOptions(
            workers=1, include=("*.log", "*.txt"), exclude=("archive.zip",), max_record_lines=2
        ),
    )
    sources = {s.source_file.rsplit("/", 1)[-1] for s in summary.files}
    assert sources == {"auth.log", "worker.txt"}
    (longest,) = _rows(output, "SELECT max(line_end - line_start + 1) FROM events")
    assert longest == (2,)


def test_a_failing_input_does_not_stop_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = build_mixed(tmp_path / "in")
    real = worker_module._process

    def flaky(task: worker_module.Task, result: worker_module.TaskResult, stdin: Any) -> None:
        if task.item.source.endswith("auth.log"):
            raise RuntimeError("disk on fire")
        real(task, result, stdin)

    monkeypatch.setattr(worker_module, "_process", flaky)
    summary = run_parse([source], tmp_path / "out", ParseOptions(workers=1))
    assert [(s.rsplit("/", 1)[-1], e) for s, e in summary.failed_inputs] == [
        ("auth.log", "RuntimeError: disk on fire")
    ]
    assert summary.events == 47 - 10
    warnings = _rows(tmp_path / "out", "SELECT reason FROM warnings WHERE stage = 'pipeline'")
    assert warnings == [("failed: RuntimeError: disk on fire",)]


def test_empty_input(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    summary = run_parse([tmp_path / "in"], tmp_path / "out", ParseOptions(workers=1))
    assert (summary.events, summary.files) == (0, [])
    assert _rows(tmp_path / "out", "SELECT count(*) FROM events") == [(0,)]


def test_store_is_read_only_and_movable(
    tmp_path: Path, mixed_run: tuple[Path, ParseSummary]
) -> None:
    output, _ = mixed_run
    moved = tmp_path / "moved"
    shutil.copytree(output, moved)
    assert _rows(moved, "SELECT count(*) FROM events") == [(47,)]
    for statement in ("DROP TABLE store.main.templates", "CREATE TABLE store.main.x (a INT)"):
        with pytest.raises(duckdb.Error):
            query(moved, statement)
    assert _rows(moved, "SELECT count(*) FROM templates")[0][0] > 0


def test_query_limits_rows(mixed_run: tuple[Path, ParseSummary]) -> None:
    output, _ = mixed_run
    result = query(output, "SELECT * FROM events", limit=5)
    assert len(result.rows) == 5 and result.truncated
    assert result.columns[:3] == ["event_id", "timestamp", "timestamp_raw"]


def test_query_requires_a_store(tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        query(tmp_path, "SELECT 1")


def test_invalid_options() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ParseOptions(timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="workers"):
        ParseOptions(workers=0)
