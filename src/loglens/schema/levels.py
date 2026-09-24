"""The normalized log levels of the unified event schema."""

from typing import Final

LEVELS: Final = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL", "UNKNOWN")
"""Every normalized level, from least to most severe, then ``UNKNOWN``."""
