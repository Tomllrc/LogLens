"""Detectors for well-known text formats, plus the free-text fallback.

The known formats are declared in YAML (``loglens/parsers/patterns/``, see
:mod:`loglens.parsers.textformats`), so a new format needs no Python code. Each one's
score is the share of sample records it matches; the regexes are anchored at the start
of the record, so multi-line records (a log4j entry followed by its stack trace) are
recognized by their first line.
"""

from collections.abc import Sequence
from typing import Final

from loglens.detect.base import Detector, non_blank
from loglens.parsers.textformats import TEXT_FORMATS, TextFormat


def text_format_detector(text_format: TextFormat) -> Detector:
    """A detector scoring the share of records that match a declared text format."""
    return Detector(text_format.name, text_format.score)


TEXT_FORMAT_DETECTORS: Final = tuple(text_format_detector(f) for f in TEXT_FORMATS.values())
"""One detector per built-in text format, most specific (lowest priority) first."""

# The formats themselves, by their historical names: each has ``.match(text)``.
SYSLOG_RFC5424: Final = TEXT_FORMATS["syslog_rfc5424"]
SYSLOG_RFC3164: Final = TEXT_FORMATS["syslog_rfc3164"]
APACHE_COMBINED: Final = TEXT_FORMATS["apache_combined"]
APACHE_COMMON: Final = TEXT_FORMATS["apache_common"]
NGINX_ERROR: Final = TEXT_FORMATS["nginx_error"]
LOG4J: Final = TEXT_FORMATS["log4j"]
PYTHON_LOGGING: Final = TEXT_FORMATS["python_logging"]

FREE_TEXT_SCORE: Final = 0.1
"""Fixed score of the ``free_text`` fallback: it always applies, but never convincingly."""


def _free_text_score(sample: Sequence[str]) -> float:
    return FREE_TEXT_SCORE if non_blank(sample) else 0.0


FREE_TEXT_DETECTOR: Final = Detector("free_text", _free_text_score)
