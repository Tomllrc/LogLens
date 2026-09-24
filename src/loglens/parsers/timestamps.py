"""Parse raw timestamps using format hints (``strptime`` patterns or keywords)."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

EPOCH_DIVISORS: Final = {"epoch": 1.0, "epoch_ms": 1e3, "epoch_us": 1e6}
KEYWORD_HINTS: Final = ("iso8601", *EPOCH_DIVISORS)
"""Hints that are not ``strptime`` patterns: ISO 8601, and Unix time in s, ms or µs."""

YEARLESS_PLACEHOLDER: Final = 2000
"""Year assumed for timestamps that have none (a leap year, so Feb 29 is valid)."""


def is_valid_hint(hint: str) -> bool:
    """True for a keyword hint or anything that looks like a ``strptime`` pattern."""
    return hint in KEYWORD_HINTS or "%" in hint


def parse_timestamp(raw: str, hints: Sequence[str]) -> datetime | None:
    """Parse ``raw`` with the first hint that fits, or return None.

    Epoch hints give aware UTC datetimes; other hints give whatever the text holds
    (naive when it carries no offset). A pattern without a year (syslog's
    ``%b %d %H:%M:%S``) is read in the placeholder year :data:`YEARLESS_PLACEHOLDER`, a
    leap year so that Feb 29 parses; the real year must come from context.
    """
    text = raw.strip()
    for hint in hints:
        try:
            if hint == "iso8601":
                return datetime.fromisoformat(text)
            if hint in EPOCH_DIVISORS:
                return datetime.fromtimestamp(float(text) / EPOCH_DIVISORS[hint], tz=UTC)
            if "%" in hint:
                if "%Y" in hint or "%y" in hint:
                    return datetime.strptime(text, hint)
                return datetime.strptime(f"{YEARLESS_PLACEHOLDER} {text}", f"%Y {hint}")
        except (ValueError, OverflowError, OSError):
            continue
    return None
