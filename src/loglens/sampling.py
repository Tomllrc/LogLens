"""Read a log's records and pick a diverse sample of them (for format tools and the AI)."""

import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loglens.ingest import ARCHIVE_SEPARATOR, iter_lines
from loglens.split import iter_records

SAMPLE_SIZE: Final = 50
VALIDATION_RECORDS: Final = 10_000


def load_records(source: str | Path, limit: int = VALIDATION_RECORDS) -> list[str]:
    """Texts of the first ``limit`` records of a file or of an archive member."""
    text = str(source)
    path, _, member = text.partition(ARCHIVE_SEPARATOR)
    records: list[str] = []
    for record in iter_records(iter_lines(Path(path))):
        if member and not record.source_file.endswith(ARCHIVE_SEPARATOR + member):
            continue
        if record.text.strip():
            records.append(record.text)
        if len(records) >= limit:
            break
    return records


_DIGITS: Final = re.compile(r"\d+")
_LETTERS: Final = re.compile(r"[A-Za-z]+")
_SPACES: Final = re.compile(r"\s+")


def line_shape(text: str, width: int = 40) -> str:
    """The layout of a record's first line: digit runs -> 9, letter runs -> a."""
    first = text.partition("\n")[0][:width]
    return _SPACES.sub(" ", _LETTERS.sub("a", _DIGITS.sub("9", first)))


def select_samples(records: Sequence[str], size: int = SAMPLE_SIZE) -> list[str]:
    """Up to ``size`` records, round-robin across line shapes (rare shapes included)."""
    groups: dict[str, list[str]] = defaultdict(list)
    for text in records:
        groups[line_shape(text)].append(text)
    ordered = sorted(groups.values(), key=len, reverse=True)
    samples: list[str] = []
    depth = 0
    while len(samples) < size and any(depth < len(group) for group in ordered):
        for group in ordered:
            if depth < len(group) and len(samples) < size:
                samples.append(group[depth])
        depth += 1
    return samples
