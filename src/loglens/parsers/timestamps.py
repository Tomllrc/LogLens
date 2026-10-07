"""Parse raw timestamps using format hints (``strptime`` patterns or keywords)."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo
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
        parsed = _parse_with(text, hint)
        if parsed is not None:
            return parsed
    return None


def _parse_with(text: str, hint: str) -> datetime | None:
    try:
        if hint == "iso8601":
            return datetime.fromisoformat(text)
        if hint in EPOCH_DIVISORS:
            return datetime.fromtimestamp(float(text) / EPOCH_DIVISORS[hint], tz=UTC)
        if "%" in hint:
            if _has_year(hint):
                return datetime.strptime(text, hint)
            return datetime.strptime(f"{YEARLESS_PLACEHOLDER} {text}", f"%Y {hint}")
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _has_year(hint: str) -> bool:
    return "%Y" in hint or "%y" in hint


def _has_date(hint: str) -> bool:
    return hint in KEYWORD_HINTS or any(d in hint for d in ("%d", "%j", "%b", "%m", "%a"))


_EPOCH_DIGITS: Final = {10: 1.0, 13: 1e3, 16: 1e6, 19: 1e9}


def _from_epoch(text: str) -> datetime | None:
    """Unix time written as 10 (s), 13 (ms), 16 (µs) or 19 (ns) digits, or s with decimals."""
    whole, _, fraction = text.partition(".")
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        return None
    divisor = _EPOCH_DIGITS.get(len(whole))
    if divisor is None or (fraction and divisor != 1.0):
        return None
    try:
        return datetime.fromtimestamp(float(text) / divisor, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


class TimestampNormalizer:
    """Turns raw timestamps into aware UTC datetimes, for one file.

    Hints are tried in order, the last one that worked first (files are consistent).
    Bare Unix times are recognized by their digit count. Naive timestamps are read in
    ``tz``. A timestamp without a year (syslog) gets the year of ``reference``, or the
    year before if that would put it more than a day after ``reference``. A time
    without a date cannot be placed and gives None.
    """

    def __init__(self, hints: Sequence[str], *, tz: tzinfo, reference: datetime) -> None:
        self._hints = list(dict.fromkeys(hints))
        self._tz = tz
        self._reference = reference

    def to_utc(self, raw: str | None) -> datetime | None:
        """The UTC datetime for ``raw``, or None if it cannot be read or placed."""
        if not raw:
            return None
        text = raw.strip()
        epoch = _from_epoch(text)
        if epoch is not None:
            return epoch
        for index, hint in enumerate(self._hints):
            parsed = _parse_with(text, hint)
            if parsed is None:
                continue
            if index:
                self._hints.insert(0, self._hints.pop(index))
            return self._place(parsed, hint)
        return None

    def _place(self, parsed: datetime, hint: str) -> datetime | None:
        if not _has_date(hint):
            return None
        if "%" in hint and not _has_year(hint):
            dated = self._with_inferred_year(parsed)
            if dated is None:
                return None
            parsed = dated
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._tz)
        return parsed.astimezone(UTC)

    def _with_inferred_year(self, parsed: datetime) -> datetime | None:
        reference = self._reference
        limit = reference + timedelta(days=1)
        for year in range(reference.year, reference.year - 8, -1):  # Feb 29: a leap year
            try:
                candidate = parsed.replace(year=year)
            except ValueError:  # Feb 29 in a non-leap year
                continue
            aware = candidate if candidate.tzinfo else candidate.replace(tzinfo=self._tz)
            if aware <= limit:
                return candidate
        return None
