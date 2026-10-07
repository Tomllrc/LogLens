"""The format registry: built-in text formats plus user plugins.

A text format (one YAML file) is at once a detector, a parser and, with ``record_start``,
a record-splitting rule. The registry loads the built-in formats, then user formats from
``~/.loglens/formats/`` (or ``$LOGLENS_HOME/formats``) and any ``--formats-dir``, later
sources overriding earlier ones with the same name. Invalid user files are reported in
``errors`` and skipped: one bad plugin never stops a run.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final

from loglens.detect.base import Detector
from loglens.detect.structured import (
    CSV_DETECTOR,
    JSON_DETECTOR,
    LOGFMT_DETECTOR,
    PREFIX_JSON_DETECTOR,
    XML_DETECTOR,
)
from loglens.detect.text import FREE_TEXT_DETECTOR, text_format_detector
from loglens.parsers.base import Parser
from loglens.parsers.registry import STRUCTURED_PARSERS
from loglens.parsers.textformats import (
    TEXT_FORMATS,
    FormatDefinitionError,
    TextFormat,
    TextFormatParser,
    load_text_format,
)
from loglens.split.signature import DeclaredStart

BUILT_IN: Final = "built-in"


@dataclass(frozen=True)
class FormatRegistry:
    """Text formats by name (most specific first), where each came from, and load errors."""

    text_formats: Mapping[str, TextFormat]
    origins: Mapping[str, str]
    errors: tuple[FormatDefinitionError, ...] = field(default=())

    def detectors(self) -> tuple[Detector, ...]:
        """Every candidate format, most specific first (same order as the defaults)."""
        return (
            JSON_DETECTOR,
            XML_DETECTOR,
            *(text_format_detector(fmt) for fmt in self.text_formats.values()),
            PREFIX_JSON_DETECTOR,
            LOGFMT_DETECTOR,
            CSV_DETECTOR,
            FREE_TEXT_DETECTOR,
        )

    def create_parser(
        self, format_name: str, details: Mapping[str, str] | None = None
    ) -> Parser | None:
        """A fresh parser for a detected format (structured first, then text formats)."""
        factory = STRUCTURED_PARSERS.get(format_name)
        if factory is not None:
            return factory(dict(details or {}))
        text_format = self.text_formats.get(format_name)
        return None if text_format is None else TextFormatParser(text_format)

    def declared_starts(self) -> tuple[DeclaredStart, ...]:
        """Record-start rules of multi-line formats, for the splitter."""
        return tuple(
            DeclaredStart(fmt.name, fmt.record_start)
            for fmt in self.text_formats.values()
            if fmt.record_start is not None
        )


def load_registry(directories: Sequence[Path] = ()) -> FormatRegistry:
    """Built-in formats overridden by the YAML files of ``directories``, in order."""
    formats: dict[str, TextFormat] = dict(TEXT_FORMATS)
    origins = dict.fromkeys(TEXT_FORMATS, BUILT_IN)
    errors: list[FormatDefinitionError] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        paths = sorted(p for p in directory.iterdir() if p.suffix in {".yaml", ".yml"})
        for path in paths:
            try:
                text_format = load_text_format(path)
            except FormatDefinitionError as error:
                errors.append(error)
                continue
            formats[text_format.name] = text_format
            origins[text_format.name] = path.as_posix()
    ordered = dict(sorted(formats.items(), key=lambda item: (item[1].priority, item[0])))
    return FormatRegistry(ordered, origins, tuple(errors))


@lru_cache(maxsize=8)
def cached_registry(directories: tuple[Path, ...]) -> FormatRegistry:
    """:func:`load_registry`, loaded once per process for a given list of folders."""
    return load_registry(directories)
