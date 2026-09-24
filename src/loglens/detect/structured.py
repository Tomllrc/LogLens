"""Detectors for self-describing structured formats: JSON, XML, logfmt, delimited (CSV).

Each predicate decides whether one record parses as the format. They never raise:
anything unparseable is simply "not this format".
"""

import csv
import io
import json
import re
from collections import Counter
from collections.abc import Sequence
from typing import Final, NamedTuple
from xml.parsers import expat

from loglens.detect.base import Detector, non_blank, share_detector

# --- JSON -------------------------------------------------------------------------


def is_json_object(text: str) -> bool:
    """True if the record is exactly one JSON object (arrays and scalars are rejected)."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        return isinstance(json.loads(stripped), dict)
    except (ValueError, RecursionError):
        return False


# --- XML --------------------------------------------------------------------------

_XML_DECLARATION: Final = re.compile(r"<\?xml\b[^>]*\?>\s*\Z")
_XML_CLOSING_ONLY: Final = re.compile(r"(?:\s*</[^\s<>/]+\s*>)+\s*\Z")
_XML_FORBIDDEN: Final = ("<!DOCTYPE", "<!ENTITY")


def is_xml_element(text: str) -> bool:
    """True if the record is an XML element.

    A single-line record must be one complete, well-formed element. A multi-line record
    may also be a well-formed *prefix* of one (an element cut by the record-size cap, or
    a wrapper such as ``<Events>`` whose children follow). A record made only of closing
    tags, or only of an ``<?xml ...?>`` declaration, also counts.

    Documents with a DOCTYPE or entity declarations are rejected without being parsed,
    so no entity is ever expanded and no external resource is ever fetched.
    """
    stripped = text.strip()
    if not (stripped.startswith("<") and stripped.endswith(">")):
        return False
    if _XML_DECLARATION.match(stripped) or _XML_CLOSING_ONLY.match(stripped):
        return True
    upper = stripped.upper()
    if any(marker in upper for marker in _XML_FORBIDDEN):
        return False
    started = False

    def on_start(_name: str, _attributes: dict[str, str]) -> None:
        nonlocal started
        started = True

    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    if hasattr(parser, "SetReparseDeferralEnabled"):  # expat >= 2.6: report errors eagerly
        parser.SetReparseDeferralEnabled(False)
    parser.StartElementHandler = on_start
    try:
        parser.Parse(stripped, "\n" not in stripped)  # final unless a prefix is acceptable
    except (expat.ExpatError, ValueError, RecursionError):
        return False
    return started


# --- logfmt -----------------------------------------------------------------------

_QUOTED: Final = r'"(?:[^"\\]|\\.)*"'
LOGFMT_TOKEN: Final = re.compile(
    rf"(?P<pair>[A-Za-z_@][\w.\-/@]*=(?:{_QUOTED}|[^\s\"]*))(?=\s|\Z)|{_QUOTED}|\S+"
)
LOGFMT_MIN_PAIRS: Final = 2
LOGFMT_MIN_PAIR_SHARE: Final = 0.75


def is_logfmt(text: str) -> bool:
    """True if the record is a single line made mostly of ``key=value`` pairs.

    At least ``LOGFMT_MIN_PAIRS`` pairs, and at least ``LOGFMT_MIN_PAIR_SHARE`` of the
    whitespace-separated tokens (a quoted value counts as part of its pair) must be pairs,
    so one ``a=b`` inside a sentence is not enough.
    """
    stripped = text.strip()
    if "\n" in stripped:
        return False
    tokens = [match.group("pair") is not None for match in LOGFMT_TOKEN.finditer(stripped)]
    pairs = sum(tokens)
    return pairs >= LOGFMT_MIN_PAIRS and pairs / len(tokens) >= LOGFMT_MIN_PAIR_SHARE


# --- Delimited (CSV, TSV, semicolon, pipe) ------------------------------------------

CSV_DELIMITERS: Final = (",", "\t", ";", "|")
CSV_MIN_COLUMNS: Final = 3
_HEADER_FIELD: Final = re.compile(r"[A-Za-z_][\w .()/-]*\Z")


class CsvDialect(NamedTuple):
    """How a sample splits into columns.

    ``score`` is the share of non-blank sample records that have exactly ``columns``
    fields with ``delimiter``.
    """

    delimiter: str
    columns: int
    has_header: bool
    score: float


def _csv_row(text: str, delimiter: str) -> list[str] | None:
    """The fields of a record, or None unless it is exactly one delimited row."""
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return None
    return rows[0] if len(rows) == 1 else None


def _has_header(rows: list[list[str]]) -> bool:
    """True if the first row names the columns: identifier-like, unique, and at least one
    column holds digits in most data rows but not in the header."""
    header, data = rows[0], rows[1:]
    fields = [value.strip() for value in header]
    if not data or len(set(fields)) != len(fields):
        return False
    if not all(_HEADER_FIELD.match(value) for value in fields):
        return False
    for column in range(len(header)):
        with_digits = sum(any(ch.isdigit() for ch in row[column]) for row in data)
        if with_digits * 2 >= len(data):
            return True
    return False


def _dialect_for(records: list[str], delimiter: str) -> CsvDialect | None:
    rows = [_csv_row(text, delimiter) for text in records]
    counts = Counter(len(row) for row in rows if row is not None)
    if not counts:
        return None
    columns, matching = counts.most_common(1)[0]
    if columns < CSV_MIN_COLUMNS:
        return None
    consistent = [row for row in rows if row is not None and len(row) == columns]
    has_header = rows[0] is not None and len(rows[0]) == columns and _has_header(consistent)
    return CsvDialect(delimiter, columns, has_header, matching / len(records))


def sniff_csv(sample: Sequence[str]) -> CsvDialect | None:
    """Find the delimiter that splits most records into the same number of columns.

    Candidates are ``CSV_DELIMITERS``; the most common column count must be at least
    ``CSV_MIN_COLUMNS`` (two delimiters per row), and at least two non-blank records are
    required, so prose with a comma here and there does not qualify. Earlier delimiters
    win ties. Returns None when no delimiter qualifies.
    """
    records = non_blank(sample)
    if len(records) < 2:
        return None
    best: CsvDialect | None = None
    for delimiter in CSV_DELIMITERS:
        dialect = _dialect_for(records, delimiter)
        if dialect is not None and (best is None or dialect.score > best.score):
            best = dialect
    return best


def _csv_score(sample: Sequence[str]) -> float:
    dialect = sniff_csv(sample)
    return 0.0 if dialect is None else dialect.score


JSON_DETECTOR: Final = share_detector("json", is_json_object)
XML_DETECTOR: Final = share_detector("xml", is_xml_element)
LOGFMT_DETECTOR: Final = share_detector("logfmt", is_logfmt)
CSV_DETECTOR: Final = Detector("csv", _csv_score)
