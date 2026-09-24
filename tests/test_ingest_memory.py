"""Memory stays bounded however large the input is (the requirement is 5 GB, constant RAM).

The files here are tens of MB, not GB, to keep the suite fast. The assertion is that peak
Python allocations stay far below the file size and below a fixed ceiling, which only
holds if nothing buffers the whole file.
"""

import gzip
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

from loglens.ingest import IngestOptions, IngestReport, iter_lines

MiB = 1024 * 1024
PEAK_CEILING = 4 * MiB


def _peak_while_consuming(lines: Iterator[object]) -> tuple[int, int]:
    """Consume an iterator under tracemalloc; return (item count, peak traced bytes)."""
    tracemalloc.start()
    try:
        count = sum(1 for _ in lines)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return count, peak


def _write_large_log(path: Path, target_bytes: int) -> int:
    """Write about ``target_bytes`` of log lines; return the number of lines written."""
    line = (
        "2026-01-01T00:00:{:02d}Z INFO worker-{:04d} handled request id=0123456789abcdef in 12 ms\n"
    )
    block = "".join(line.format(i % 60, i) for i in range(1000)).encode()
    blocks = target_bytes // len(block) + 1
    with path.open("wb") as handle:
        for _ in range(blocks):
            handle.write(block)
    return blocks * 1000


def test_memory_bounded_on_large_file(tmp_path: Path) -> None:
    path = tmp_path / "large.log"
    expected_lines = _write_large_log(path, 40 * MiB)
    size = path.stat().st_size

    report = IngestReport()
    count, peak = _peak_while_consuming(iter_lines(path, report=report))

    assert count == expected_lines
    assert report.bytes_read == size
    assert peak < PEAK_CEILING, f"peak {peak / MiB:.1f} MiB while reading {size / MiB:.0f} MiB"


def test_memory_bounded_on_large_gzip(tmp_path: Path) -> None:
    plain = tmp_path / "large.log"
    expected_lines = _write_large_log(plain, 24 * MiB)
    compressed = tmp_path / "large.log.gz"
    with plain.open("rb") as source, gzip.open(compressed, "wb", compresslevel=1) as target:
        while chunk := source.read(MiB):
            target.write(chunk)
    plain.unlink()

    count, peak = _peak_while_consuming(iter_lines(compressed))

    assert count == expected_lines
    assert peak < PEAK_CEILING, f"peak {peak / MiB:.1f} MiB"


def test_memory_bounded_on_single_huge_line(tmp_path: Path) -> None:
    """A file with no line breaks at all must not be read into one giant string."""
    path = tmp_path / "one_line.log"
    chunk = b"x" * MiB
    with path.open("wb") as handle:
        for _ in range(24):
            handle.write(chunk)

    options = IngestOptions(max_line_chars=64 * 1024)
    report = IngestReport()
    lines = iter_lines(path, options=options, report=report)
    count, peak = _peak_while_consuming(line for line in lines if line.line_number == 1)

    assert count == 24 * MiB // (64 * 1024)
    assert peak < PEAK_CEILING, f"peak {peak / MiB:.1f} MiB"
    assert len(report.warnings) == 1
    assert "split" in report.warnings[0].reason
