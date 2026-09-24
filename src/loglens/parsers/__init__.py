"""Parsers: known-format, generic (Drain template mining), and AI-taught YAML plugins.

Implemented so far: structured formats (JSON, text prefix + JSON, CSV/TSV, logfmt,
XML) with automatic field mapping.
"""

from loglens.parsers.base import Parser, malformed_event, safe_parse
from loglens.parsers.fields import MappedFields, flatten, map_by_value, map_fields
from loglens.parsers.models import MALFORMED_CONFIDENCE, ParsedEvent
from loglens.parsers.registry import STRUCTURED_PARSERS, create_parser
from loglens.parsers.structured import (
    CsvParser,
    JsonParser,
    LogfmtParser,
    PrefixJsonParser,
    XmlParser,
)

__all__ = [
    "MALFORMED_CONFIDENCE",
    "STRUCTURED_PARSERS",
    "CsvParser",
    "JsonParser",
    "LogfmtParser",
    "MappedFields",
    "ParsedEvent",
    "Parser",
    "PrefixJsonParser",
    "XmlParser",
    "create_parser",
    "flatten",
    "malformed_event",
    "map_by_value",
    "map_fields",
    "safe_parse",
]
