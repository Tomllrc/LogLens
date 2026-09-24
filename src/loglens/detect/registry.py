"""The default candidate formats, in priority order.

When two candidates reach the same score, the one listed first wins, so the order runs
from the most specific format to the least specific:

1. ``json`` and ``xml``: checked by a real parser, nearly impossible to match by accident.
2. ``syslog_rfc5424``, ``syslog_rfc3164``, ``apache_combined``, ``apache_common``,
   ``nginx_error``, ``log4j``, ``python_logging``: anchored regexes pinning down several
   fixed fields.
3. ``prefix_json``: any text prefix followed by a JSON object. It comes after the known
   text formats, so a log4j line whose message happens to be JSON stays ``log4j``.
4. ``logfmt``: any text made of ``key=value`` pairs.
5. ``csv``: anything with a consistent number of delimiters (JSON lines with the same
   keys, for instance, also score high as CSV).
6. ``free_text``: always applies, with a low fixed score.

Later steps (YAML parser plugins) extend detection by passing their own tuple of
detectors in :class:`loglens.detect.DetectOptions`.
"""

from typing import Final

from loglens.detect.base import Detector
from loglens.detect.structured import (
    CSV_DETECTOR,
    JSON_DETECTOR,
    LOGFMT_DETECTOR,
    PREFIX_JSON_DETECTOR,
    XML_DETECTOR,
)
from loglens.detect.text import (
    APACHE_COMBINED_DETECTOR,
    APACHE_COMMON_DETECTOR,
    FREE_TEXT_DETECTOR,
    LOG4J_DETECTOR,
    NGINX_ERROR_DETECTOR,
    PYTHON_LOGGING_DETECTOR,
    SYSLOG_RFC3164_DETECTOR,
    SYSLOG_RFC5424_DETECTOR,
)

FREE_TEXT: Final = "free_text"
"""Name of the fallback format, chosen when no candidate is convincing."""

DEFAULT_MIN_CONFIDENCE: Final = 0.6
"""Lowest winning score; below it a file is ``free_text``."""

DEFAULT_SAMPLE_RECORDS: Final = 500
"""Records per file scored to detect its format."""

DEFAULT_DETECTORS: Final[tuple[Detector, ...]] = (
    JSON_DETECTOR,
    XML_DETECTOR,
    SYSLOG_RFC5424_DETECTOR,
    SYSLOG_RFC3164_DETECTOR,
    APACHE_COMBINED_DETECTOR,
    APACHE_COMMON_DETECTOR,
    NGINX_ERROR_DETECTOR,
    LOG4J_DETECTOR,
    PYTHON_LOGGING_DETECTOR,
    PREFIX_JSON_DETECTOR,
    LOGFMT_DETECTOR,
    CSV_DETECTOR,
    FREE_TEXT_DETECTOR,
)
"""Built-in candidates, most specific first (earlier wins exact ties)."""
