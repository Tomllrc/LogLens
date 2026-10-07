"""The generic parser: best-effort extraction from free text in any layout.

Used for files no known format matches, and as a fallback for records a known parser
handled with low confidence. From the first line of a record it extracts:

- a timestamp: the earliest timestamp-looking text within the first characters, in any
  of the common layouts (ISO 8601, syslog, Apache, epoch, BGL, HDFS, Android...);
- a level: a level word among the first tokens after the timestamp (upper case may be a
  little further, as in ``RAS KERNEL INFO``; other cases must be close or bracketed);
- a component: the token right after the level ending in ``:`` (``dfs.DataNode:``), or a
  syslog-style ``name[pid]:`` tag among the first tokens.

The rest is the message (following lines are kept). Confidence grows with what was
found, so files without timestamps are visibly low-confidence.
"""

import re
from typing import ClassVar, Final

from loglens.parsers.models import ParsedEvent
from loglens.split.models import Record

ANSI_ESCAPE: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
"""ANSI terminal sequences (colors, cursor moves, OSC titles)."""

TIMESTAMP_WINDOW: Final = 96
"""Characters at the start of a line searched for a timestamp."""

_MONTH: Final = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_DAY: Final = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"

# Ordered from most to least specific: at the same position, the first alternative wins.
GENERIC_TIMESTAMP: Final = re.compile(
    "|".join(
        rf"(?P<{name}>{pattern})"
        for name, pattern in (
            (
                "iso",
                r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?",
            ),
            ("bgl", r"\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}\.\d{1,6}"),
            ("slash", r"\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?"),
            ("apache", rf"{_DAY} {_MONTH} [ \d]\d \d{{2}}:\d{{2}}:\d{{2}}(?:\.\d+)? \d{{4}}"),
            ("clf", rf"\d{{2}}/{_MONTH}/\d{{4}}:\d{{2}}:\d{{2}}:\d{{2}} [+-]\d{{4}}"),
            ("syslog", rf"{_MONTH} +\d{{1,2}} \d{{2}}:\d{{2}}:\d{{2}}(?:\.\d{{1,6}})?"),
            ("dmy", r"\d{1,2}/\d{1,2}/\d{4},? \d{1,2}:\d{2}:\d{2}(?: [AP]M)?"),
            ("yymmdd", r"\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}"),
            ("compact", r"\d{8}-\d{1,2}:\d{1,2}:\d{1,2}:\d{1,3}"),
            ("hdfs", r"\d{6} \d{6}"),
            ("android", r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}"),
            ("dotted", r"\d{2}\.\d{2} \d{2}:\d{2}:\d{2}"),
            ("epoch", r"\d{10}(?:\.\d{1,9})?|\d{13}"),
            ("time", r"\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?"),
        )
    ).replace("(?P<", r"(?<![\w.:/-])(?P<")
    + r"(?![\w:])"
)

GENERIC_TIMESTAMP_HINTS: Final = (
    "iso8601",
    "%Y-%m-%d %H:%M:%S,%f",
    "%Y-%m-%d-%H.%M.%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S,%f",
    "%a %b %d %H:%M:%S %Y",
    "%a %b %d %H:%M:%S.%f %Y",
    "%d/%b/%Y:%H:%M:%S %z",
    "%b %d %H:%M:%S",
    "%b %d %H:%M:%S.%f",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y, %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%y/%m/%d %H:%M:%S",
    "%Y%m%d-%H:%M:%S:%f",
    "%y%m%d %H%M%S",
    "%m-%d %H:%M:%S.%f",
    "%m.%d %H:%M:%S",
    "epoch",
    "epoch_ms",
    "%H:%M:%S",
    "%H:%M:%S.%f",
    "%H:%M:%S,%f",
)
"""``strptime`` hints covering every layout :data:`GENERIC_TIMESTAMP` recognizes."""

_LEVEL_WORDS: Final = (
    "TRACE|DEBUG|INFO|INFORMATION|NOTICE|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|CRIT"
    "|SEVERE|ALERT|EMERG|EMERGENCY|PANIC|VERBOSE"
)
_UPPER_LEVEL: Final = re.compile(rf"\[?<?({_LEVEL_WORDS})>?\]?:?")
_ANY_CASE_LEVEL: Final = re.compile(rf"\[?<?({_LEVEL_WORDS})>?\]?:?", re.IGNORECASE)
UPPER_LEVEL_WINDOW: Final = 12
"""Tokens after the timestamp in which an upper-case level word is accepted."""
ANY_CASE_LEVEL_WINDOW: Final = 3
"""Tokens after the timestamp in which a level word of any case is accepted."""

_COMPONENT_AFTER_LEVEL: Final = re.compile(r"([A-Za-z_][\w.$/\-]*(?:\([\w.\-]*\))?):")
_SYSLOG_TAG: Final = re.compile(r"([A-Za-z_][\w.$/\-]*(?:\([\w.\-]*\))?)\[(\d+)\]:")
SYSLOG_TAG_WINDOW: Final = 8
_SEPARATORS: Final = " \t-:|,"
_TOKEN: Final = re.compile(r"\S+")

BASE_CONFIDENCE: Final = 0.3
TIMESTAMP_CONFIDENCE: Final = 0.35
LEVEL_CONFIDENCE: Final = 0.15
COMPONENT_CONFIDENCE: Final = 0.1


def strip_ansi(text: str) -> str:
    """Remove ANSI color and cursor sequences."""
    return ANSI_ESCAPE.sub("", text) if "\x1b" in text else text


def find_timestamp(line: str) -> re.Match[str] | None:
    """The earliest timestamp-looking match near the start of ``line``."""
    return GENERIC_TIMESTAMP.search(line, 0, TIMESTAMP_WINDOW)


class GenericParser:
    """Best-effort parser for free text; never fails, only reports low confidence."""

    format_name: ClassVar[str] = "free_text"

    def parse(self, record: Record) -> ParsedEvent:
        """Extract timestamp, level, component and message from a record."""
        text = strip_ansi(record.text)
        first, newline, rest = text.partition("\n")
        extra: dict[str, str] = {}

        timestamp = None
        position = 0
        found = find_timestamp(first)
        if found is not None:
            timestamp = found.group()
            prefix = first[: found.start()].strip(_SEPARATORS + "[(")
            if prefix:
                extra["prefix"] = prefix
            position = found.end()
            while position < len(first) and first[position] in ")]>":
                position += 1  # the closing bracket of "[timestamp]"

        tokens = _tokens(first, position)
        level, level_index = _find_level(tokens)
        component = None
        cursor = level_index + 1 if level_index is not None else 0
        if level_index is not None:
            component, cursor = _component_after(tokens, cursor)
        if component is None:
            tag = _syslog_tag(tokens)
            if tag is not None:
                component, pid, tag_index = tag
                extra["pid"] = pid
                if level_index is None or tag_index >= cursor:
                    cursor = tag_index + 1

        start = tokens[cursor][0] if cursor < len(tokens) else len(first)
        message = first[start:].strip(_SEPARATORS) if (timestamp or level or component) else first
        message = (message + newline + rest).strip()

        confidence = BASE_CONFIDENCE
        confidence += TIMESTAMP_CONFIDENCE if timestamp else 0.0
        confidence += LEVEL_CONFIDENCE if level else 0.0
        confidence += COMPONENT_CONFIDENCE if component else 0.0
        return ParsedEvent(
            message=message,
            timestamp_raw=timestamp,
            level_raw=level,
            component=component,
            extra=extra,
            confidence=round(confidence, 2),
        )


def _tokens(line: str, start: int) -> list[tuple[int, str]]:
    """Whitespace-separated tokens of ``line`` from ``start``, with their offsets."""
    return [(m.start(), m.group()) for m in _TOKEN.finditer(line, start)]


def _find_level(tokens: list[tuple[int, str]]) -> tuple[str | None, int | None]:
    for index, (_, token) in enumerate(tokens[:UPPER_LEVEL_WINDOW]):
        stripped = token.strip(",;")
        upper = _UPPER_LEVEL.fullmatch(stripped)
        if upper is not None:
            return upper.group(1), index
        bracketed = stripped.startswith(("[", "<")) or stripped.endswith(":")
        near = index < ANY_CASE_LEVEL_WINDOW
        any_case = _ANY_CASE_LEVEL.fullmatch(stripped)
        if any_case is not None and (near or bracketed):
            return any_case.group(1), index
    return None, None


def _component_after(tokens: list[tuple[int, str]], index: int) -> tuple[str | None, int]:
    """A ``name:`` token right after the level, skipping bracketed ``[thread]`` groups."""
    while index < len(tokens) and tokens[index][1] in {"-", "|", ":"}:
        index += 1
    while index < len(tokens) and tokens[index][1].startswith("["):
        index += 1
        if not tokens[index - 1][1].endswith("]"):  # a bracket spanning several tokens
            while index < len(tokens) and not tokens[index - 1][1].endswith("]"):
                index += 1
    while index < len(tokens) and tokens[index][1] in {"-", "|", ":"}:
        index += 1
    if index < len(tokens):
        found = _COMPONENT_AFTER_LEVEL.fullmatch(tokens[index][1])
        if found is not None:
            return found.group(1), index + 1
    return None, index


def _syslog_tag(tokens: list[tuple[int, str]]) -> tuple[str, str, int] | None:
    for index, (_, token) in enumerate(tokens[:SYSLOG_TAG_WINDOW]):
        found = _SYSLOG_TAG.fullmatch(token)
        if found is not None:
            return found.group(1), found.group(2), index
    return None
