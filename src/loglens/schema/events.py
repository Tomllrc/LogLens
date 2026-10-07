"""The unified event schema as an Arrow schema, shared by the writer and readers."""

import hashlib
from typing import Final

import pyarrow as pa

EVENT_SCHEMA: Final = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("timestamp_raw", pa.string()),
        pa.field("source_file", pa.string(), nullable=False),
        pa.field("line_start", pa.int64(), nullable=False),
        pa.field("line_end", pa.int64(), nullable=False),
        pa.field("level", pa.string(), nullable=False),
        pa.field("level_raw", pa.string()),
        pa.field("component", pa.string()),
        pa.field("message", pa.string(), nullable=False),
        pa.field("template_id", pa.string(), nullable=False),
        pa.field("template", pa.string(), nullable=False),
        pa.field("params", pa.list_(pa.string()), nullable=False),
        pa.field("format_name", pa.string(), nullable=False),
        pa.field("parser_confidence", pa.float64(), nullable=False),
        pa.field("raw", pa.string(), nullable=False),
    ]
)
"""Exactly the columns of the unified event schema documented in CLAUDE.md, in order."""

EVENT_COLUMNS: Final = tuple(EVENT_SCHEMA.names)


def make_event_id(source_file: str, line_start: int, line_end: int) -> str:
    """A stable 16-hex-digit id for the record at ``source_file:line_start-line_end``."""
    key = f"{source_file}\x00{line_start}\x00{line_end}".encode()
    return hashlib.blake2b(key, digest_size=8).hexdigest()
