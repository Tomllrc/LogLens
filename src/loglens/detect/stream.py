"""Detect each file's format while streaming its records."""

import logging
from collections.abc import Iterable, Iterator
from itertools import groupby, islice
from operator import attrgetter

from loglens.detect.formats import detect_format
from loglens.detect.models import DetectedRecord, Detection, DetectOptions, DetectReport
from loglens.detect.registry import FREE_TEXT
from loglens.ingest.models import FileIssue
from loglens.split.models import Record

logger = logging.getLogger(__name__)


def iter_detected(
    records: Iterable[Record],
    *,
    options: DetectOptions | None = None,
    report: DetectReport | None = None,
) -> Iterator[DetectedRecord]:
    """Lazily tag records with the format of their file.

    Consecutive records sharing a ``source_file`` form one file. Its format is detected
    from its first ``sample_records`` records; those are then replayed, followed by the
    rest of the file, each yielded unchanged and in order with the file's format name and
    confidence. Memory is bounded by the sample.
    """
    options = options or DetectOptions()
    report = report if report is not None else DetectReport()
    for source, file_records in groupby(records, key=attrgetter("source_file")):
        yield from _detect_file(source, file_records, options, report)


def _detect_file(
    source: str, records: Iterator[Record], options: DetectOptions, report: DetectReport
) -> Iterator[DetectedRecord]:
    sample = list(islice(records, options.sample_records))

    def on_error(name: str, error: Exception) -> None:
        _warn(report, source, f"detector {name!r} failed: {type(error).__name__}: {error}")

    detection = detect_format(
        [record.text for record in sample],
        detectors=options.detectors,
        min_confidence=options.min_confidence,
        on_error=on_error,
    )
    report.detections[source] = detection
    _warn_partial_match(report, source, detection)

    for record in _replay(sample, records):
        yield DetectedRecord(record, detection.format_name, detection.confidence)


def _warn_partial_match(report: DetectReport, source: str, detection: Detection) -> None:
    """Warn when a structured format was chosen although some sample records don't fit."""
    if detection.format_name != FREE_TEXT and detection.confidence < 1.0:
        mismatched = 1.0 - detection.confidence
        _warn(
            report,
            source,
            f"detected {detection.format_name} but {mismatched:.0%} of sampled records "
            "do not match it",
        )


def _warn(report: DetectReport, source: str, message: str) -> None:
    report.warnings.append(FileIssue(source, message))
    logger.warning("%s: %s", source, message)


def _replay(buffer: list[Record], rest: Iterator[Record]) -> Iterator[Record]:
    """Yield the buffered sample, releasing it as it goes, then the remaining records."""
    buffer.reverse()
    while buffer:
        yield buffer.pop()
    yield from rest
