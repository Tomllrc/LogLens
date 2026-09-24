"""Regexes for values that look like timestamps or log levels, shared by parsers."""

import re
from typing import Final

_MONTH: Final = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_TIMESTAMP: Final = (
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"  # ISO 8601
    r"|\d{4}-\d{2}-\d{2}"  # date only
    r"|\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"  # 2026/01/31 10:00:00
    rf"|{_MONTH} +\d{{1,2}} \d{{2}}:\d{{2}}:\d{{2}}"  # syslog: Jan  1 10:00:00
    r"|\d{10}(?:\d{3})?(?:\.\d+)?"  # epoch seconds or milliseconds
)
_LEVEL: Final = (
    r"trace|debug|info|information|notice|warn|warning|err|error|fatal|crit|critical"
    r"|severe|alert|emerg|emergency|panic|verbose"
)

TIMESTAMP_VALUE: Final = re.compile(_TIMESTAMP)
"""Use with ``fullmatch``: a whole value that is a timestamp."""

LEVEL_VALUE: Final = re.compile(_LEVEL, re.IGNORECASE)
"""Use with ``fullmatch``: a whole value that is a level word (any case)."""

NUMERIC_LEVEL: Final = re.compile(r"\d{1,3}")
"""Numeric levels: syslog severities (0-7), Windows levels (0-5), bunyan/pino (10-60)."""

PREFIX_TIMESTAMP: Final = re.compile(rf"[\[(]?(?P<ts>{_TIMESTAMP})[\])]?(?=[\s:|]|\Z)[\s:|]*")
"""A timestamp at the start of a text prefix, with optional brackets and separator."""

PREFIX_LEVEL: Final = re.compile(
    rf"[\[(<]?(?P<level>{_LEVEL})[\])>]?(?=[\s:|,-]|\Z)[\s:|,-]*", re.IGNORECASE
)
"""A level token at the start of a text prefix, with optional brackets and separator."""


def split_text_prefix(prefix: str) -> tuple[str | None, str | None, str]:
    """Split a text prefix into (timestamp, level, rest), each part optional.

    ``"2026-01-01T10:00:00Z INFO payments "`` gives
    ``("2026-01-01T10:00:00Z", "INFO", "payments")``. The rest is trimmed of spaces and
    common separators.
    """
    rest = prefix.strip()
    timestamp = level = None
    if match := PREFIX_TIMESTAMP.match(rest):
        timestamp, rest = match.group("ts"), rest[match.end() :]
    if match := PREFIX_LEVEL.match(rest):
        level, rest = match.group("level"), rest[match.end() :]
    return timestamp, level, rest.strip(" \t:|-")
