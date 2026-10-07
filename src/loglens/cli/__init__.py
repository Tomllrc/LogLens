"""Command-line interface for LogLens: a thin layer over the core package.

Each command group lives in its own module; importing them registers the commands.
"""

from loglens.cli import ai_cmd, detect_cmd, formats_cmd, parse_cmd
from loglens.cli.app import app

__all__ = ["ai_cmd", "app", "detect_cmd", "formats_cmd", "parse_cmd"]
