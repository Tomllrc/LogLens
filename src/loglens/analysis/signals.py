"""Issue signals: which templates are candidate issues, and the impact of their events.

The rules live in ``signals.yaml`` (overridable with ``--signals FILE``) and look only at
normalized levels and template text, so they work for every log format.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

import yaml

from loglens.schema.levels import LEVELS

ISSUE_LEVELS: Final = frozenset({"WARN", "ERROR", "FATAL"})
"""Levels that make a template a candidate issue on their own."""

DEFAULT_SIGNALS_FILE: Final = resources.files("loglens.analysis").joinpath("signals.yaml")


class SignalsError(ValueError):
    """A signals file cannot be read or is invalid."""


@dataclass(frozen=True)
class Signals:
    """Failure keywords and level weights."""

    keywords: tuple[str, ...]
    level_weights: Mapping[str, float]
    unknown_keyword_weight: float
    _regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        alternatives = "|".join(f"(?:{keyword})" for keyword in self.keywords)
        regex = re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])", re.IGNORECASE)
        object.__setattr__(self, "_regex", regex)

    def matches_keyword(self, template: str) -> bool:
        """Whether the template contains a failure keyword (whole word)."""
        return self._regex.search(template) is not None

    def is_candidate(self, by_level: Mapping[str, int], template: str) -> bool:
        """Whether a template with these level counts is a candidate issue."""
        if any(by_level.get(level, 0) > 0 for level in ISSUE_LEVELS):
            return True
        return by_level.get("UNKNOWN", 0) > 0 and self.matches_keyword(template)

    def impact(self, by_level: Mapping[str, int], template: str) -> float:
        """Weighted event count: each event weighs its level's weight."""
        total = sum(self.level_weights.get(level, 0.0) * n for level, n in by_level.items())
        if by_level.get("UNKNOWN", 0) and self.matches_keyword(template):
            total += self.unknown_keyword_weight * by_level["UNKNOWN"]
        return total


def load_signals(path: Path | None = None) -> Signals:
    """Read a signals file (the packaged defaults when ``path`` is None)."""
    source: Traversable | Path = path if path is not None else DEFAULT_SIGNALS_FILE
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SignalsError(f"{source}: cannot read signals: {error}") from error
    if not isinstance(data, dict):
        raise SignalsError(f"{source}: expected a mapping with failure_keywords and weights")
    keywords = data.get("failure_keywords")
    if (
        not isinstance(keywords, list)
        or not keywords
        or not all(isinstance(k, str) and k for k in keywords)
    ):
        raise SignalsError(f"{source}: 'failure_keywords' must be a non-empty list of strings")
    for keyword in keywords:
        try:
            re.compile(keyword)
        except re.error as error:
            message = f"{source}: keyword {keyword!r} does not compile: {error}"
            raise SignalsError(message) from error
    weights = data.get("level_weights")
    if not isinstance(weights, dict) or not weights:
        raise SignalsError(f"{source}: 'level_weights' must map levels to numbers")
    for level, weight in weights.items():
        if level not in LEVELS:
            raise SignalsError(f"{source}: unknown level {level!r} in level_weights")
        if not _is_weight(weight):
            raise SignalsError(f"{source}: weight of {level} must be a number >= 0")
    unknown_weight = data.get("unknown_keyword_weight", 0)
    if not _is_weight(unknown_weight):
        raise SignalsError(f"{source}: 'unknown_keyword_weight' must be a number >= 0")
    return Signals(
        tuple(keywords),
        {level: float(weight) for level, weight in weights.items()},
        float(unknown_weight),
    )


def _is_weight(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
