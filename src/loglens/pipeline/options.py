"""Settings of a parse run."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOW_CONFIDENCE: Final = 0.5
"""Records parsed below this confidence are re-parsed by the generic parser."""

LEARN_CONFIDENCE: Final = 0.6
"""What users are told is low confidence: the parse summary counts events below it, and
free-text sources whose average is below it are suggested for learning (one threshold, so
the two never disagree)."""

DEFAULT_WORKERS: Final = max(1, min(8, (os.cpu_count() or 2) - 1))


@dataclass(frozen=True)
class ParseOptions:
    """Everything that shapes a parse run (picklable, sent to worker processes)."""

    timezone: str = "UTC"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    workers: int = DEFAULT_WORKERS
    max_record_lines: int = 500
    masking_file: Path | None = None
    state_file: Path | None = None
    batch_rows: int = 10_000
    formats_dirs: tuple[Path, ...] = ()
    """User format plugin folders, later ones overriding earlier ones and built-ins."""

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown timezone {self.timezone!r}") from error
        if self.workers < 1:
            raise ValueError("workers must be at least 1")
        if self.max_record_lines < 1:
            raise ValueError("max_record_lines must be at least 1")
        if self.batch_rows < 1:
            raise ValueError("batch_rows must be at least 1")
