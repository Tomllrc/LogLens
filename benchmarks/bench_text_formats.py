"""Throughput of the declarative text-format parsers.

Run with: uv run python benchmarks/bench_text_formats.py [records_per_format]

For each built-in format, records are built by cycling through its YAML sample lines,
then parsed with the format's parser (regex match, field mapping, level normalization,
event construction). Raw regex matching is timed separately. Formats whose samples are
all single lines are "simple"; the target for them is 100k records/s.
"""

import sys
import time
from dataclasses import dataclass
from itertools import cycle, islice

from loglens.parsers.textformats import TEXT_FORMATS, TextFormat, TextFormatParser
from loglens.split import Record

TARGET_RECORDS_PER_SECOND = 100_000


@dataclass(frozen=True)
class Result:
    """Throughput of one format."""

    name: str
    simple: bool
    parse_per_second: float
    match_per_second: float


def _records(text_format: TextFormat, count: int) -> list[Record]:
    lines = [sample.line for sample in text_format.samples]
    return [Record("bench.log", n, n, text) for n, text in enumerate(islice(cycle(lines), count))]


def _rate(count: int, seconds: float) -> float:
    return count / seconds if seconds > 0 else float("inf")


def benchmark_format(text_format: TextFormat, count: int) -> Result:
    """Parse ``count`` records of one format and return its throughput."""
    records = _records(text_format, count)
    parser = TextFormatParser(text_format)
    start = time.perf_counter()
    for record in records:
        parser.parse(record)
    parse_seconds = time.perf_counter() - start

    texts = [record.text for record in records]
    start = time.perf_counter()
    for text in texts:
        text_format.match(text)
    match_seconds = time.perf_counter() - start

    simple = all("\n" not in sample.line.strip() for sample in text_format.samples)
    return Result(
        text_format.name, simple, _rate(count, parse_seconds), _rate(count, match_seconds)
    )


def benchmark(count: int = 100_000) -> list[Result]:
    """Benchmark every built-in format."""
    return [benchmark_format(text_format, count) for text_format in TEXT_FORMATS.values()]


def main(argv: list[str]) -> int:
    count = int(argv[1]) if len(argv) > 1 else 100_000
    results = benchmark(count)
    print(f"{'format':<20} {'kind':<11} {'parse rec/s':>12} {'match rec/s':>12}")
    for result in results:
        kind = "simple" if result.simple else "multi-line"
        print(
            f"{result.name:<20} {kind:<11} {result.parse_per_second:>12,.0f}"
            f" {result.match_per_second:>12,.0f}"
        )
    slow = [r.name for r in results if r.simple and r.parse_per_second < TARGET_RECORDS_PER_SECOND]
    if slow:
        print(f"below {TARGET_RECORDS_PER_SECOND:,} records/s: {', '.join(slow)}")
        return 1
    print(f"all simple formats reach {TARGET_RECORDS_PER_SECOND:,} records/s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
