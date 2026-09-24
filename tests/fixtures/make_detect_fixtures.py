"""Regenerate the format-detection fixtures in tests/fixtures/detect/.

Run with: uv run python tests/fixtures/make_detect_fixtures.py
Output is deterministic (fixed data, no clock or randomness), so re-running produces
identical bytes. Each file holds a single format (see tests/test_detect_stream.py).
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "detect"
N = 24
LEVELS = ["INFO", "DEBUG", "WARN", "ERROR"]
USERS = ["alice", "bob", "carol", "dave"]
PATHS = ["/", "/login", "/api/items?id=7", "/static/app.js"]
AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) Firefox/128.0",
    "curl/8.5.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0",
]


def _ts(i: int, sep: str = "T") -> str:
    return f"2026-01-01{sep}10:{i // 60:02d}:{i % 60:02d}"


def json_lines() -> list[str]:
    return [
        json.dumps({"ts": _ts(i) + "Z", "level": LEVELS[i % 4].lower(), "msg": f"event {i}"})
        for i in range(N)
    ]


def pretty_json() -> list[str]:
    lines: list[str] = []
    for i in range(8):
        event = {"time": _ts(i) + "Z", "level": "info", "ctx": {"user": USERS[i % 4]}}
        lines.extend(json.dumps(event, indent=2).splitlines())
    return lines


def header_csv() -> list[str]:
    rows = ["timestamp,level,component,message"]
    for i in range(N):
        rows.append(f'{_ts(i)}Z,{LEVELS[i % 4]},auth,"user {USERS[i % 4]} logged in, id {i}"')
    return rows


def tsv() -> list[str]:
    return [f"{_ts(i, ' ')}\t{LEVELS[i % 4]}\tworker-{i % 3}\tjob {i} done" for i in range(N)]


def logfmt() -> list[str]:
    return [
        f"ts={_ts(i)}Z level={LEVELS[i % 4].lower()} user={USERS[i % 4]} "
        f'msg="request {i} served" duration_ms={i * 3}'
        for i in range(N)
    ]


def xml_events() -> list[str]:
    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    for i in range(8):
        lines += [
            '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">',
            "  <System>",
            f"    <EventID>{4624 + i % 2}</EventID>",
            f'    <TimeCreated SystemTime="{_ts(i)}.000Z"/>',
            "    <Computer>host01</Computer>",
            "  </System>",
            "  <EventData>",
            f'    <Data Name="TargetUserName">{USERS[i % 4]}</Data>',
            "  </EventData>",
            "</Event>",
        ]
    return lines


def syslog_rfc3164() -> list[str]:
    return [
        f"Jan  1 10:{i // 60:02d}:{i % 60:02d} host01 sshd[{1000 + i}]: "
        f"Accepted publickey for {USERS[i % 4]} from 10.0.0.{i} port 22"
        for i in range(N)
    ]


def syslog_rfc5424() -> list[str]:
    lines = []
    for i in range(N):
        sd = f'[origin@32473 ip="10.0.0.{i}"]' if i % 2 else "-"
        lines.append(f"<165>1 {_ts(i)}.003Z host01 app {100 + i} ID{i} {sd} event {i}")
    return lines


def _clf(i: int) -> str:
    return (
        f"10.0.0.{i} - {USERS[i % 4] if i % 3 else '-'} [01/Jan/2026:10:00:{i:02d} +0000] "
        f'"GET {PATHS[i % 4]} HTTP/1.1" {200 if i % 5 else 404} {512 + i}'
    )


def apache_combined() -> list[str]:
    return [f'{_clf(i)} "https://example.com/" "{AGENTS[i % 3]}"' for i in range(N)]


def apache_common() -> list[str]:
    return [_clf(i) for i in range(N)]


def nginx_access() -> list[str]:
    return [f'{_clf(i)} "-" "{AGENTS[(i + 1) % 3]}"' for i in range(N)]


def nginx_error() -> list[str]:
    return [
        f"2026/01/01 10:00:{i:02d} [{'error' if i % 2 else 'warn'}] 12#0: *{i} "
        f"connect() failed (111: Connection refused), client: 10.0.0.{i}, server: example.com"
        for i in range(N)
    ]


def python_logging() -> list[str]:
    lines = []
    for i in range(N):
        level = ["INFO", "DEBUG", "WARNING", "ERROR"][i % 4]
        lines.append(f"{_ts(i, ' ')},{i:03d} - app.worker - {level} - task {i} finished")
        if i % 8 == 3:
            lines += [
                "Traceback (most recent call last):",
                '  File "worker.py", line 10, in run',
                "    process(task)",
                "ValueError: bad task",
            ]
    return lines


def log4j() -> list[str]:
    lines = []
    for i in range(N):
        level = ["INFO", "DEBUG", "WARN", "ERROR"][i % 4]
        lines.append(f"{_ts(i, ' ')},{i:03d} {level:<5} [main] com.acme.App - step {i}")
        if i % 8 == 3:
            lines += [
                "java.lang.IllegalStateException: boom",
                "\tat com.acme.App.handle(App.java:42)",
                "Caused by: java.io.IOException: disk full",
                "\t... 3 more",
            ]
    return lines


def free_text() -> list[str]:
    return [
        "Starting the nightly backup job.",
        "Connected to the storage server, checking quota, then copying files.",
        "Copied 1204 files in 3 minutes",
        "Some files were skipped because retry=3 was reached",
        "<not really xml> just a note",
        "Warning: disk nearly full, please clean up soon",
        "Backup finished; everything looks fine",
        "Next run scheduled for tomorrow night",
        "User bob asked for a restore, so we restored it, checked it, and closed the ticket.",
        "Done.",
    ]


GENERATORS = {
    "json_lines.jsonl": json_lines,
    "pretty.json": pretty_json,
    "header.csv": header_csv,
    "data.tsv": tsv,
    "logfmt.log": logfmt,
    "events.xml": xml_events,
    "syslog_rfc3164.log": syslog_rfc3164,
    "syslog_rfc5424.log": syslog_rfc5424,
    "apache_combined.log": apache_combined,
    "apache_common.log": apache_common,
    "nginx_access.log": nginx_access,
    "nginx_error.log": nginx_error,
    "python_logging.log": python_logging,
    "log4j.log": log4j,
    "free_text.txt": free_text,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, generate in GENERATORS.items():
        (OUT / name).write_bytes("".join(line + "\n" for line in generate()).encode("utf-8"))


if __name__ == "__main__":
    main()
