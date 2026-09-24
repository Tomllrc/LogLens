"""Data types produced by format detection."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NamedTuple

from loglens.detect.base import Detector
from loglens.detect.registry import (
    DEFAULT_DETECTORS,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_SAMPLE_RECORDS,
    FREE_TEXT,
)
from loglens.ingest.models import FileIssue
from loglens.split.models import Record


@dataclass(frozen=True)
class DetectionResult:
    """The format chosen for one file.

    ``confidence`` is in [0, 1]. ``scores_by_format`` maps every candidate format to its
    score, in priority order (most specific first). ``details`` holds what the winning
    detector learned about the file, when it describes itself (for ``csv``: delimiter,
    column count, and whether there is a header).
    """

    format_name: str
    confidence: float
    scores_by_format: Mapping[str, float] = field(default_factory=dict, compare=False)
    details: Mapping[str, str] = field(default_factory=dict, compare=False)

    @property
    def runner_up(self) -> tuple[str, float] | None:
        """The best-scoring other candidate (``free_text`` excluded), if any scored above 0."""
        others = [
            (name, score)
            for name, score in self.scores_by_format.items()
            if name not in (self.format_name, FREE_TEXT) and score > 0
        ]
        return max(others, key=lambda item: item[1], default=None)


class DetectedRecord(NamedTuple):
    """A record, unchanged, tagged with the format detected for its file."""

    record: Record
    format_name: str
    confidence: float


@dataclass(frozen=True)
class DetectOptions:
    """Settings for :func:`loglens.detect.iter_detected`.

    The first ``sample_records`` records of each file are scored by each of
    ``detectors`` (most specific first). The best candidate wins if its score reaches
    ``min_confidence``; otherwise the file is ``free_text``.
    """

    sample_records: int = DEFAULT_SAMPLE_RECORDS
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    detectors: tuple[Detector, ...] = DEFAULT_DETECTORS

    def __post_init__(self) -> None:
        if self.sample_records < 1:
            raise ValueError("sample_records must be at least 1")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        names = [detector.name for detector in self.detectors]
        if len(set(names)) != len(names):
            raise ValueError("detector names must be unique")


@dataclass
class DetectReport:
    """What happened during detection. Filled in progressively while records are consumed."""

    detections: dict[str, DetectionResult] = field(default_factory=dict)
    warnings: list[FileIssue] = field(default_factory=list)

    @property
    def format_counts(self) -> Counter[str]:
        """Number of files per detected format."""
        return Counter(d.format_name for d in self.detections.values())
