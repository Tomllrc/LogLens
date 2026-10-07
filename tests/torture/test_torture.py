"""Torture tests: hostile, broken and extreme inputs through the whole pipeline.

The contract: the pipeline never crashes, never loses lines silently, and reports what
it could not read. Tests marked ``slow`` run with ``pytest -m slow`` (and in CI).
"""

import gzip
import random
import string
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from loglens.ingest import discover_inputs
from loglens.pipeline import ParseOptions, ParseSummary, run_parse
from loglens.store import EVENTS_DIR, query


def _parse(paths: list[Path], output: Path, **options: Any) -> ParseSummary:
    summary = run_parse(paths, output, ParseOptions(**{"workers": 1, **options}))
    assert summary.failed_inputs == []
    return summary


def _rows(output: Path, sql: str) -> list[tuple[Any, ...]]:
    return query(output, sql, limit=10_000_000).rows


def _lines_accounted(output: Path) -> int:
    ((covered,),) = _rows(output, "SELECT coalesce(sum(line_end - line_start + 1), 0) FROM events")
    return int(covered)


def test_mixed_encodings_in_one_file(tmp_path: Path) -> None:
    path = tmp_path / "mixed.log"
    utf8 = "".join(f"2026-03-14 09:00:{i:02d} INFO café ✓ {i}\n" for i in range(30)).encode()
    latin1 = "".join(f"2026-03-14 09:01:{i:02d} INFO résumé {i}\n" for i in range(30))
    path.write_bytes(utf8 + latin1.encode("latin-1"))
    summary = _parse([path], tmp_path / "out")
    assert summary.events == 60
    messages = [m for (m,) in _rows(tmp_path / "out", "SELECT message FROM events")]
    assert any("café ✓" in m for m in messages)
    assert any("�" in m for m in messages)  # undecodable bytes replaced, never raised


def test_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "bad.log"
    path.write_bytes(b"2026-03-14 09:00:00 ERROR bad \xff\xfe\xc3( bytes\n" * 50)
    summary = _parse([path], tmp_path / "out")
    assert summary.events == 50
    ((raw,),) = _rows(tmp_path / "out", "SELECT any_value(raw) FROM events")
    # The whole file being invalid UTF-8, a single-byte code page is detected: every byte
    # decodes (nothing replaced, nothing lost) and the text around them is intact.
    assert raw.startswith("2026-03-14 09:00:00 ERROR bad ") and raw.endswith("( bytes")


def test_nul_bytes(tmp_path: Path) -> None:
    middle = tmp_path / "middle.log"
    head = b"".join(b"2026-03-14 09:00:%02d INFO ok %d\n" % (i % 60, i) for i in range(600))
    middle.write_bytes(head + b"2026-03-14 09:10:00 ERROR crash\x00\x00\x00 padding\n" + head)
    padded = tmp_path / "padded.log"
    padded.write_bytes(b"\x00" * 4096 + head)
    summary = _parse([middle, padded], tmp_path / "out")
    assert summary.events == 1201  # NULs after the sniffed start are kept as text
    assert [(issue.source_file.rsplit("/", 1)[-1], issue.reason) for issue in summary.skipped] == [
        ("padded.log", "binary content")  # NUL padding at the start looks binary: reported
    ]


def test_ten_megabyte_single_line(tmp_path: Path) -> None:
    path = tmp_path / "huge.log"
    path.write_bytes(b"2026-03-14 09:00:00 ERROR payload " + b"x" * (10 * 1024 * 1024) + b"\n")
    summary = _parse([path], tmp_path / "out")
    assert summary.events == 1
    ((length, template, level),) = _rows(
        tmp_path / "out", "SELECT length(raw), template, level FROM events"
    )
    assert length > 10 * 1024 * 1024
    assert level == "ERROR"
    assert len(template) <= 1000  # mining uses a bounded headline


def test_truncated_files(tmp_path: Path) -> None:
    lines = b"".join(b"2026-03-14 09:00:%02d INFO line %d\n" % (i % 60, i) for i in range(5000))
    compressed = gzip.compress(lines)
    (tmp_path / "cut.log.gz").write_bytes(compressed[: len(compressed) // 2])
    (tmp_path / "cut.jsonl").write_bytes(
        b'{"msg": "ok", "level": "info"}\n{"msg": "cut',
    )
    (tmp_path / "no_newline.log").write_bytes(b"2026-03-14 09:00:00 INFO last line without newline")
    summary = _parse([tmp_path], tmp_path / "out")
    assert summary.events > 1000
    reasons = [r for (r,) in _rows(tmp_path / "out", "SELECT reason FROM warnings")]
    assert any("rest of file skipped" in r for r in reasons)  # the truncated gzip, reported
    ((cut_conf,),) = _rows(
        tmp_path / "out",
        "SELECT min(parser_confidence) FROM events WHERE source_file LIKE '%jsonl'",
    )
    assert cut_conf < 0.5


def test_timestamps_going_backwards(tmp_path: Path) -> None:
    path = tmp_path / "clock.log"
    path.write_text(
        "2026-03-14 10:00:00 INFO a\n2026-03-14 09:00:00 INFO clock jumped back\n"
        "2026-03-14 11:00:00 INFO b\n",
        encoding="utf-8",
    )
    _parse([path], tmp_path / "out")
    rows = _rows(
        tmp_path / "out", "SELECT line_start, hour(timestamp) FROM events ORDER BY line_start"
    )
    assert rows == [(1, 10), (2, 9), (3, 11)]  # kept as written, in line order


def test_dst_changes(tmp_path: Path) -> None:
    path = tmp_path / "dst.log"
    stamps = [
        "2026-03-29 01:30:00",
        "2026-03-29 03:30:00",
        "2026-10-25 02:30:00",
        "2026-10-25 03:30:00",
    ]
    path.write_text("".join(f"{s} INFO tick\n" for s in stamps), encoding="utf-8")
    _parse([path], tmp_path / "out", timezone="Europe/Paris")
    rows = [
        ts for (ts,) in _rows(tmp_path / "out", "SELECT timestamp FROM events ORDER BY line_start")
    ]
    assert rows == [
        datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 30, tzinfo=UTC),
    ]


def test_half_the_lines_missing_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "half.log"
    lines = []
    for i in range(1000):
        lines.append(f"2026-03-14 09:{i // 60 % 60:02d}:{i % 60:02d} INFO event {i}")
        lines.append(f"detail line without a timestamp {i}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = _parse([path], tmp_path / "out")
    assert summary.records == 1000  # each detail line continues the previous event
    assert _lines_accounted(tmp_path / "out") == 2000
    ((missing,),) = _rows(tmp_path / "out", "SELECT count(*) FROM events WHERE timestamp IS NULL")
    assert missing == 0


def test_all_unique_lines(tmp_path: Path) -> None:
    rng = random.Random(7)
    words = ["".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 9))) for _ in range(5000)]
    path = tmp_path / "unique.log"
    path.write_text(
        "".join(" ".join(rng.choices(words, k=rng.randint(2, 12))) + "\n" for _ in range(20_000)),
        encoding="utf-8",
    )
    summary = _parse([path], tmp_path / "out")
    assert summary.events == 20_000
    assert summary.templates > 1000
    assert summary.low_confidence_share == 1.0  # free text without timestamps: flagged


def test_ansi_color_codes_are_stripped_but_raw_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "color.log"
    path.write_text(
        "".join(
            f"2026-03-14 09:00:{i:02d} \x1b[31mERROR\x1b[0m \x1b[1mjob {i} failed\x1b[0m\n"
            for i in range(10)
        ),
        encoding="utf-8",
    )
    _parse([path], tmp_path / "out")
    rows = _rows(tmp_path / "out", "SELECT DISTINCT level, template FROM events")
    assert rows == [("ERROR", "job <NUM> failed")]
    ((raw,),) = _rows(tmp_path / "out", "SELECT any_value(raw) FROM events")
    assert "\x1b[31m" in raw


def test_rotation_sets_are_read_oldest_first(tmp_path: Path) -> None:
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "app.log").write_text("2026-03-14 10:00:00 INFO newest\n", encoding="utf-8")
    (folder / "app.log.1").write_text("2026-03-13 10:00:00 INFO older\n", encoding="utf-8")
    (folder / "app.log.2.gz").write_bytes(gzip.compress(b"2026-03-12 10:00:00 INFO oldest\n"))
    (folder / "other.log").write_text("2026-03-14 11:00:00 INFO other\n", encoding="utf-8")
    order = [item.source.rsplit("/", 1)[-1] for item in discover_inputs(folder)]
    assert order == ["app.log.2.gz", "app.log.1", "app.log", "other.log"]
    summary = _parse([folder], tmp_path / "out")
    assert summary.events == 4
    first = _rows(tmp_path / "out", "SELECT message FROM events ORDER BY timestamp LIMIT 1")
    assert first == [("oldest",)]


@pytest.mark.slow
def test_ten_thousand_tiny_files(tmp_path: Path) -> None:
    folder = tmp_path / "many"
    for i in range(10_000):
        sub = folder / f"d{i // 1000}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"f{i}.log").write_text(
            f"2026-03-14 09:00:{i % 60:02d} INFO file {i} ok\n", encoding="utf-8"
        )
    summary = _parse([folder], tmp_path / "out", workers=4)
    assert summary.events == 10_000
    assert len(summary.files) == 10_000
    assert len(list((tmp_path / "out" / EVENTS_DIR).iterdir())) == 10_000
    assert summary.templates == 1


@pytest.mark.slow
def test_one_million_identical_lines(tmp_path: Path) -> None:
    path = tmp_path / "same.log"
    with path.open("wb") as handle:
        for _ in range(100):
            handle.write(b"2026-03-14 09:00:00 WARN disk almost full\n" * 10_000)
    summary = _parse([path], tmp_path / "out")
    assert summary.events == 1_000_000
    assert summary.templates == 1
    parts = list((tmp_path / "out" / EVENTS_DIR).glob("*/*.parquet"))
    assert sum(pq.read_metadata(p).num_rows for p in parts) == 1_000_000


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(data=st.binary(max_size=3000))
def test_fuzz_random_bytes_never_crash(tmp_path: Path, data: bytes) -> None:
    """Random bytes through the whole pipeline: no crash, every line accounted for."""
    case = tmp_path / f"case-{abs(hash(data))}"
    case.mkdir()
    (case / "fuzz.log").write_bytes(data)
    summary = run_parse([case / "fuzz.log"], case / "out", ParseOptions(workers=1))
    assert summary.failed_inputs == []
    if summary.files:
        assert _lines_accounted(case / "out") >= summary.events
