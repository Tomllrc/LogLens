"""Detectors for well-known line-oriented text formats, plus the free-text fallback.

Each known format is a regex anchored at the start of the record, so multi-line
records (a log4j entry followed by its stack trace) are recognized by their first line.
"""

import re
from collections.abc import Sequence
from typing import Final

from loglens.detect.base import Detector, non_blank, regex_detector

_MONTH: Final = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_ISO_DATE_TIME: Final = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
_JAVA_LEVEL: Final = r"(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE)"
_PY_LEVEL: Final = r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)"

SYSLOG_RFC5424: Final = re.compile(
    r"<\d{1,3}>[1-9]\d{0,2}"  # <PRI>VERSION
    r" (?:-|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2}))"  # TIMESTAMP
    r" \S{1,255} \S{1,48} \S{1,128} \S{1,32}"  # HOSTNAME APP-NAME PROCID MSGID
    r" (?:-|\[)"  # STRUCTURED-DATA
)
"""RFC 5424: ``<165>1 2026-01-01T10:00:00.003Z host app 1234 ID47 [sd@1 a="b"] msg``."""

SYSLOG_RFC3164: Final = re.compile(
    rf"(?:<\d{{1,3}}>)?{_MONTH} [ \d]\d \d{{2}}:\d{{2}}:\d{{2}} \S+ \S"
)
"""BSD syslog (RFC 3164): ``Jan  1 10:00:00 host sshd[42]: msg``, optional ``<PRI>``."""

_CLF_PREFIX: Final = r"\S+ \S+ \S+ \[[^\]]+\] \"[^\"]*\" (?:\d{3}|-) (?:\d+|-)"

APACHE_COMBINED: Final = re.compile(rf"{_CLF_PREFIX} \"[^\"]*\" \"[^\"]*\"")
"""Apache/nginx combined log format (nginx's default ``access_log`` format)."""

APACHE_COMMON: Final = re.compile(rf"{_CLF_PREFIX}\s*\Z")
"""Apache Common Log Format (CLF): the combined format without referer and user agent."""

NGINX_ERROR: Final = re.compile(
    r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \[(?:debug|info|notice|warn|error|crit|alert|emerg)\]"
    r" \d+#\d+: "
)
"""nginx error log: ``2026/01/01 10:00:00 [error] 12#0: *1 message``."""

LOG4J: Final = re.compile(
    rf"(?:{_ISO_DATE_TIME}|\d{{2}}:\d{{2}}:\d{{2}})[,.]\d{{3}}"
    rf"(?: +{_JAVA_LEVEL} +\[[^\]]*\]| +\[[^\]]*\] +{_JAVA_LEVEL})"
    r" +\S+ +- "
)
"""log4j / logback: ``2026-01-01 10:00:00,123 INFO [main] com.acme.App - msg``.

Also ``%d [%t] %-5p %c - %m`` (thread before level) and logback's time-only default.
"""

PYTHON_LOGGING: Final = re.compile(
    rf"(?:{_ISO_DATE_TIME},\d{{3}} - \S.*? - {_PY_LEVEL} - "
    rf"|{_PY_LEVEL}:[\w.\-]*:)"
)
"""Python ``logging``: ``%(asctime)s - %(name)s - %(levelname)s - %(message)s`` and the
``basicConfig`` default ``%(levelname)s:%(name)s:%(message)s``."""

FREE_TEXT_SCORE: Final = 0.1
"""Fixed score of the ``free_text`` fallback: it always applies, but never convincingly."""


def _free_text_score(sample: Sequence[str]) -> float:
    return FREE_TEXT_SCORE if non_blank(sample) else 0.0


SYSLOG_RFC5424_DETECTOR: Final = regex_detector("syslog_rfc5424", SYSLOG_RFC5424)
SYSLOG_RFC3164_DETECTOR: Final = regex_detector("syslog_rfc3164", SYSLOG_RFC3164)
APACHE_COMBINED_DETECTOR: Final = regex_detector("apache_combined", APACHE_COMBINED)
APACHE_COMMON_DETECTOR: Final = regex_detector("apache_common", APACHE_COMMON)
NGINX_ERROR_DETECTOR: Final = regex_detector("nginx_error", NGINX_ERROR)
LOG4J_DETECTOR: Final = regex_detector("log4j", LOG4J)
PYTHON_LOGGING_DETECTOR: Final = regex_detector("python_logging", PYTHON_LOGGING)
FREE_TEXT_DETECTOR: Final = Detector("free_text", _free_text_score)
