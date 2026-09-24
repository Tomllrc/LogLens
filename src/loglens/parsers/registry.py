"""Choose a parser for a detected format."""

from collections.abc import Callable, Mapping
from typing import Final

from loglens.parsers.base import Parser
from loglens.parsers.structured import (
    CsvParser,
    JsonParser,
    LogfmtParser,
    PrefixJsonParser,
    XmlParser,
)
from loglens.parsers.textformats import TEXT_FORMATS, TextFormatParser

ParserFactory = Callable[[dict[str, str]], Parser]
"""Builds a fresh parser from the detection's ``details``."""

STRUCTURED_PARSERS: Final[Mapping[str, ParserFactory]] = {
    "json": lambda _details: JsonParser(),
    "prefix_json": lambda _details: PrefixJsonParser(),
    "csv": CsvParser.from_details,
    "logfmt": lambda _details: LogfmtParser(),
    "xml": lambda _details: XmlParser(),
}
"""Parsers for the structured formats named by :mod:`loglens.detect`."""


def create_parser(format_name: str, details: Mapping[str, str] | None = None) -> Parser | None:
    """A new parser for a detected format, or None if no parser handles it.

    Structured formats come first, then the text formats declared in YAML. Create one
    parser per file: some parsers keep state (a CSV header).
    """
    factory = STRUCTURED_PARSERS.get(format_name)
    if factory is not None:
        return factory(dict(details or {}))
    text_format = TEXT_FORMATS.get(format_name)
    return None if text_format is None else TextFormatParser(text_format)
