"""Ingestion: files, folders, archives, stdin, encoding detection, chunked streaming."""

from loglens.ingest.models import FileIssue, IngestOptions, IngestReport, Line
from loglens.ingest.reader import ARCHIVE_SEPARATOR, STDIN_MARKER, STDIN_SOURCE, iter_lines

__all__ = [
    "ARCHIVE_SEPARATOR",
    "STDIN_MARKER",
    "STDIN_SOURCE",
    "FileIssue",
    "IngestOptions",
    "IngestReport",
    "Line",
    "iter_lines",
]
