"""Synthetic logs with planted issues and anomalies, for the analysis tests.

Run with: uv run python tests/fixtures/make_analysis_fixtures.py [OUTPUT_DIR]
(default ``data/analysis-fixtures``, gitignored). Tests import the functions and write
into a temporary folder instead, so no generated file is committed. Output is
deterministic (seeded randomness, no clock). All data is synthetic.

24 hours from 2026-03-01T00:00:00Z, two files per store:

* ``app``: 8-12 INFO events per minute from four templates, a cache WARN every 10
  minutes, a payment-gateway ERROR every hour, and two variants of one connection error
  ("refused by peer" / "refused by remote peer", 60 each, evenly spread): Drain keeps
  them apart, the issue grouping must merge them.
* ``worker``: 1,200 jobs, each "started" -> "step load done" -> "finished".

The *scenario* store adds the planted problems; the *background* twin has none:

* 200 payment-gateway timeouts between 06:30:00 and 06:34:59 (spike),
* app traffic down to a quarter from 12:00 (level shift),
* "Disk quota exceeded" every 2 minutes from 18:00 (new pattern),
* job 900 logs "step load skipped from cache" instead of "step load done"
  (rare transition).

Each store can be rendered as free text or as JSON lines, with identical content.
"""

import json
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 3, 1, tzinfo=UTC)
MINUTES = 24 * 60
BURST_AT = START + timedelta(hours=6, minutes=30)
DROP_AT = START + timedelta(hours=12)
NEW_AT = START + timedelta(hours=18)
DEVIANT_JOB = 900
JOBS = 1200

INFO_TEMPLATES = (
    "GET /api/items/{n} served in {n}ms",
    "User u{n} logged in from 10.1.0.{n}",
    "Cache hit for key k{n}",
    "Scheduled task t{n} completed",
)


@dataclass(frozen=True)
class Event:
    """One log event before rendering."""

    at: datetime
    level: str
    service: str
    message: str


def app_events(scenario: bool) -> list[Event]:
    """The ``app`` file of a store (scenario: with the planted problems)."""
    rng = random.Random(4)
    events: list[Event] = []
    for minute in range(MINUTES):
        base = START + timedelta(minutes=minute)
        count = rng.randint(8, 12)
        if scenario and base >= DROP_AT:
            count = max(1, count // 4)
        for i in range(count):
            text = rng.choice(INFO_TEMPLATES).format(n=rng.randint(1, 250))
            events.append(Event(base + timedelta(seconds=i * 60 / count), "INFO", "api", text))
        if minute % 10 == 0:
            region = rng.choice(["eu", "us", "ap"])
            events.append(
                Event(
                    base + timedelta(seconds=7),
                    "WARN",
                    "cache",
                    f"Cache miss rate high for region {region}",
                )
            )
        if minute % 60 == 0:
            events.append(_payment(base + timedelta(seconds=13), rng))
        if minute % 24 == 0:
            host = f"10.0.{rng.randint(0, 9)}.{rng.randint(1, 250)}"
            events.append(
                Event(
                    base + timedelta(seconds=17),
                    "ERROR",
                    "db",
                    f"Connection to {host} refused by peer",
                )
            )
        if minute % 24 == 12:
            host = f"10.0.{rng.randint(0, 9)}.{rng.randint(1, 250)}"
            events.append(
                Event(
                    base + timedelta(seconds=17),
                    "ERROR",
                    "db",
                    f"Connection to {host} refused by remote peer",
                )
            )
        if scenario and base >= NEW_AT and minute % 2 == 0:
            events.append(
                Event(
                    base + timedelta(seconds=23),
                    "ERROR",
                    "storage",
                    f"Disk quota exceeded on volume /data/v{rng.randint(1, 40)}",
                )
            )
    if scenario:
        for i in range(200):
            events.append(_payment(BURST_AT + timedelta(seconds=i * 1.5), rng))
    return sorted(events, key=lambda e: e.at)


def _payment(at: datetime, rng: random.Random) -> Event:
    return Event(
        at, "ERROR", "payments", f"Payment gateway timeout after {rng.randint(3000, 9000)} ms"
    )


def worker_events(scenario: bool) -> list[Event]:
    """The ``worker`` file: a strict three-step cycle per job."""
    events: list[Event] = []
    step = timedelta(seconds=MINUTES * 60 / JOBS)
    for job in range(JOBS):
        at = START + job * step
        load = (
            "step load skipped from cache" if scenario and job == DEVIANT_JOB else "step load done"
        )
        events += [
            Event(at, "INFO", "worker", f"Job {job} started on worker pool"),
            Event(at + timedelta(seconds=1), "INFO", "worker", f"Job {job} {load}"),
            Event(at + timedelta(seconds=2), "INFO", "worker", f"Job {job} finished"),
        ]
    return events


def render(events: list[Event], fmt: str) -> list[str]:
    """Lines of a file: ``text`` (``<time> <LEVEL> [service] message``) or ``json``."""
    lines = []
    for event in events:
        stamp = event.at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if fmt == "text":
            lines.append(f"{stamp} {event.level} [{event.service}] {event.message}")
        else:
            record = {
                "time": stamp,
                "level": event.level.lower(),
                "service": event.service,
                "message": event.message,
            }
            lines.append(json.dumps(record))
    return lines


def write_store_logs(out_dir: Path, scenario: bool, fmt: str) -> Path:
    """Write ``app`` and ``worker`` files into ``out_dir`` and return it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    extension = "log" if fmt == "text" else "jsonl"
    for name, events in (("app", app_events(scenario)), ("worker", worker_events(scenario))):
        text = "".join(f"{line}\n" for line in render(events, fmt))
        (out_dir / f"{name}.{extension}").write_text(text, encoding="utf-8", newline="\n")
    return out_dir


def main() -> None:
    """Write the four variants (scenario/background x text/json) under the output dir."""
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/analysis-fixtures")
    for scenario in (True, False):
        for fmt in ("text", "json"):
            name = f"{'scenario' if scenario else 'background'}-{fmt}"
            print(write_store_logs(root / name, scenario, fmt))


if __name__ == "__main__":
    main()
