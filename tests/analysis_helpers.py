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


_M = timedelta(minutes=1)


def two_hour_lines(first_hour: bool, second_hour: bool) -> list[str]:
    """Two hours of an app log: steady INFO and refused errors; payment timeouts grow
    from 5 to 60 in the second hour, cache warm-up warnings stop, disk-quota errors start.
    """
    lines = []
    for minute in range(120):
        if (minute < 60 and not first_hour) or (minute >= 60 and not second_hour):
            continue
        lines.append(line(minute * _M, "INFO", f"GET /api/items/{minute} served in 5ms"))
        lines.append(line(minute * _M, "ERROR", f"Connection to 10.0.0.{minute} refused by peer"))
        if minute < 60 and minute % 12 == 0:
            lines.append(line(minute * _M, "ERROR", f"Payment gateway timeout after {minute} ms"))
        if minute >= 60:
            lines.append(line(minute * _M, "ERROR", f"Payment gateway timeout after {minute} ms"))
        if minute < 60 and minute % 10 == 0:
            lines.append(line(minute * _M, "WARN", f"Cache warm-up failed for shard {minute}"))
        if minute >= 60 and minute % 5 == 0:
            lines.append(
                line(minute * _M, "ERROR", f"Disk quota exceeded on volume /data/{minute}")
            )
    return lines
