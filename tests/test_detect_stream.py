"""End-to-end tests for iter_detected: ingest -> split -> detect."""

import logging
from collections.abc import Sequence
from itertools import count, islice, pairwise
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loglens.detect import (
    DEFAULT_DETECTORS,
    FREE_TEXT,
    DetectedRecord,
    DetectionResult,
    DetectOptions,
    Detector,
    DetectReport,
    iter_detected,
)
from loglens.ingest import FileIssue, iter_lines
from loglens.split import Record, iter_records

FIXTURES = Path(__file__).parent / "fixtures" / "detect"

EXPECTED = {
    "json_lines.jsonl": "json",
    "pretty.json": "json",
    "header.csv": "csv",
    "noheader.csv": "csv",
    "data.tsv": "csv",
    "prefix_json.log": "prefix_json",
    "ambiguous.log": "logfmt",
    "logfmt.log": "logfmt",
    "events.xml": "xml",
    "syslog_rfc3164.log": "syslog_rfc3164",
    "syslog_rfc5424.log": "syslog_rfc5424",
    "apache_combined.log": "apache_combined",
    "apache_common.log": "apache_common",
    "nginx_access.log": "apache_combined",
    "nginx_error.log": "nginx_error",
    "python_logging.log": "python_logging",
    "log4j.log": "log4j",
    "free_text.txt": FREE_TEXT,
}


def _pipeline(
    path: Path, options: DetectOptions | None = None
) -> tuple[list[DetectedRecord], DetectReport]:
    report = DetectReport()
    detected = list(iter_detected(iter_records(iter_lines(path)), options=options, report=report))
    return detected, report


def _records(texts: Sequence[str], source: str = "app.log") -> list[Record]:
    return [Record(source, n, n, text) for n, text in enumerate(texts, start=1)]


@pytest.mark.parametrize(("name", "expected"), EXPECTED.items(), ids=list(EXPECTED))
def test_fixture_detected(name: str, expected: str) -> None:
    detected, report = _pipeline(FIXTURES / name)
    source = (FIXTURES / name).as_posix()
    assert report.detections[source].format_name == expected
    assert {d.format_name for d in detected} == {expected}
    if expected != FREE_TEXT:
        assert report.detections[source].confidence == 1.0
    assert report.warnings == []


def test_every_fixture_has_an_expectation() -> None:
    assert sorted(p.name for p in FIXTURES.iterdir()) == sorted(EXPECTED)


def test_whole_fixture_folder_each_file_independent() -> None:
    detected, report = _pipeline(FIXTURES)
    assert {Path(s).name: d.format_name for s, d in report.detections.items()} == EXPECTED
    for item in detected:
        assert item.format_name == EXPECTED[Path(item.record.source_file).name]
    assert report.format_counts["json"] == 2
    assert report.format_counts["apache_combined"] == 2


@pytest.mark.parametrize(
    ("name", "details"),
    [
        ("header.csv", {"delimiter": "comma", "columns": "4", "header": "yes"}),
        ("noheader.csv", {"delimiter": "comma", "columns": "4", "header": "no"}),
        ("data.tsv", {"delimiter": "tab", "columns": "4", "header": "no"}),
    ],
)
def test_csv_fixture_details(name: str, details: dict[str, str]) -> None:
    _, report = _pipeline(FIXTURES / name)
    assert report.detections[(FIXTURES / name).as_posix()].details == details


def test_ambiguous_file_two_formats_compete() -> None:
    """Quoted logfmt messages with two commas each: csv fits every record too."""
    _, report = _pipeline(FIXTURES / "ambiguous.log")
    result = report.detections[(FIXTURES / "ambiguous.log").as_posix()]
    assert result.scores_by_format["logfmt"] == result.scores_by_format["csv"] == 1.0
    assert result.format_name == "logfmt"  # the more specific format wins the tie
    assert result.runner_up == ("csv", 1.0)


def test_multi_line_records_keep_their_stack_traces() -> None:
    detected, _ = _pipeline(FIXTURES / "log4j.log")
    traces = [d.record for d in detected if "\n" in d.record.text]
    assert len(traces) == 3
    assert all("\tat com.acme.App.handle" in r.text for r in traces)


def test_pretty_xml_events_are_whole_elements() -> None:
    detected, _ = _pipeline(FIXTURES / "events.xml")
    events = [d.record for d in detected if d.record.text.startswith("<Event")]
    assert len(events) == 8
    assert all(r.text.endswith("</Event>") for r in events)


def test_malformed_records_in_a_json_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    lines = [f'{{"n": {i}, "msg": "ok"}}' for i in range(18)]
    lines[5] = '{"n": 5, "msg": "truncated'
    lines[11] = "{panic: something went wrong}"
    path = tmp_path / "app.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="loglens.detect"):
        detected, report = _pipeline(path)

    detection = report.detections[path.as_posix()]
    assert detection.format_name == "json"
    assert detection.confidence == pytest.approx(16 / 18)
    assert len(detected) == 18
    assert all(d.format_name == "json" and d.confidence < 1.0 for d in detected)
    assert report.warnings == [
        FileIssue(path.as_posix(), "detected json but 11% of sampled records do not match it")
    ]
    assert "do not match it" in caplog.text


def test_records_pass_through_unchanged_and_in_order() -> None:
    records = _records(["a=1 b=2", "", "a=3 b=4"]) + _records(["plain"], "other.log")
    detected = list(iter_detected(records))
    assert [d.record for d in detected] == records
    assert [d.format_name for d in detected] == ["logfmt", "logfmt", "logfmt", FREE_TEXT]


def test_per_file_detection_on_interleaved_sources() -> None:
    records = [
        *_records(['{"a": 1}', '{"a": 2}'], "a.log"),
        *_records(["x=1 y=2", "x=3 y=4"], "b.log"),
        *_records(['{"a": 3}'], "a.log"),  # a new group: detected again on its own
    ]
    report = DetectReport()
    detected = list(iter_detected(records, report=report))
    assert [d.format_name for d in detected] == ["json", "json", "logfmt", "logfmt", "json"]
    assert set(report.detections) == {"a.log", "b.log"}


def test_detection_uses_only_the_sample() -> None:
    texts = ["plain words"] * 3 + ['{"a": 1}'] * 10
    report = DetectReport()
    options = DetectOptions(sample_records=3)
    detected = list(iter_detected(_records(texts), options=options, report=report))
    assert report.detections["app.log"] == DetectionResult(FREE_TEXT, 0.1)
    assert len(detected) == 13


def test_streams_lazily() -> None:
    endless = (Record("inf", n, n, f'{{"n": {n}}}') for n in count(1))
    first = list(islice(iter_detected(endless, options=DetectOptions(sample_records=20)), 3))
    assert [d.record.line_start for d in first] == [1, 2, 3]
    assert {d.format_name for d in first} == {"json"}


def test_empty_input() -> None:
    report = DetectReport()
    assert list(iter_detected([], report=report)) == []
    assert report.detections == {}


def _boom(sample: Sequence[str]) -> float:
    raise RuntimeError("plugin bug")


def test_failing_detector_is_reported_not_raised() -> None:
    options = DetectOptions(detectors=(Detector("broken", _boom), *DEFAULT_DETECTORS))
    report = DetectReport()
    detected = list(iter_detected(_records(['{"a": 1}']), options=options, report=report))
    assert [d.format_name for d in detected] == ["json"]
    assert report.warnings == [
        FileIssue("app.log", "detector 'broken' failed: RuntimeError: plugin bug")
    ]


def test_invalid_options_are_rejected() -> None:
    with pytest.raises(ValueError, match="sample_records"):
        DetectOptions(sample_records=0)
    with pytest.raises(ValueError, match="min_confidence"):
        DetectOptions(min_confidence=1.5)
    with pytest.raises(ValueError, match="unique"):
        DetectOptions(detectors=(*DEFAULT_DETECTORS, DEFAULT_DETECTORS[0]))


_texts = st.one_of(
    st.text(st.characters(exclude_categories=("Cc", "Cs")), max_size=40),
    st.sampled_from(['{"a": 1}', "a=1 b=2", "x,y,z", "<a/>", ""]),
)
_record_lists = st.lists(st.tuples(st.sampled_from(["a.log", "b.log"]), _texts), max_size=40).map(
    lambda items: [Record(src, n, n, text) for n, (src, text) in enumerate(items, start=1)]
)


@given(records=_record_lists, sample=st.integers(1, 10))
def test_records_are_preserved_and_tagged_consistently(records: list[Record], sample: int) -> None:
    report = DetectReport()
    detected = list(
        iter_detected(records, options=DetectOptions(sample_records=sample), report=report)
    )
    assert [d.record for d in detected] == records
    assert all(0.0 <= d.confidence <= 1.0 for d in detected)
    for d in detected:
        assert d.format_name in {*(det.name for det in DEFAULT_DETECTORS)}
    # Consecutive records of one file share a single detection.
    for before, after in pairwise(detected):
        if before.record.source_file == after.record.source_file:
            assert (before.format_name, before.confidence) == (after.format_name, after.confidence)
