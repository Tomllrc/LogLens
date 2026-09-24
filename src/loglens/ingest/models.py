"""Data types produced and consumed by the ingestion layer."""

from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple


class Line(NamedTuple):
    """One decoded line of text, without its line terminator.

    ``source_file`` is the file path in POSIX form; files inside archives use
    ``archive.zip!/inner/file.log``. ``line_number`` is 1-based.
    """

    source_file: str
    line_number: int
    text: str


@dataclass(frozen=True)
class FileIssue:
    """A file-level problem: why a source was skipped, or what went wrong reading it."""

    source_file: str
    reason: str


@dataclass(frozen=True)
class IngestOptions:
    """Settings for :func:`loglens.ingest.iter_lines`.

    ``include`` and ``exclude`` are glob patterns matched against a source's full path
    and against its basename. They filter files discovered inside folders and archives;
    paths given explicitly are always read. ``exclude`` also prunes whole directories and
    archives, while ``include`` only applies to text files (archives are always opened).

    Lines longer than ``max_line_chars`` are yielded in several pieces sharing one line
    number, so a file without line breaks cannot exhaust memory.

    ``max_lines_per_file`` stops reading each file after that many lines, for callers
    that only need a sample (such as format detection). ``None`` reads everything.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_line_chars: int = 1_000_000
    max_archive_depth: int = 8
    max_lines_per_file: int | None = None

    def __post_init__(self) -> None:
        if self.max_line_chars < 1:
            raise ValueError("max_line_chars must be at least 1")
        if self.max_archive_depth < 0:
            raise ValueError("max_archive_depth must not be negative")
        if self.max_lines_per_file is not None and self.max_lines_per_file < 1:
            raise ValueError("max_lines_per_file must be at least 1")


@dataclass
class IngestReport:
    """What happened during ingestion. Filled in progressively while lines are consumed."""

    files_read: list[str] = field(default_factory=list)
    files_skipped: list[FileIssue] = field(default_factory=list)
    warnings: list[FileIssue] = field(default_factory=list)
    encodings: dict[str, str] = field(default_factory=dict)
    bytes_read: int = 0

    @property
    def encoding_counts(self) -> Counter[str]:
        """Number of files read per detected encoding."""
        return Counter(self.encodings.values())
