"""Data types produced by parsers."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

MALFORMED_CONFIDENCE: Final = 0.1
"""Confidence of an event whose record could not be parsed at all."""


@dataclass(frozen=True)
class ParsedEvent:
    """What a parser extracted from one record: a partial unified event.

    ``timestamp_raw`` and ``level_raw`` are the original texts, not yet normalized.
    ``extra`` holds every field that was not mapped to a schema column, with values
    rendered as strings (non-strings as JSON). ``confidence`` is in [0, 1].

    ``is_event`` is False for records that carry no event, such as a CSV header row or
    an XML declaration or closing wrapper tag; callers should drop them. ``error``
    explains why a record was (partly) malformed, for the warnings report.
    """

    message: str
    timestamp_raw: str | None = None
    level_raw: str | None = None
    component: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)
    confidence: float = 1.0
    is_event: bool = True
    error: str | None = None
