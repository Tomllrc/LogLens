"""The parser protocol and helpers shared by all parsers."""

import logging
from typing import Protocol

from loglens.parsers.fields import MappedFields, describe_fields
from loglens.parsers.models import MALFORMED_CONFIDENCE, ParsedEvent
from loglens.split.models import Record

logger = logging.getLogger(__name__)


class Parser(Protocol):
    """Turns one record of a known format into a :class:`ParsedEvent`.

    Implementations must never raise on bad input: a record they cannot parse becomes
    a low-confidence event whose message is the raw text (see :func:`malformed_event`).
    Some parsers are stateful (a CSV parser learns the header from the first record),
    so create one parser per file.
    """

    @property
    def format_name(self) -> str:
        """The detected format this parser handles."""
        ...

    def parse(self, record: Record) -> ParsedEvent:
        """Parse one record."""
        ...


def malformed_event(text: str, reason: str) -> ParsedEvent:
    """A low-confidence event for a record that could not be parsed: the raw text is kept."""
    return ParsedEvent(message=text.strip() or text, confidence=MALFORMED_CONFIDENCE, error=reason)


def structural_event(text: str) -> ParsedEvent:
    """A record that holds no event (a header row, an XML wrapper tag)."""
    return ParsedEvent(message=text.strip(), is_event=False)


def event_from_mapping(
    mapped: MappedFields, *, confidence: float = 1.0, error: str | None = None
) -> ParsedEvent:
    """Build an event from mapped fields; without a message field, describe the others."""
    message = mapped.message if mapped.message is not None else describe_fields(mapped.extra)
    return ParsedEvent(
        message=message,
        timestamp_raw=mapped.timestamp_raw,
        level_raw=mapped.level_raw,
        component=mapped.component,
        extra=mapped.extra,
        confidence=confidence,
        error=error,
    )


def safe_parse(parser: Parser, record: Record) -> ParsedEvent:
    """Call ``parser.parse``, turning any unexpected exception into a malformed event.

    Built-in parsers never raise; this guards against plugin parsers that do.
    """
    try:
        return parser.parse(record)
    except Exception as error:  # a parser bug must never crash the pipeline
        logger.warning(
            "%s parser failed on %s:%d: %r",
            parser.format_name,
            record.source_file,
            record.line_start,
            error,
        )
        return malformed_event(record.text, f"{parser.format_name} parser failed: {error!r}")
