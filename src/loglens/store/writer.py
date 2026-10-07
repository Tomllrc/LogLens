"""Write events to Parquet, one folder per source file, in bounded batches."""

import hashlib
import re
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from loglens.schema.events import EVENT_SCHEMA

ROWS_PER_FILE: Final = 1_000_000
"""A source file's events roll over to a new Parquet part after this many rows."""

_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]+")


def source_key(source_file: str) -> str:
    """A filesystem-safe, unique folder name for a source file (readable slug + hash)."""
    slug = _UNSAFE.sub("_", source_file.rsplit("/", 1)[-1])[:60].strip("._") or "source"
    digest = hashlib.blake2b(source_file.encode(), digest_size=5).hexdigest()
    return f"{slug}-{digest}"


class PartitionedEventWriter:
    """Writes event batches sorted by source file, opening one Parquet writer at a time.

    Batches must arrive grouped by ``source_file`` (all rows of a source together); a
    source that comes back after another one gets a new part file, so correctness does
    not depend on it, only the number of files.
    """

    def __init__(self, events_dir: Path, compression: str = "zstd") -> None:
        self.events_dir = events_dir
        self.compression = compression
        self._source: str | None = None
        self._writer: pq.ParquetWriter | None = None
        self._rows_in_part = 0
        self._parts: dict[str, int] = {}
        self.rows_written = 0

    def write(self, batch: pa.RecordBatch) -> None:
        """Write a batch whose rows may span several (consecutive) source files."""
        if batch.num_rows == 0:
            return
        sources = batch.column("source_file").to_pylist()
        start = 0
        for index in range(1, len(sources) + 1):
            if index == len(sources) or sources[index] != sources[start]:
                self._write_source(sources[start], batch.slice(start, index - start))
                start = index

    def _write_source(self, source: str, rows: pa.RecordBatch) -> None:
        if source != self._source or self._rows_in_part >= ROWS_PER_FILE:
            self._open(source)
        assert self._writer is not None
        self._writer.write_batch(rows)
        self._rows_in_part += rows.num_rows
        self.rows_written += rows.num_rows

    def _open(self, source: str) -> None:
        self.close()
        folder = self.events_dir / source_key(source)
        folder.mkdir(parents=True, exist_ok=True)
        part = self._parts.get(source, 0)
        self._parts[source] = part + 1
        path = folder / f"part-{part:05d}.parquet"
        self._writer = pq.ParquetWriter(path, EVENT_SCHEMA, compression=self.compression)
        self._source = source
        self._rows_in_part = 0

    def close(self) -> None:
        """Close the open part file, if any."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self._source = None
