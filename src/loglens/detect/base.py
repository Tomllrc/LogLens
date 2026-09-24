"""The detector abstraction and scoring helpers shared by all detectors."""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Detector:
    """A candidate format.

    ``score`` receives the texts of a file's sample records and returns a value in
    [0, 1], normally the share of (non-blank) records it can structurally parse.
    ``describe``, optional, is called on the same sample only when this detector wins,
    and returns format-specific details for later stages (e.g. a CSV delimiter).
    """

    name: str
    score: Callable[[Sequence[str]], float] = field(compare=False)
    describe: Callable[[Sequence[str]], Mapping[str, str]] | None = field(
        default=None, compare=False
    )


def non_blank(sample: Sequence[str]) -> list[str]:
    """The records of a sample that contain something other than whitespace."""
    return [text for text in sample if text.strip()]


def share(sample: Sequence[str], predicate: Callable[[str], bool]) -> float:
    """Share of the non-blank records of ``sample`` satisfying ``predicate`` (0 if none)."""
    records = non_blank(sample)
    if not records:
        return 0.0
    return sum(1 for text in records if predicate(text)) / len(records)


def share_detector(name: str, predicate: Callable[[str], bool]) -> Detector:
    """A detector scoring the share of records accepted by ``predicate``."""
    return Detector(name, lambda sample: share(sample, predicate))


def regex_detector(name: str, pattern: re.Pattern[str]) -> Detector:
    """A detector scoring the share of records whose text matches ``pattern`` at its start."""
    return share_detector(name, lambda text: pattern.match(text) is not None)
