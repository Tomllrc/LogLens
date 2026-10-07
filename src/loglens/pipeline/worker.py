"""Stage 1 of a parse run, one input file per task (run in worker processes).

A task reads one input (a file, possibly an archive, or stdin), splits it into records,
detects each source's format, parses every record (falling back to the generic parser on
low confidence), normalizes timestamps and levels, masks the message, and writes the
result to a staging Parquet file. Templates are mined afterwards, over all files.
"""

import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any, BinaryIO, Final
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from loglens.detect import DetectionResult, DetectOptions, DetectReport, iter_detected
from loglens.formats import FormatRegistry, cached_registry
from loglens.ingest import FileIssue, IngestOptions, IngestReport, InputFile, iter_input_lines
from loglens.parsers import ParsedEvent, Parser, safe_parse
from loglens.parsers.generic import GENERIC_TIMESTAMP_HINTS, GenericParser, strip_ansi
from loglens.parsers.masking import DEFAULT_RULES, Masker, load_masking_rules
from loglens.parsers.mining import mining_text
from loglens.parsers.timestamps import TimestampNormalizer
from loglens.pipeline.options import LOW_CONFIDENCE, ParseOptions
from loglens.schema import UNKNOWN, make_event_id, normalize_level
from loglens.split import Record, SplitOptions, SplitReport, iter_records

STAGING_SCHEMA: Final = pa.schema(
    [
        pa.field("event_id", pa.string()),
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("timestamp_raw", pa.string()),
        pa.field("source_file", pa.string()),
        pa.field("line_start", pa.int64()),
        pa.field("line_end", pa.int64()),
        pa.field("level", pa.string()),
        pa.field("level_raw", pa.string()),
        pa.field("component", pa.string()),
        pa.field("message", pa.string()),
        pa.field("format_name", pa.string()),
        pa.field("parser_confidence", pa.float64()),
        pa.field("raw", pa.string()),
        pa.field("masked", pa.string()),
        pa.field("mask_values", pa.list_(pa.string())),
        pa.field("field_params", pa.list_(pa.string())),
        pa.field("generic", pa.bool_()),
        pa.field("task_index", pa.int64()),
        pa.field("ordinal", pa.int64()),
    ]
)
"""Events before mining: unified fields plus what mining needs."""


@dataclass(frozen=True)
class Task:
    """Inputs processed together (small files are bundled to save per-task costs)."""

    index: int
    items: tuple[InputFile, ...]
    staging_path: Path
    options: ParseOptions
    reference: datetime


@dataclass
class TaskResult:
    """What one task produced (counts, detections and problems; events are on disk)."""

    index: int
    staging_path: Path
    records: int = 0
    events: int = 0
    non_events: int = 0
    malformed: int = 0
    generic: int = 0
    bytes_read: int = 0
    seconds: float = 0.0
    files_read: list[str] = field(default_factory=list)
    skipped: list[FileIssue] = field(default_factory=list)
    warnings: list[tuple[str, FileIssue]] = field(default_factory=list)
    detections: dict[str, DetectionResult] = field(default_factory=dict)
    events_per_source: Counter[str] = field(default_factory=Counter)
    errors: list[tuple[str, str]] = field(default_factory=list)
    """Inputs that failed: (source, error). Their events read before the failure are kept."""


class _StagingWriter:
    def __init__(self, path: Path, batch_rows: int) -> None:
        self._path = path
        self._batch_rows = batch_rows
        self._columns: dict[str, list[Any]] = {name: [] for name in STAGING_SCHEMA.names}
        self._writer: pq.ParquetWriter | None = None

    def add(self, row: dict[str, Any]) -> None:
        for name, values in self._columns.items():
            values.append(row[name])
        if len(self._columns["event_id"]) >= self._batch_rows:
            self.flush()

    def flush(self) -> None:
        if not self._columns["event_id"]:
            return
        table = pa.Table.from_pydict(self._columns, schema=STAGING_SCHEMA)
        if self._writer is None:
            self._writer = pq.ParquetWriter(self._path, STAGING_SCHEMA, compression="lz4")
        self._writer.write_table(table)
        for values in self._columns.values():
            values.clear()

    def close(self) -> None:
        self.flush()
        if self._writer is not None:
            self._writer.close()


class _SourceState:
    """Per-source parsing state: the parser (stateful for CSV) and timestamp normalizer."""

    def __init__(
        self,
        detection: DetectionResult | None,
        registry: FormatRegistry,
        tz: ZoneInfo,
        reference: datetime,
    ):
        format_name = detection.format_name if detection else "free_text"
        details = dict(detection.details) if detection else {}
        self.parser: Parser | None = registry.create_parser(format_name, details)
        text_format = registry.text_formats.get(format_name)
        hints = text_format.timestamp_formats if text_format else ()
        self.normalizer = TimestampNormalizer(
            (*hints, *GENERIC_TIMESTAMP_HINTS), tz=tz, reference=reference
        )


def run_task(task: Task, stdin: BinaryIO | None = None) -> TaskResult:
    """Process a task's inputs into one staging file. Never raises: failures are reported."""
    started = time.perf_counter()
    result = TaskResult(task.index, task.staging_path)
    options = task.options
    masker = Masker(
        load_masking_rules(options.masking_file) if options.masking_file else DEFAULT_RULES
    )
    writer = _StagingWriter(task.staging_path, options.batch_rows)
    ordinals = count()
    try:
        for item in task.items:
            try:
                _process_item(task, item, result, writer, masker, ordinals, stdin)
            except Exception as error:  # one bad input must not stop the others
                result.errors.append((item.source, f"{type(error).__name__}: {error}"))
    finally:
        writer.close()
    result.seconds = time.perf_counter() - started
    return result


def _process_item(
    task: Task,
    item: InputFile,
    result: TaskResult,
    writer: "_StagingWriter",
    masker: Masker,
    ordinals: Iterator[int],
    stdin: BinaryIO | None,
) -> None:
    options = task.options
    registry = cached_registry(options.formats_dirs)
    ingest_report, split_report, detect_report = IngestReport(), SplitReport(), DetectReport()
    ingest_options = IngestOptions(include=options.include, exclude=options.exclude)
    try:
        lines = iter_input_lines(item, options=ingest_options, report=ingest_report, stdin=stdin)
        records = iter_records(
            lines,
            options=SplitOptions(
                max_record_lines=options.max_record_lines,
                declared_starts=registry.declared_starts(),
            ),
            report=split_report,
        )
        detected = iter_detected(
            records, options=DetectOptions(detectors=registry.detectors()), report=detect_report
        )
        for row in _rows(detected, detect_report, registry, task, masker, result):
            row["task_index"], row["ordinal"] = task.index, next(ordinals)
            writer.add(row)
    finally:
        result.files_read.extend(ingest_report.files_read)
        result.skipped.extend(ingest_report.files_skipped)
        result.bytes_read += ingest_report.bytes_read
        result.warnings.extend(
            [
                *(("ingest", issue) for issue in ingest_report.warnings),
                *(("split", issue) for issue in split_report.warnings),
                *(("detect", issue) for issue in detect_report.warnings),
            ]
        )
        result.detections.update(detect_report.detections)


def _rows(
    detected: Iterator[Any],
    detect_report: DetectReport,
    registry: FormatRegistry,
    task: Task,
    masker: Masker,
    result: TaskResult,
) -> Iterator[dict[str, Any]]:
    tz = ZoneInfo(task.options.timezone)
    generic = GenericParser()
    state: _SourceState | None = None
    current_source: str | None = None
    for item in detected:
        record: Record = item.record
        result.records += 1
        if record.source_file != current_source:
            current_source = record.source_file
            state = _SourceState(
                detect_report.detections.get(current_source), registry, tz, task.reference
            )
        assert state is not None
        event, used_generic = _parse(record, state.parser, generic)
        if not event.is_event:
            result.non_events += 1
            continue
        if event.error is not None:
            result.malformed += 1
        result.generic += used_generic
        result.events += 1
        result.events_per_source[record.source_file] += 1
        level = event.level or normalize_level(event.level_raw)
        if event.error is not None and used_generic is False:
            level = UNKNOWN
        masked = masker.mask(mining_text(event.message))
        yield {
            "event_id": make_event_id(record.source_file, record.line_start, record.line_end),
            "timestamp": state.normalizer.to_utc(event.timestamp_raw),
            "timestamp_raw": event.timestamp_raw,
            "source_file": record.source_file,
            "line_start": record.line_start,
            "line_end": record.line_end,
            "level": level,
            "level_raw": event.level_raw,
            "component": event.component,
            "message": event.message,
            "format_name": GenericParser.format_name if used_generic else item.format_name,
            "parser_confidence": event.confidence,
            "raw": record.text,
            "masked": masked.text,
            "mask_values": list(masked.params),
            "field_params": [f"{key}={value}" for key, value in event.extra.items()],
            "generic": used_generic,
        }


def _parse(
    record: Record, parser: Parser | None, generic: GenericParser
) -> tuple[ParsedEvent, bool]:
    """Parse with the format's parser; fall back to the generic one on low confidence."""
    clean_text = strip_ansi(record.text)
    clean = record if clean_text == record.text else record._replace(text=clean_text)
    event = safe_parse(parser, clean) if parser is not None else None
    if event is not None and (not event.is_event or event.confidence >= LOW_CONFIDENCE):
        return event, False
    fallback = generic.parse(clean)
    if event is None or fallback.confidence > event.confidence:
        return fallback, True
    return event, False
