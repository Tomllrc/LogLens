"""Helpers for analysis tests: build small LogLens stores from log text."""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loglens.pipeline import ParseOptions, run_parse

T0 = datetime(2026, 3, 1, tzinfo=UTC)


def line(at: timedelta | None, level: str, message: str) -> str:
    """A free-text log line ``<ISO time> <LEVEL> <message>`` (no time when ``at`` is None)."""
    if at is None:
        return f"{level} {message}" if level else message
    stamp = (T0 + at).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return f"{stamp} {level} {message}"


def build_store(root: Path, files: Mapping[str, Iterable[str]], name: str = "store") -> Path:
    """Write ``files`` (name -> lines) under ``root/logs-<name>`` and parse them into a store."""
    logs = root / f"logs-{name}"
    logs.mkdir(parents=True, exist_ok=True)
    for filename, lines in files.items():
        (logs / filename).write_text("".join(f"{text}\n" for text in lines), encoding="utf-8")
    output = root / name
    run_parse([logs], output, ParseOptions(workers=1))
    return output
