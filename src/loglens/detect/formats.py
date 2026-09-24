"""Choose a file's format from a sample of its records."""

import math
from collections.abc import Callable, Mapping, Sequence

from loglens.detect.base import Detector
from loglens.detect.models import Detection
from loglens.detect.registry import DEFAULT_DETECTORS, DEFAULT_MIN_CONFIDENCE, FREE_TEXT
from loglens.detect.text import FREE_TEXT_SCORE

ErrorHandler = Callable[[str, Exception], None]
"""Called with a detector's name and the exception it raised."""


def _clamp(value: float) -> float:
    """Force a score into [0, 1]; NaN becomes 0."""
    if math.isnan(value):
        return 0.0
    return min(1.0, max(0.0, value))


def score_candidates(
    sample: Sequence[str],
    detectors: Sequence[Detector] = DEFAULT_DETECTORS,
    on_error: ErrorHandler | None = None,
) -> dict[str, float]:
    """Score every detector on the sample, keeping the detectors' order.

    A detector that raises, or returns something that is not a number, scores 0 (and
    ``on_error`` is told); scores are clamped to [0, 1].
    """
    scores: dict[str, float] = {}
    for detector in detectors:
        try:
            scores[detector.name] = _clamp(float(detector.score(sample)))
        except Exception as error:  # a plugin detector must never crash the pipeline
            scores[detector.name] = 0.0
            if on_error is not None:
                on_error(detector.name, error)
    return scores


def choose(
    scores: Mapping[str, float], min_confidence: float = DEFAULT_MIN_CONFIDENCE
) -> Detection:
    """Pick the best-scoring candidate; on exact ties the one listed first wins.

    ``free_text`` wins when no other candidate reaches ``min_confidence`` (or all score 0).
    """
    best_name, best_score = FREE_TEXT, 0.0
    for name, score in scores.items():
        if name != FREE_TEXT and score > best_score:
            best_name, best_score = name, score
    if best_name == FREE_TEXT or best_score < min_confidence:
        return Detection(FREE_TEXT, scores.get(FREE_TEXT, FREE_TEXT_SCORE), dict(scores))
    return Detection(best_name, best_score, dict(scores))


def detect_format(
    sample: Sequence[str],
    *,
    detectors: Sequence[Detector] = DEFAULT_DETECTORS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    on_error: ErrorHandler | None = None,
) -> Detection:
    """Detect a file's format from the texts of a sample of its records.

    Every candidate is scored (see :func:`score_candidates`). The highest score wins if
    it reaches ``min_confidence``; exact ties go to the candidate listed first (the more
    specific one). Otherwise the result is ``free_text`` with its low fixed confidence.
    """
    return choose(score_candidates(sample, detectors, on_error), min_confidence)
