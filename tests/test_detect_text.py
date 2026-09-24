"""Tests for the known text-format detectors and the free-text fallback."""

import re

import pytest

from loglens.detect.text import (
    APACHE_COMBINED,
    APACHE_COMMON,
    FREE_TEXT_DETECTOR,
    FREE_TEXT_SCORE,
    LOG4J,
    NGINX_ERROR,
    PYTHON_LOGGING,
    SYSLOG_RFC3164,
    SYSLOG_RFC5424,
)

COMBINED = (
    '203.0.113.7 - bob [01/Jan/2026:10:00:00 +0000] "GET /index.html HTTP/1.1" 200 2326 '
    '"https://example.com/" "Mozilla/5.0 (X11; Linux x86_64)"'
)
COMMON = '203.0.113.7 - - [01/Jan/2026:10:00:00 +0000] "GET / HTTP/1.1" 404 -'

POSITIVES: list[tuple[re.Pattern[str], str]] = [
    (
        SYSLOG_RFC5424,
        "<165>1 2003-10-11T22:14:15.003Z mymachine.example.com evntslog - ID47 "
        '[exampleSDID@32473 iut="3" eventSource="Application"] An application event',
    ),
    (SYSLOG_RFC5424, "<34>1 2026-01-01T10:00:00+02:00 host su 42 - - 'su root' failed"),
    (SYSLOG_RFC5424, "<13>1 - - - - - -"),
    (SYSLOG_RFC3164, "Jan  1 10:00:00 host01 sshd[42]: Accepted publickey for bob"),
    (SYSLOG_RFC3164, "<34>Oct 11 22:14:15 mymachine su: 'su root' failed for lonvick"),
    (SYSLOG_RFC3164, "Dec 31 23:59:59 host kernel: [ 0.000000] Linux version"),
    (APACHE_COMBINED, COMBINED),
    (APACHE_COMBINED, '::1 - - [01/Jan/2026:10:00:00 +0000] "-" 400 0 "-" "-"'),
    (APACHE_COMMON, COMMON),
    (APACHE_COMMON, '10.0.0.1 ident frank [10/Oct/2000:13:55:36 -0700] "GET /a.gif" 200 2326'),
    (NGINX_ERROR, "2026/01/01 10:00:00 [error] 12#0: *1 connect() failed"),
    (NGINX_ERROR, "2026/01/01 10:00:00 [emerg] 1#1: bind() failed"),
    (LOG4J, "2026-01-01 10:00:00,123 INFO  [main] com.acme.App - Starting"),
    (LOG4J, "2026-01-01T10:00:00.123 ERROR [pool-1-thread-2] c.a.Svc - failed\n\tat x"),
    (LOG4J, "2026-01-01 10:00:00,123 [main] WARN  com.acme.App - thread first"),
    (LOG4J, "10:00:00.123 [main] DEBUG com.acme.App - logback default"),
    (PYTHON_LOGGING, "2026-01-01 10:00:00,123 - app.worker - INFO - started"),
    (PYTHON_LOGGING, "2026-01-01 10:00:00,123 - root - ERROR - failed\nTraceback (most recent"),
    (PYTHON_LOGGING, "WARNING:root:disk nearly full"),
    (PYTHON_LOGGING, "INFO:app.db:connected"),
]

NEGATIVES: list[tuple[re.Pattern[str], str]] = [
    (SYSLOG_RFC5424, "<34>Oct 11 22:14:15 mymachine su: failed"),
    (SYSLOG_RFC5424, "<34>1 yesterday host app - - - msg"),
    (SYSLOG_RFC3164, "2026-01-01 10:00:00 INFO started"),
    (SYSLOG_RFC3164, "Jan 1st was a good day"),
    (SYSLOG_RFC3164, "Foo  1 10:00:00 host x"),
    (APACHE_COMBINED, COMMON),
    (APACHE_COMBINED, "GET /index.html 200"),
    (APACHE_COMMON, COMBINED),
    (NGINX_ERROR, "2026-01-01 10:00:00 [error] 12#0: x"),
    (NGINX_ERROR, "2026/01/01 10:00:00 [error] something"),
    (LOG4J, "2026-01-01 10:00:00 INFO [main] com.acme.App - no millis"),
    (LOG4J, "2026-01-01 10:00:00,123 - app - INFO - python style"),
    (LOG4J, "2026-01-01 10:00:00,123 NOTALEVEL [main] x - y"),
    (PYTHON_LOGGING, "2026-01-01 10:00:00,123 INFO [main] com.acme.App - log4j style"),
    (PYTHON_LOGGING, "INFO: something happened"),
    (PYTHON_LOGGING, "2026-01-01 10:00:00 - app - INFO - no millis"),
    (PYTHON_LOGGING, "info:root:lowercase"),
]


@pytest.mark.parametrize(("pattern", "text"), POSITIVES, ids=[f"pos{i}" for i in range(20)])
def test_pattern_matches(pattern: re.Pattern[str], text: str) -> None:
    assert pattern.match(text)


@pytest.mark.parametrize(("pattern", "text"), NEGATIVES, ids=[f"neg{i}" for i in range(17)])
def test_pattern_rejects(pattern: re.Pattern[str], text: str) -> None:
    assert not pattern.match(text)


def test_parametrize_ids_cover_every_case() -> None:
    assert (len(POSITIVES), len(NEGATIVES)) == (20, 17)


def test_free_text_always_applies_with_a_low_score() -> None:
    assert FREE_TEXT_DETECTOR.score(["anything at all"]) == FREE_TEXT_SCORE
    assert FREE_TEXT_DETECTOR.score(["", " "]) == 0.0
    assert FREE_TEXT_SCORE < 0.5
