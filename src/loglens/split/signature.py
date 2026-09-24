"""Learn, per file, which lines start a new record.

Each candidate signature is a regex anchored at column 0. A candidate is scored on a
sample of lines:

- ``coverage``: share of lines it matches (record starts);
- ``explained``: share of lines that either match it or look like a continuation
  (indented, ``at ...``, ``Caused by:``, ``Traceback ...``, exception names, ...).

A candidate is plausible if it matches at least one line and either explains most of
the sample or covers a sizeable share of it (the latter admits unindented
continuations such as multi-line SQL). The plausible candidate with the best
(explained, coverage) wins; exact ties go to the more specific signature.

Without a plausible candidate, a fallback applies: only unmistakable continuation
lines are attached to the previous record, so a file where every line looks the same
gets one record per line. If most of the sample is indented, every line is a record.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

MIN_EXPLAINED: Final = 0.8
MIN_COVERAGE: Final = 0.3
MAX_INDENTED_SHARE_FOR_FALLBACK: Final = 0.5

_MONTH: Final = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
)
_DAY: Final = r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]{0,6}\b"
_TIME: Final = r"\d{1,2}:\d{2}(?::\d{2})?"

TIMESTAMP_START: Final = re.compile(
    rf"""^[\[(]?(?:
        \d{{4}}[-/.]\d{{1,2}}[-/.]\d{{1,2}}(?!\d)             # 2026-01-31, 2026/01/31
      | \d{{1,2}}[-/.]\d{{1,2}}[-/.]\d{{2,4}}[\sT,]+{_TIME}   # 31/01/2026 10:00
      | \d{{1,2}}[-/\s]{_MONTH}[-/\s]\d{{2,4}}               # 31/Jan/2026, 31 Jan 2026
      | (?:{_DAY},?\s+)?{_MONTH}\s+\d{{1,2}}\s+{_TIME}        # Jan 31 10:00:00 (syslog)
      | {_DAY},?\s+\d{{1,2}}\s+{_MONTH}                       # Mon, 31 Jan
      | \d{{1,2}}:\d{{2}}:\d{{2}}                             # 10:00:00
      | [IWEF]\d{{4}}\s+{_TIME}                               # glog: I0131 10:00:00
      | \d{{10}}(?:\d{{3}})?(?:\.\d+)?(?!\d)                  # epoch seconds or millis
    )""",
    re.VERBOSE | re.IGNORECASE,
)
LEVEL_START: Final = re.compile(
    r"^[\[(<]?(?:trace|debug|info|notice|warn(?:ing)?|err(?:or)?|fatal|crit(?:ical)?"
    r"|severe|alert|emerg(?:ency)?|panic|fine(?:r|st)?|config)[\])>]?(?=[\s:|,\]-]|$)",
    re.IGNORECASE,
)
BRACKET_START: Final = re.compile(r"^(?:\[[^\]\n]{1,100}\]|<\d{1,3}>)")
JSON_START: Final = re.compile(r"^\{")
XML_START: Final = re.compile(r"^<[A-Za-z_?]")

_STRONG_CONTINUATION: Final = re.compile(r"^(?:\s|at\s|Caused by:|Suppressed:|\.\.\.|[}\]]|</)")
_WEAK_CONTINUATION: Final = re.compile(
    r"^(?:Traceback \(most recent call last\):"
    r"|During handling of the above exception"
    r"|The above exception was the direct cause"
    r"|(?:[\w$]+\.)*[\w$]*(?:Exception|Error|Throwable)\b)"
)


@dataclass(frozen=True)
class Signature:
    """A rule deciding whether a line starts a new record."""

    name: str
    is_start: Callable[[str], bool] = field(compare=False)


def is_strong_continuation(text: str) -> bool:
    """True for lines that can only continue a record: blank, indented, stack frames, ``}``,
    XML closing tags (``</Event>``)."""
    return not text or _STRONG_CONTINUATION.match(text) is not None


def looks_like_continuation(text: str) -> bool:
    """True for lines typical inside a multi-line record (strong or weak evidence)."""
    return is_strong_continuation(text) or _WEAK_CONTINUATION.match(text) is not None


def _regex_signature(name: str, pattern: re.Pattern[str]) -> Signature:
    return Signature(name, lambda text: pattern.match(text) is not None)


TIMESTAMP: Final = _regex_signature("timestamp", TIMESTAMP_START)
LEVEL: Final = _regex_signature("level", LEVEL_START)
JSON: Final = _regex_signature("json", JSON_START)
XML: Final = _regex_signature("xml", XML_START)
BRACKET: Final = _regex_signature("bracket", BRACKET_START)
UNINDENTED: Final = Signature("unindented", lambda text: not is_strong_continuation(text))
EVERY_LINE: Final = Signature("every-line", lambda _text: True)

CANDIDATES: Final = (TIMESTAMP, LEVEL, JSON, XML, BRACKET)
"""Candidate signatures, most specific first (earlier wins exact ties)."""


def learn_signature(sample: Iterable[str]) -> Signature:
    """Choose the signature that best explains a sample of a file's lines."""
    lines = [text for text in sample if text.strip()]
    if not lines:
        return UNINDENTED

    total = len(lines)
    continuation = [looks_like_continuation(text) for text in lines]
    best: Signature | None = None
    best_key: tuple[float, float, int] | None = None
    for rank, candidate in enumerate(CANDIDATES):
        starts = [candidate.is_start(text) for text in lines]
        matched = sum(starts)
        if matched == 0:
            continue
        coverage = matched / total
        explained = sum(s or c for s, c in zip(starts, continuation, strict=True)) / total
        if explained < MIN_EXPLAINED and coverage < MIN_COVERAGE:
            continue
        key = (explained, coverage, -rank)
        if best_key is None or key > best_key:
            best, best_key = candidate, key
    if best is not None:
        return best

    indented_share = sum(is_strong_continuation(text) for text in lines) / total
    return UNINDENTED if indented_share <= MAX_INDENTED_SHARE_FOR_FALLBACK else EVERY_LINE
