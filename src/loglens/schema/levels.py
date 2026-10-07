"""The normalized log levels of the unified event schema."""

from typing import Final

LEVELS: Final = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL", "UNKNOWN")
"""Every normalized level, from least to most severe, then ``UNKNOWN``."""

UNKNOWN: Final = "UNKNOWN"
ERROR_LEVELS: Final = frozenset({"ERROR", "FATAL"})

_WORDS: Final = {
    "trace": "TRACE",
    "finest": "TRACE",
    "debug": "DEBUG",
    "verbose": "DEBUG",
    "fine": "DEBUG",
    "finer": "DEBUG",
    "info": "INFO",
    "information": "INFO",
    "informational": "INFO",
    "notice": "INFO",
    "config": "INFO",
    "warn": "WARN",
    "warning": "WARN",
    "error": "ERROR",
    "err": "ERROR",
    "severe": "ERROR",
    "fatal": "FATAL",
    "critical": "FATAL",
    "crit": "FATAL",
    "alert": "FATAL",
    "emerg": "FATAL",
    "emergency": "FATAL",
    "panic": "FATAL",
}
_SYSLOG_SEVERITIES: Final = ("FATAL", "FATAL", "FATAL", "ERROR", "WARN", "INFO", "INFO", "DEBUG")
_PINO_LEVELS: Final = {10: "TRACE", 20: "DEBUG", 30: "INFO", 40: "WARN", 50: "ERROR", 60: "FATAL"}


def normalize_level(raw: str | None) -> str:
    """Map a raw level to the schema's vocabulary, or ``UNKNOWN``.

    Words are matched case-insensitively (``Warning``, ``ERR``, ``notice``). Numbers are
    read as syslog severities when 0-7 and as pino/bunyan levels when 10-60.
    """
    if raw is None:
        return UNKNOWN
    text = raw.strip().strip("[]<>():").lower()
    if text in _WORDS:
        return _WORDS[text]
    if text.isdigit():
        number = int(text)
        if number < len(_SYSLOG_SEVERITIES):
            return _SYSLOG_SEVERITIES[number]
        return _PINO_LEVELS.get(number, UNKNOWN)
    return UNKNOWN
