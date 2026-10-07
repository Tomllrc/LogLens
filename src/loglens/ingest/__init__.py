"""Ingestion: files, folders, archives, stdin, encoding detection, chunked streaming."""

from loglens.ingest.models import FileIssue, IngestOptions, IngestReport, Line
from loglens.ingest.reader import (
    ARCHIVE_SEPARATOR,
    STDIN_MARKER,
    STDIN_SOURCE,
    InputFile,
    discover_inputs,
    iter_input_lines,
    iter_lines,
)

__all__ = [
    "ARCHIVE_SEPARATOR",
    "STDIN_MARKER",
    "STDIN_SOURCE",
    "FileIssue",
    "IngestOptions",
    "IngestReport",
    "InputFile",
    "Line",
    "discover_inputs",
    "iter_input_lines",
    "iter_lines",
]
