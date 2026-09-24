"""End-to-end tests for iter_records."""

import logging
from itertools import count, islice
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loglens.ingest import FileIssue, Line, iter_lines
from loglens.split import Record, SplitOptions, SplitReport, iter_records

JAVA = [
    "2026-01-01 10:00:00,001 INFO  [main] com.acme.App - Starting",
    "2026-01-01 10:00:01,002 ERROR [main] com.acme.App - Request failed",
    "java.lang.IllegalStateException: boom",
    "\tat com.acme.App.handle(App.java:42)",
    "\tat com.acme.App.main(App.java:10)",
    "Caused by: java.io.IOException: disk full",
    "\tat com.acme.Store.write(Store.java:7)",
    "\t... 2 more",
    "2026-01-01 10:00:02,003 INFO  [main] com.acme.App - Recovered",
]

PYTHON = [
    "2026-01-01 10:00:00,000 - app - INFO - started",
    "2026-01-01 10:00:01,000 - app - ERROR - unhandled",
    "Traceback (most recent call last):",
    '  File "app.py", line 10, in <module>',
    "    main()",
    '  File "app.py", line 6, in main',
    '    raise ValueError("boom")',
    "ValueError: boom",
    "",
    "During handling of the above exception, another exception occurred:",
    "",
    "Traceback (most recent call last):",
    '  File "app.py", line 12, in <module>',
    "RuntimeError: wrapped",
    "2026-01-01 10:00:02,000 - app - INFO - done",
]

PRETTY_JSON = [
    "{",
    '  "time": "2026-01-01T10:00:00Z",',
    '  "level": "info",',
    '  "msg": "started"',
    "}",
    "{",
    '  "time": "2026-01-01T10:00:01Z",',
    '  "nested": {',
    '    "values": [1, 2]',
    "  }",
    "}",
]


def _lines(texts: list[str], source: str = "app.log") -> list[Line]:
    return [Line(source, number, text) for number, text in enumerate(texts, start=1)]


def _split(texts: list[str], **options: int) -> tuple[list[Record], SplitReport]:
    report = SplitReport()
    records = list(iter_records(_lines(texts), options=SplitOptions(**options), report=report))
    return records, report


def _ranges(records: list[Record]) -> list[tuple[int, int]]:
    return [(r.line_start, r.line_end) for r in records]


def test_java_stack_trace() -> None:
    records, report = _split(JAVA)
    assert _ranges(records) == [(1, 1), (2, 8), (9, 9)]
    assert records[1] == Record("app.log", 2, 8, "\n".join(JAVA[1:8]))
    assert report.signatures == {"app.log": "timestamp"}
    assert report.warnings == []


def test_python_traceback_with_chained_exceptions() -> None:
    records, report = _split(PYTHON)
    assert _ranges(records) == [(1, 1), (2, 14), (15, 15)]
    assert records[1].text.endswith("RuntimeError: wrapped")
    assert report.signatures["app.log"] == "timestamp"


def test_every_line_is_a_record() -> None:
    texts = [f"2026-01-01 10:00:0{i} INFO request {i} served" for i in range(10)]
    records, _ = _split(texts)
    assert _ranges(records) == [(i, i) for i in range(1, 11)]
    assert [r.text for r in records] == texts


def test_no_timestamps_at_all() -> None:
    texts = [
        "Starting server",
        "Listening on port 8080",
        "Accepted connection from 10.0.0.1",
        "Shutting down",
    ]
    records, report = _split(texts)
    assert [r.text for r in records] == texts
    assert report.signatures["app.log"] == "unindented"


def test_no_timestamps_still_attaches_stack_frames() -> None:
    texts = [
        "Starting",
        'Exception in thread "main" java.lang.RuntimeException: x',
        "\tat A.b(A.java:1)",
        "\tat A.main(A.java:5)",
        "Done",
    ]
    records, _ = _split(texts)
    assert _ranges(records) == [(1, 1), (2, 4), (5, 5)]


def test_pretty_printed_json() -> None:
    records, report = _split(PRETTY_JSON)
    assert _ranges(records) == [(1, 5), (6, 11)]
    assert records[1].text == "\n".join(PRETTY_JSON[5:])
    assert report.signatures["app.log"] == "json"


def test_pretty_printed_xml_events() -> None:
    event = ["<Event>", "  <System>", "    <EventID>1</EventID>", "  </System>", "</Event>"]
    records, report = _split(['<?xml version="1.0"?>', *event, *event])
    assert _ranges(records) == [(1, 1), (2, 6), (7, 11)]
    assert records[1].text == "\n".join(event)
    assert report.signatures["app.log"] == "xml"


def test_json_lines() -> None:
    texts = ['{"a": 1}', '{"a": 2}', '{"a": 3}']
    records, _ = _split(texts)
    assert [r.text for r in records] == texts


def test_multiline_sql_without_indentation() -> None:
    texts = [
        "2026-01-01 10:00:00 DEBUG executing:",
        "SELECT *",
        "FROM users",
        "WHERE id = 1",
        "2026-01-01 10:00:01 DEBUG done",
    ]
    records, _ = _split(texts)
    assert _ranges(records) == [(1, 4), (5, 5)]


def test_leading_continuation_lines_form_their_own_record() -> None:
    """A file rotated mid-trace starts with orphan continuation lines."""
    records, _ = _split(["\tat A.b(A.java:1)", "\t... 3 more", *JAVA])
    assert _ranges(records)[0] == (1, 2)
    assert _ranges(records)[1] == (3, 3)


def test_pathological_giant_record_is_capped(caplog: pytest.LogCaptureFixture) -> None:
    texts = ["2026-01-01 10:00:00 ERROR dump follows", *(["  0xdeadbeef"] * 2000)]
    with caplog.at_level(logging.WARNING, logger="loglens.split"):
        records, report = _split(texts)

    assert _ranges(records) == [(1, 500), (501, 1000), (1001, 1500), (1501, 2000), (2001, 2001)]
    assert all(r.text.count("\n") < 500 for r in records)
    assert report.warnings == [FileIssue("app.log", "4 records exceeded 500 lines and were split")]
    assert "4 records exceeded 500 lines" in caplog.text


def test_max_record_lines_is_configurable() -> None:
    records, report = _split(JAVA, max_record_lines=3)
    assert _ranges(records) == [(1, 1), (2, 4), (5, 7), (8, 8), (9, 9)]
    assert report.warnings[0].reason == "2 records exceeded 3 lines and were split"


def test_pieces_of_an_over_long_line_are_rejoined() -> None:
    lines = [Line("f", 1, "abc"), Line("f", 1, "def"), Line("f", 2, "ghi")]
    records = list(iter_records(lines))
    assert records == [Record("f", 1, 1, "abcdef"), Record("f", 2, 2, "ghi")]


def test_signature_learned_per_file_and_records_never_span_files() -> None:
    lines = _lines(JAVA, "java.log") + _lines(PRETTY_JSON, "data.json")
    report = SplitReport()
    records = list(iter_records(lines, report=report))
    assert report.signatures == {"java.log": "timestamp", "data.json": "json"}
    assert [(r.source_file, r.line_start, r.line_end) for r in records] == [
        ("java.log", 1, 1),
        ("java.log", 2, 8),
        ("java.log", 9, 9),
        ("data.json", 1, 5),
        ("data.json", 6, 11),
    ]


def test_signature_comes_from_the_sample_only() -> None:
    texts = ["plain one", "plain two", "2026-01-01 10:00:00 INFO late", "  indented"]
    records, report = _split(texts, sample_lines=2)
    assert report.signatures["app.log"] == "unindented"
    assert _ranges(records) == [(1, 1), (2, 2), (3, 4)]


def test_empty_input() -> None:
    assert list(iter_records([])) == []


def test_streams_lazily() -> None:
    endless = (Line("inf", n, f"2026-01-01 10:00:00 INFO event {n}") for n in count(1))
    first = list(islice(iter_records(endless, options=SplitOptions(sample_lines=50)), 3))
    assert _ranges(first) == [(1, 1), (2, 2), (3, 3)]


def test_integration_with_ingest(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_bytes("\r\n".join(JAVA).encode("utf-8"))
    records = list(iter_records(iter_lines(path)))
    assert _ranges(records) == [(1, 1), (2, 8), (9, 9)]
    assert {r.source_file for r in records} == {path.as_posix()}


def test_invalid_options_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_record_lines"):
        SplitOptions(max_record_lines=0)
    with pytest.raises(ValueError, match="sample_lines"):
        SplitOptions(sample_lines=0)


_line_texts = st.sampled_from(
    [
        "2026-01-01 10:00:00 INFO event",
        "\tat A.b(A.java:1)",
        "Caused by: x",
        "plain text",
        "",
        "{",
        "}",
        "[main] x",
    ]
)


@given(texts=st.lists(_line_texts, max_size=60), max_lines=st.integers(1, 8))
def test_records_partition_the_input(texts: list[str], max_lines: int) -> None:
    records, _ = _split(texts, max_record_lines=max_lines)

    # Contiguous, in order, covering every line exactly once.
    expected_start = 1
    for record in records:
        assert record.line_start == expected_start
        assert record.line_start <= record.line_end
        assert record.line_end - record.line_start + 1 <= max_lines
        expected_start = record.line_end + 1
    assert expected_start == len(texts) + 1

    # No text is lost or altered.
    assert "\n".join(r.text for r in records) == "\n".join(texts)
