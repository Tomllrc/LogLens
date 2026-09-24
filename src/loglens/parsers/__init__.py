"""Parsers: known-format, generic (Drain template mining), and AI-taught YAML plugins.

Implemented so far: structured formats (JSON, text prefix + JSON, CSV/TSV, logfmt,
XML) with automatic field mapping, and well-known text formats declared in YAML
(``patterns/``: syslog, Apache/Nginx, log4j, Python logging, Go log, systemd journal,
Windows event exports).
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
from loglens.parsers.textformats import (
    TEXT_FORMATS,
    FormatDefinitionError,
    TextFormat,
    TextFormatParser,
    load_text_formats,
)

__all__ = [
    "MALFORMED_CONFIDENCE",
    "STRUCTURED_PARSERS",
    "TEXT_FORMATS",
    "CsvParser",
    "FormatDefinitionError",
    "JsonParser",
    "LogfmtParser",
    "MappedFields",
    "ParsedEvent",
    "Parser",
    "PrefixJsonParser",
    "TextFormat",
    "TextFormatParser",
    "XmlParser",
    "create_parser",
    "flatten",
    "load_text_formats",
    "malformed_event",
    "map_by_value",
    "map_fields",
    "safe_parse",
]
