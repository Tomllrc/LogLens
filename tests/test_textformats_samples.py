"""Tests generated from the YAML format definitions: every ``sample_lines`` entry is a test.

Adding a format (or a sample) to ``src/loglens/parsers/patterns/`` adds tests here with
no Python change.
"""

from pathlib import Path

import pytest

from loglens.detect import DetectReport, detect_format, iter_detected
from loglens.ingest import iter_lines
from loglens.parsers import TEXT_FORMATS, TextFormat, TextFormatParser, create_parser
from loglens.parsers.textformats import Sample
from loglens.parsers.timestamps import parse_timestamp
from loglens.schema import LEVELS
from loglens.split import Record, iter_records

SAMPLES = [
    pytest.param(text_format, sample, id=f"{name}-{index}")
    for name, text_format in TEXT_FORMATS.items()
    for index, sample in enumerate(text_format.samples)
]
FORMATS = [pytest.param(text_format, id=name) for name, text_format in TEXT_FORMATS.items()]


def _observed(text_format: TextFormat, line: str) -> dict[str, str | None]:
    event = TextFormatParser(text_format).parse(Record("sample.log", 1, 1, line))
    assert event.error is None
    assert event.confidence == 1.0
    return {
        "timestamp": event.timestamp_raw,
        "level": event.level,
        "level_raw": event.level_raw,
        "component": event.component,
        "message": event.message,
        **event.extra,
    }


@pytest.mark.parametrize(("text_format", "sample"), SAMPLES)
def test_sample_parses_as_expected(text_format: TextFormat, sample: Sample) -> None:
    observed = _observed(text_format, sample.line)
    assert {key: observed.get(key) for key in sample.expect} == dict(sample.expect)


@pytest.mark.parametrize(("text_format", "sample"), SAMPLES)
def test_sample_timestamp_parses_with_the_declared_hints(
    text_format: TextFormat, sample: Sample
) -> None:
    raw = _observed(text_format, sample.line)["timestamp"]
    if raw is not None:
        assert parse_timestamp(raw, text_format.timestamp_formats) is not None, raw


@pytest.mark.parametrize(("text_format", "sample"), SAMPLES)
def test_sample_level_is_normalized(text_format: TextFormat, sample: Sample) -> None:
    observed = _observed(text_format, sample.line)
    if text_format.level is not None and observed["level_raw"] is not None:
        assert observed["level"] in LEVELS


@pytest.mark.parametrize(("text_format", "sample"), SAMPLES)
def test_sample_alone_is_detected_as_its_format(text_format: TextFormat, sample: Sample) -> None:
    result = detect_format([sample.line])
    assert result.format_name == text_format.name, result.scores_by_format


@pytest.mark.parametrize("text_format", FORMATS)
def test_samples_as_a_file_through_the_whole_pipeline(
    text_format: TextFormat, tmp_path: Path
) -> None:
    """Written to a file, a format's samples are split, detected and parsed cleanly."""
    path = tmp_path / f"{text_format.name}.log"
    path.write_text("\n".join(s.line.rstrip("\n") for s in text_format.samples) + "\n", "utf-8")

    report = DetectReport()
    detected = list(iter_detected(iter_records(iter_lines(path)), report=report))
    (result,) = report.detections.values()
    assert result.format_name == text_format.name, result.scores_by_format

    parser = create_parser(result.format_name, result.details)
    assert parser is not None
    events = [parser.parse(item.record) for item in detected]
    assert all(event.confidence == 1.0 for event in events if event.is_event)


@pytest.mark.parametrize("text_format", FORMATS)
def test_definition_is_documented(text_format: TextFormat) -> None:
    assert len(text_format.description) > 40
    assert text_format.samples
    assert text_format.timestamp_formats, "declare how to read the timestamps"
