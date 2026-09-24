"""Format detection: score candidate log formats per file."""

from loglens.detect.base import Detector, regex_detector, share, share_detector
from loglens.detect.formats import choose, detect_format, score_candidates
from loglens.detect.models import DetectedRecord, Detection, DetectOptions, DetectReport
from loglens.detect.registry import DEFAULT_DETECTORS, DEFAULT_MIN_CONFIDENCE, FREE_TEXT
from loglens.detect.stream import iter_detected
from loglens.detect.structured import CsvDialect, sniff_csv

__all__ = [
    "DEFAULT_DETECTORS",
    "DEFAULT_MIN_CONFIDENCE",
    "FREE_TEXT",
    "CsvDialect",
    "DetectOptions",
    "DetectReport",
    "DetectedRecord",
    "Detection",
    "Detector",
    "choose",
    "detect_format",
    "iter_detected",
    "regex_detector",
    "score_candidates",
    "share",
    "share_detector",
    "sniff_csv",
]
