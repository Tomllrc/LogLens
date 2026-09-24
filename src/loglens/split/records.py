"""Group the lines of each file into records, streaming."""

import logging
from collections.abc import Iterable, Iterator
from functools import cache
from itertools import groupby, islice
from operator import attrgetter

from loglens.ingest.models import FileIssue, Line
from loglens.split.models import Record, SplitOptions, SplitReport
from loglens.split.signature import DeclaredStart, learn_signature

logger = logging.getLogger(__name__)


def iter_records(
    lines: Iterable[Line],
    *,
    options: SplitOptions | None = None,
    report: SplitReport | None = None,
) -> Iterator[Record]:
    """Lazily group lines into records, one file at a time.

    Each file's record-start signature is learned from its first ``sample_lines``
    lines. Lines that don't start a record are appended to the previous one. Memory
    is bounded by the sample and by ``max_record_lines``. Records never span files.
    """
    options = options or SplitOptions()
    report = report if report is not None else SplitReport()
    declared = options.declared_starts
    if declared is None:
        declared = builtin_declared_starts()
    for source, file_lines in groupby(lines, key=attrgetter("source_file")):
        yield from _split_file(source, file_lines, options, declared, report)


@cache
def builtin_declared_starts() -> tuple[DeclaredStart, ...]:
    """Record starts declared by the built-in text formats (YAML ``record_start``)."""
    # Imported here, not at module level: the parsers package itself depends on split.
    from loglens.parsers.textformats import TEXT_FORMATS

    return tuple(
        DeclaredStart(fmt.name, fmt.record_start)
        for fmt in TEXT_FORMATS.values()
        if fmt.record_start is not None
    )


def _split_file(
    source: str,
    lines: Iterator[Line],
    options: SplitOptions,
    declared: tuple[DeclaredStart, ...],
    report: SplitReport,
) -> Iterator[Record]:
    sample = list(islice(lines, options.sample_lines))
    signature = learn_signature(_first_pieces(sample), declared)
    report.signatures[source] = signature.name

    builder = _RecordBuilder(source)
    overflows = 0
    for line in _replay(sample, lines):
        if builder.line_count and line.line_number == builder.line_end:
            builder.extend_last(line.text)  # another piece of an over-long physical line
            continue
        starts = signature.is_start(line.text)
        if builder.line_count and not starts and builder.line_count < options.max_record_lines:
            builder.append(line)
            continue
        if builder.line_count:
            if not starts:
                overflows += 1
            yield builder.flush()
        builder.append(line)
    if builder.line_count:
        yield builder.flush()

    if overflows:
        message = f"{overflows} records exceeded {options.max_record_lines} lines and were split"
        report.warnings.append(FileIssue(source, message))
        logger.warning("%s: %s", source, message)


def _first_pieces(sample: list[Line]) -> Iterator[str]:
    """Text of each physical line, skipping continuation pieces of over-long lines."""
    previous = None
    for line in sample:
        if line.line_number != previous:
            yield line.text
        previous = line.line_number


def _replay(buffer: list[Line], rest: Iterator[Line]) -> Iterator[Line]:
    """Yield the buffered sample, releasing it as it goes, then the remaining lines."""
    buffer.reverse()
    while buffer:
        yield buffer.pop()
    yield from rest


class _RecordBuilder:
    """Accumulates the lines of the record under construction."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.line_start = 0
        self.line_end = 0
        self._parts: list[str] = []

    @property
    def line_count(self) -> int:
        return len(self._parts)

    def append(self, line: Line) -> None:
        if not self._parts:
            self.line_start = line.line_number
        self._parts.append(line.text)
        self.line_end = line.line_number

    def extend_last(self, text: str) -> None:
        self._parts[-1] += text

    def flush(self) -> Record:
        record = Record(self.source, self.line_start, self.line_end, "\n".join(self._parts))
        self._parts = []
        return record
