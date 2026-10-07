"""Ingestion entry point: files, folders, archives, and stdin as one lazy stream of lines."""

import bz2
import gzip
import io
import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from loglens.ingest.filters import matches_any
from loglens.ingest.models import FileIssue, IngestOptions, IngestReport, Line
from loglens.ingest.sniff import ContainerKind, sniff_container, sniff_text
from loglens.ingest.streams import ByteSource, ReplayStream, iter_text_lines, read_sample

STDIN_MARKER: Final = "-"
STDIN_SOURCE: Final = "<stdin>"
ARCHIVE_SEPARATOR: Final = "!/"
SAMPLE_SIZE: Final = 64 * 1024
"""Bytes read ahead from each stream to sniff its archive format and encoding."""

# Everything a corrupt, truncated, encrypted, or unreadable input can raise.
_READ_ERRORS: Final = (
    OSError,
    EOFError,
    zlib.error,
    zipfile.BadZipFile,
    tarfile.TarError,
    RuntimeError,
    NotImplementedError,
)

InputPath = str | os.PathLike[str]


@dataclass
class _Context:
    options: IngestOptions
    report: IngestReport

    def skip(self, source: str, reason: str) -> None:
        self.report.files_skipped.append(FileIssue(source, reason))

    def warn(self, source: str, reason: str) -> None:
        self.report.warnings.append(FileIssue(source, reason))

    def is_excluded(self, source: str) -> bool:
        return matches_any(source, self.options.exclude)

    def is_included(self, source: str) -> bool:
        return not self.options.include or matches_any(source, self.options.include)


def iter_lines(
    inputs: InputPath | Iterable[InputPath],
    *,
    options: IngestOptions | None = None,
    report: IngestReport | None = None,
    stdin: ByteSource | None = None,
) -> Iterator[Line]:
    """Lazily yield every text line from files, folders (recursive), archives, and stdin.

    ``"-"`` reads stdin (or the ``stdin`` argument, if given). ``.gz``, ``.bz2``, ``.zip``
    and ``.tar`` content is unpacked transparently at any nesting level, recognised by
    magic bytes rather than file extension. Binary files are skipped. Each file's encoding
    is detected from a sample, and every line ending style is accepted.

    Never raises on bad input: problems are recorded in ``report``, which is filled in
    progressively as lines are consumed.
    """
    options = options or IngestOptions()
    report = report if report is not None else IngestReport()
    for item in discover_inputs(inputs, options=options, report=report):
        yield from iter_input_lines(item, options=options, report=report, stdin=stdin)


@dataclass(frozen=True)
class InputFile:
    """One unit of input: a file on disk (possibly an archive), or stdin (``path`` None).

    ``filtered`` is True for files discovered inside a folder: include/exclude patterns
    apply to them, while paths given explicitly are always read.
    """

    path: Path | None
    filtered: bool = False

    @property
    def source(self) -> str:
        """The source name lines from this input get (POSIX path, or ``<stdin>``)."""
        return STDIN_SOURCE if self.path is None else self.path.as_posix()


def discover_inputs(
    inputs: InputPath | Iterable[InputPath],
    *,
    options: IngestOptions | None = None,
    report: IngestReport | None = None,
) -> Iterator[InputFile]:
    """Expand inputs into files to read, without reading them: folders are walked
    (in a stable order, excluded directories pruned), ``"-"`` becomes stdin."""
    ctx = _Context(options or IngestOptions(), report if report is not None else IngestReport())
    for item in _as_input_list(inputs):
        if os.fspath(item) == STDIN_MARKER:
            yield InputFile(None)
            continue
        path = Path(item)
        if path.is_dir():
            yield from (InputFile(file_path, filtered=True) for file_path in _walk(path, ctx))
        else:
            yield InputFile(path)


def iter_input_lines(
    item: InputFile,
    *,
    options: IngestOptions | None = None,
    report: IngestReport | None = None,
    stdin: ByteSource | None = None,
) -> Iterator[Line]:
    """Lazily yield the lines of one input found by :func:`discover_inputs`."""
    ctx = _Context(options or IngestOptions(), report if report is not None else IngestReport())
    if item.path is None:
        stream = stdin if stdin is not None else sys.stdin.buffer
        yield from _read_stream(STDIN_SOURCE, stream, ctx, filtered=False)
    else:
        yield from _read_file(item.path, ctx, filtered=item.filtered)


def _as_input_list(inputs: InputPath | Iterable[InputPath]) -> list[InputPath]:
    if isinstance(inputs, str | os.PathLike):
        return [inputs]
    return list(inputs)


def _walk(root: Path, ctx: _Context) -> Iterator[Path]:
    """Yield regular files under ``root``, pruning excluded directories.

    Order is stable: a directory's own files (sorted by name) come before its
    subdirectories (also sorted by name).
    """

    def on_error(error: OSError) -> None:
        where = Path(error.filename).as_posix() if error.filename else root.as_posix()
        ctx.skip(where, f"cannot list directory: {error.strerror or error}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        kept = []
        for dirname in sorted(dirnames):
            dir_source = Path(dirpath, dirname).as_posix()
            if ctx.is_excluded(dir_source):
                ctx.skip(dir_source, "excluded by pattern")
            else:
                kept.append(dirname)
        dirnames[:] = kept
        for filename in sorted(filenames):
            file_path = Path(dirpath, filename)
            if file_path.is_file():
                yield file_path
            else:
                ctx.skip(file_path.as_posix(), "not a regular file")


def _read_file(path: Path, ctx: _Context, *, filtered: bool) -> Iterator[Line]:
    source = path.as_posix()
    if filtered and ctx.is_excluded(source):
        ctx.skip(source, "excluded by pattern")
        return
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        ctx.skip(source, "not found")
        return
    except OSError as exc:
        ctx.skip(source, f"cannot open: {exc.strerror or exc}")
        return
    with handle:
        yield from _read_stream(source, handle, ctx, filtered=filtered, path=path)


def _read_stream(
    source: str,
    stream: ByteSource,
    ctx: _Context,
    *,
    filtered: bool,
    path: Path | None = None,
    depth: int = 0,
) -> Iterator[Line]:
    """Sniff a stream, then unpack it if it is a container or decode it if it is text."""
    try:
        sample = read_sample(stream, SAMPLE_SIZE)
    except _READ_ERRORS as exc:
        ctx.skip(source, f"unreadable: {exc}")
        return
    replay = ReplayStream(sample, stream)
    kind = sniff_container(sample)

    if kind is None:
        complete = len(sample) < SAMPLE_SIZE
        yield from _read_text(source, replay, sample, ctx, complete=complete, filtered=filtered)
    elif depth >= ctx.options.max_archive_depth:
        ctx.skip(source, f"{kind} nested deeper than {ctx.options.max_archive_depth} levels")
    elif kind is ContainerKind.GZIP:
        with gzip.GzipFile(fileobj=io.BufferedReader(replay), mode="rb") as inner:
            yield from _read_stream(source, inner, ctx, filtered=filtered, depth=depth + 1)
    elif kind is ContainerKind.BZIP2:
        with bz2.BZ2File(io.BufferedReader(replay)) as inner:
            yield from _read_stream(source, inner, ctx, filtered=filtered, depth=depth + 1)
    elif kind is ContainerKind.ZIP:
        yield from _read_zip(source, replay, ctx, path=path, depth=depth)
    else:
        yield from _read_tar(source, replay, ctx, depth=depth)


def _read_zip(
    source: str, replay: ReplayStream, ctx: _Context, *, path: Path | None, depth: int
) -> Iterator[Line]:
    """Read every member of a zip. Zips need random access, so streams go to a temp file."""
    with ExitStack() as stack:
        try:
            archive: Path | IO[bytes]
            if path is not None:
                archive = path
            else:
                spool = stack.enter_context(tempfile.TemporaryFile())
                shutil.copyfileobj(io.BufferedReader(replay), spool)
                spool.seek(0)
                archive = spool
            zip_file = stack.enter_context(zipfile.ZipFile(archive))
            members = [info for info in zip_file.infolist() if not info.is_dir()]
        except _READ_ERRORS as exc:
            ctx.skip(source, f"corrupt zip archive: {exc}")
            return

        for info in members:
            member_source = f"{source}{ARCHIVE_SEPARATOR}{info.filename}"
            if ctx.is_excluded(member_source):
                ctx.skip(member_source, "excluded by pattern")
                continue
            try:
                member = zip_file.open(info)
            except _READ_ERRORS as exc:
                ctx.skip(member_source, f"cannot open archive member: {exc}")
                continue
            with member:
                yield from _read_stream(member_source, member, ctx, filtered=True, depth=depth + 1)


def _read_tar(source: str, replay: ReplayStream, ctx: _Context, *, depth: int) -> Iterator[Line]:
    """Read every regular-file member of a tar, streaming it front to back."""
    try:
        with tarfile.open(fileobj=io.BufferedReader(replay), mode="r|") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                member_source = f"{source}{ARCHIVE_SEPARATOR}{member.name}"
                if ctx.is_excluded(member_source):
                    ctx.skip(member_source, "excluded by pattern")
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                with extracted:
                    yield from _read_stream(
                        member_source, extracted, ctx, filtered=True, depth=depth + 1
                    )
    except _READ_ERRORS as exc:
        ctx.warn(source, f"tar archive is corrupt or truncated: {exc}")


def _read_text(
    source: str,
    replay: ReplayStream,
    sample: bytes,
    ctx: _Context,
    *,
    complete: bool,
    filtered: bool,
) -> Iterator[Line]:
    """Decode a text stream into lines, or skip it if it is filtered out or binary."""
    if filtered and not ctx.is_included(source):
        ctx.skip(source, "not matched by include patterns")
        return
    encoding = sniff_text(sample, complete=complete)
    if encoding is None:
        ctx.skip(source, "binary content")
        return

    report = ctx.report
    report.files_read.append(source)
    report.encodings[source] = encoding.name
    if not encoding.confident:
        ctx.warn(source, "encoding not detected; decoded as UTF-8 with replacement characters")

    max_chars = ctx.options.max_line_chars
    line_limit = ctx.options.max_lines_per_file
    last_line = 0
    split_reported = False
    pieces = iter_text_lines(replay, encoding.name, max_line_chars=max_chars)
    try:
        with closing(pieces):
            for piece in pieces:
                if line_limit is not None and piece.number > line_limit:
                    break
                if piece.continued and not split_reported:
                    ctx.warn(source, f"lines longer than {max_chars} characters were split")
                    split_reported = True
                last_line = piece.number
                yield Line(source, piece.number, piece.text)
    except _READ_ERRORS as exc:
        ctx.warn(source, f"read error after line {last_line}, rest of file skipped: {exc}")
    finally:
        report.bytes_read += replay.bytes_read
