"""Data types produced by format detection."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NamedTuple

from loglens.detect.base import Detector
from loglens.detect.registry import DEFAULT_DETECTORS, DEFAULT_MIN_CONFIDENCE
from loglens.ingest.models import FileIssue
from loglens.split.models import Record


@dataclass(frozen=True)
class Detection:
    """The format chosen for one file.

    ``confidence`` is in [0, 1]. ``scores`` maps every candidate format to its score,
    in priority order (most specific first), for debugging.
    """

    format_name: str
    confidence: float
    scores: Mapping[str, float] = field(default_factory=dict, compare=False)


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

    sample_records: int = 200
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

    detections: dict[str, Detection] = field(default_factory=dict)
    warnings: list[FileIssue] = field(default_factory=list)

    @property
    def format_counts(self) -> Counter[str]:
        """Number of files per detected format."""
        return Counter(d.format_name for d in self.detections.values())
