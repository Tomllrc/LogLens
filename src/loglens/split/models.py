"""Data types produced by record splitting."""

from dataclasses import dataclass, field
from typing import NamedTuple

from loglens.ingest.models import FileIssue
from loglens.split.signature import DeclaredStart


class Record(NamedTuple):
    """One logical log event, possibly spanning several physical lines.

    ``line_start`` and ``line_end`` are 1-based and inclusive. ``text`` is the lines
    joined with ``\\n`` (pieces of an over-long line are rejoined without a separator).
    """

    source_file: str
    line_start: int
    line_end: int
    text: str


@dataclass(frozen=True)
class SplitOptions:
    """Settings for :func:`loglens.split.iter_records`.

    ``sample_lines`` leading lines of each file are used to learn its record-start
    signature. No record grows beyond ``max_record_lines``; the overflow starts a new
    record and is reported as a warning.

    ``declared_starts`` are record-start patterns of known multi-line formats, tried
    before any guessing. ``None`` (the default) uses the ``record_start`` of the built-in
    text formats (see :mod:`loglens.parsers.textformats`); ``()`` disables them.
    """

    max_record_lines: int = 500
    sample_lines: int = 1000
    declared_starts: tuple[DeclaredStart, ...] | None = None

    def __post_init__(self) -> None:
        if self.max_record_lines < 1:
            raise ValueError("max_record_lines must be at least 1")
        if self.sample_lines < 1:
            raise ValueError("sample_lines must be at least 1")


@dataclass
class SplitReport:
    """What happened during splitting. Filled in progressively while records are consumed."""

    signatures: dict[str, str] = field(default_factory=dict)
    warnings: list[FileIssue] = field(default_factory=list)
