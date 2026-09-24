"""Parsers for self-describing structured formats: JSON, text prefix + JSON, CSV, logfmt, XML.

Every parser maps fields automatically (see :mod:`loglens.parsers.fields`) and never
raises: unparseable records become low-confidence events keeping the raw text.
"""

import csv
import io
import json
import re
from dataclasses import replace
from typing import ClassVar, Final
from xml.etree import ElementTree

from loglens.detect.structured import split_prefix_json
from loglens.parsers.base import (
    event_from_mapping,
    malformed_event,
    structural_event,
)
from loglens.parsers.fields import flatten, map_by_value, map_fields
from loglens.parsers.models import ParsedEvent
from loglens.parsers.values import split_text_prefix
from loglens.split.models import Record

PARTIAL_CONFIDENCE: Final = 0.5
"""Confidence of an event parsed with something left over or missing."""

HEADERLESS_CSV_CONFIDENCE: Final = 0.8
"""Confidence of a CSV row whose columns were assigned by what their values look like."""

# --- JSON -------------------------------------------------------------------------

_JSON_DECODER: Final = json.JSONDecoder()


class JsonParser:
    """One JSON object per record (JSON lines, or pretty-printed objects).

    Text after the object (a stray line the splitter attached to it) is kept in
    ``extra["trailing_text"]`` and lowers the confidence.
    """

    format_name: ClassVar[str] = "json"

    def parse(self, record: Record) -> ParsedEvent:
        """Parse one JSON object and map its (flattened) fields."""
        text = record.text.strip()
        try:
            data, end = _JSON_DECODER.raw_decode(text)
        except (ValueError, RecursionError) as error:
            return malformed_event(record.text, f"invalid JSON: {error}")
        if not isinstance(data, dict):
            return malformed_event(record.text, "JSON value is not an object")
        try:
            event = event_from_mapping(map_fields(flatten(data)))
        except RecursionError:
            return malformed_event(record.text, "JSON object nested too deeply")
        trailing = text[end:].strip()
        if not trailing:
            return event
        return replace(
            event,
            extra={**event.extra, "trailing_text": trailing},
            confidence=PARTIAL_CONFIDENCE,
            error="text after the JSON object",
        )


# --- Text prefix + JSON -----------------------------------------------------------


class PrefixJsonParser:
    """A text prefix followed by a JSON object: ``2026-01-01T10:00:00Z INFO app {...}``.

    The payload's own fields win. The prefix fills what the payload lacks: a leading
    timestamp, a level token, and a single remaining word as the component. Prefix
    values that lose to the payload, or a multi-word rest, are kept in ``extra``.
    """

    format_name: ClassVar[str] = "prefix_json"

    def parse(self, record: Record) -> ParsedEvent:
        """Split off the prefix, parse the JSON payload, and merge the two."""
        split = split_prefix_json(record.text)
        if split is None:
            return malformed_event(record.text, "no text prefix followed by a JSON object")
        prefix, payload = split
        try:
            mapped = map_fields(flatten(payload))
        except RecursionError:
            return malformed_event(record.text, "JSON object nested too deeply")
        event = event_from_mapping(mapped)

        timestamp, level, rest = split_text_prefix(prefix)
        extra = dict(event.extra)
        if timestamp is not None and event.timestamp_raw is not None:
            extra["prefix.timestamp"] = timestamp
        if level is not None and event.level_raw is not None:
            extra["prefix.level"] = level
        component = event.component
        if rest and component is None and " " not in rest:
            component = rest
        elif rest:
            extra["prefix"] = rest
        return replace(
            event,
            timestamp_raw=event.timestamp_raw or timestamp,
            level_raw=event.level_raw or level,
            component=component,
            extra=extra,
        )


# --- CSV / TSV --------------------------------------------------------------------

DELIMITERS_BY_NAME: Final = {"comma": ",", "tab": "\t", "semicolon": ";", "pipe": "|"}


class CsvParser:
    """Delimited rows (CSV, TSV, semicolon, pipe). Stateful: use one parser per file.

    With a header, the first well-formed record names the columns and is returned as a
    non-event; fields are then mapped by column name. Without one, columns are named
    ``col1``, ``col2``... and roles are guessed from the values. A row whose width
    differs from the header's (or the first row's) gets a lower confidence; surplus
    values are named after their position.
    """

    format_name: ClassVar[str] = "csv"

    def __init__(self, delimiter: str = ",", *, has_header: bool = False) -> None:
        self.delimiter = delimiter
        self.has_header = has_header
        self._columns: list[str] | None = None
        self._width: int | None = None

    @classmethod
    def from_details(cls, details: dict[str, str]) -> "CsvParser":
        """Build from detection details (``delimiter=tab``, ``header=yes``)."""
        name = details.get("delimiter", "comma")
        delimiter = DELIMITERS_BY_NAME.get(name, name if len(name) == 1 else ",")
        return cls(delimiter, has_header=details.get("header") == "yes")

    def parse(self, record: Record) -> ParsedEvent:
        """Parse one delimited row."""
        row = self._read_row(record.text)
        if row is None:
            return malformed_event(record.text, "not a single well-formed delimited row")
        if self.has_header and self._columns is None:
            self._columns = _unique_names(row)
            self._width = len(row)
            return structural_event(record.text)
        if self._width is None:
            self._width = len(row)

        names = self._columns or []
        fields = {
            names[i] if i < len(names) else f"col{i + 1}": value for i, value in enumerate(row)
        }
        width_ok = len(row) == self._width
        if self._columns is not None:
            mapped = map_fields(fields)
            confidence = 1.0 if width_ok else PARTIAL_CONFIDENCE
        else:
            mapped = map_by_value(fields)
            confidence = HEADERLESS_CSV_CONFIDENCE if width_ok else PARTIAL_CONFIDENCE
        error = None if width_ok else f"expected {self._width} columns, got {len(row)}"
        return event_from_mapping(mapped, confidence=confidence, error=error)

    def _read_row(self, text: str) -> list[str] | None:
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=self.delimiter, strict=True))
        except csv.Error:
            return None
        return rows[0] if len(rows) == 1 and rows[0] else None


def _unique_names(header: list[str]) -> list[str]:
    """Column names from a header row: stripped, blanks named by position, duplicates numbered."""
    names: list[str] = []
    for position, raw in enumerate(header, start=1):
        base = raw.strip() or f"col{position}"
        name, count = base, 1
        while name in names:
            count += 1
            name = f"{base}_{count}"
        names.append(name)
    return names


# --- logfmt -----------------------------------------------------------------------

_QUOTED: Final = r'"(?:[^"\\]|\\.)*"'
_LOGFMT_TOKEN: Final = re.compile(
    rf"(?P<key>[A-Za-z_@][\w.\-/@]*)=(?P<value>{_QUOTED}|[^\s\"]*)(?=\s|\Z)|(?P<word>{_QUOTED}|\S+)"
)


class LogfmtParser:
    """``key=value`` pairs, values optionally double-quoted with backslash escapes.

    Confidence is the share of tokens that are pairs. Loose words (tokens that are not
    pairs) become the message when there is no message field, else ``extra["text"]``.
    A repeated key is kept as ``key[2]``, ``key[3]``...
    """

    format_name: ClassVar[str] = "logfmt"

    def parse(self, record: Record) -> ParsedEvent:
        """Tokenize and map one logfmt record."""
        pairs: dict[str, str] = {}
        words: list[str] = []
        for match in _LOGFMT_TOKEN.finditer(record.text):
            if match.group("key") is not None:
                _put_unique(pairs, match.group("key"), _unquote(match.group("value")))
            else:
                words.append(match.group("word"))
        if not pairs:
            return malformed_event(record.text, "no key=value pairs")

        mapped = map_fields(pairs)
        confidence = len(pairs) / (len(pairs) + len(words))
        free_text = " ".join(words)
        if free_text and mapped.message is None:
            mapped = replace(mapped, message=free_text)
        elif free_text:
            mapped = replace(mapped, extra={**mapped.extra, "text": free_text})
        return event_from_mapping(mapped, confidence=confidence)


def _unquote(value: str) -> str:
    """A logfmt value without its quotes, with ``\\"``, ``\\\\``, ``\\n``... unescaped."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except ValueError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value[1:-1]
    return value


def _put_unique(fields: dict[str, str], key: str, value: str) -> None:
    name, count = key, 1
    while name in fields:
        count += 1
        name = f"{key}[{count}]"
    fields[name] = value


# --- XML --------------------------------------------------------------------------

_XML_STRUCTURAL: Final = re.compile(r"(?:\s|<\?xml\b[^>]*\?>|</[^\s<>/]+\s*>)+\Z")
_XML_OPENING_TAG_ONLY: Final = re.compile(r"<[A-Za-z_][^<>]*(?<!/)>\s*\Z")
_XML_TRAILING_CLOSER: Final = re.compile(r"\s*</[^\s<>/]+\s*>\s*\Z")
_MAX_STRAY_CLOSERS: Final = 3
_XML_FORBIDDEN: Final = ("<!DOCTYPE", "<!ENTITY")


class XmlParser:
    """One XML element per record, e.g. a Windows event or a log4j XMLLayout event.

    Attributes and child elements become dotted fields (``System.EventID``,
    ``System.TimeCreated.SystemTime``); namespaces are dropped. A leaf element with a
    ``Name`` attribute and text, like ``<Data Name="User">bob</Data>``, becomes
    ``EventData.User`` (``<Data>`` also when empty); ``<Provider Name="x"/>`` stays
    ``System.Provider.Name``. The root element's own text is the ``text`` field.

    A declaration, a lone wrapper opening tag (``<Events>``) or closing tags are
    returned as non-events. Wrapper closing tags the splitter attached after an element
    are ignored. Documents with a DOCTYPE or entity declarations are never parsed.
    """

    format_name: ClassVar[str] = "xml"

    def parse(self, record: Record) -> ParsedEvent:
        """Parse one XML element and map its flattened fields."""
        text = record.text.strip()
        if not text or _XML_STRUCTURAL.fullmatch(text):
            return structural_event(record.text)
        if any(marker in text.upper() for marker in _XML_FORBIDDEN):
            return malformed_event(record.text, "DOCTYPE and entity declarations are not parsed")
        root = _parse_element_ignoring_stray_closers(text)
        if root is None:
            if _XML_OPENING_TAG_ONLY.fullmatch(text):
                return structural_event(record.text)
            return malformed_event(record.text, "invalid XML")
        event = event_from_mapping(map_fields(flatten_element(root)))
        return replace(event, extra={**event.extra, "element": _local_name(root.tag)})


def _parse_element_ignoring_stray_closers(text: str) -> ElementTree.Element | None:
    """Parse an element; if that fails, drop trailing closing tags one at a time and retry.

    Handles ``<Event>...</Event>\\n</Events>``, where the splitter attached the wrapper's
    closing tag to the last element.
    """
    candidate = text
    for _ in range(_MAX_STRAY_CLOSERS + 1):
        root = _parse_element(candidate)
        if root is not None:
            return root
        match = _XML_TRAILING_CLOSER.search(candidate)
        if match is None:
            return None
        candidate = candidate[: match.start()]
    return None


def _parse_element(text: str) -> ElementTree.Element | None:
    try:
        return ElementTree.fromstring(text)
    except (ElementTree.ParseError, ValueError, RecursionError):
        return None


def _local_name(name: str) -> str:
    """A tag or attribute name without its ``{namespace}``."""
    return name.rsplit("}", 1)[-1]


def flatten_element(root: ElementTree.Element) -> dict[str, str]:
    """Flatten an element's attributes and descendants into dotted string fields."""
    fields: dict[str, str] = {}
    for name, value in root.attrib.items():
        _put_unique(fields, _local_name(name), value)
    if root.text and root.text.strip():
        _put_unique(fields, "text", root.text.strip())
    for child in root:
        _flatten_child(child, "", fields)
    return fields


def _flatten_child(element: ElementTree.Element, parent: str, fields: dict[str, str]) -> None:
    tag = _local_name(element.tag)
    path = f"{parent}.{tag}" if parent else tag
    text = (element.text or "").strip()
    name = element.attrib.get("Name") or element.attrib.get("name")
    named_value = len(element) == 0 and (bool(text) or tag == "Data")
    if name and named_value:  # <Data Name="User">bob</Data>, but not <Provider Name="x"/>
        _put_unique(fields, f"{parent}.{name}" if parent else name, text)
        return
    for attribute, value in element.attrib.items():
        _put_unique(fields, f"{path}.{_local_name(attribute)}", value)
    if text:
        _put_unique(fields, path, text)
    for child in element:
        _flatten_child(child, path, fields)
