"""Tricky real-world lines for each declarative text format, beyond the YAML samples."""

from pathlib import Path

import pytest

from loglens.detect import DetectReport, detect_format, iter_detected
from loglens.ingest import iter_lines
from loglens.parsers import MALFORMED_CONFIDENCE, TEXT_FORMATS, ParsedEvent, create_parser
from loglens.parsers.textformats import TextFormatParser
from loglens.split import Record, SplitReport, iter_records

BOM = chr(0xFEFF)


def _parse(format_name: str, text: str) -> ParsedEvent:
    return TextFormatParser(TEXT_FORMATS[format_name]).parse(Record("t.log", 1, 1, text))


def _fields(event: ParsedEvent) -> dict[str, str | None]:
    return {
        "timestamp": event.timestamp_raw,
        "level": event.level,
        "level_raw": event.level_raw,
        "component": event.component,
        "message": event.message,
        **event.extra,
    }


TRICKY = [
    # syslog RFC 3164
    (
        "syslog_rfc3164",
        "Mar 14 09:12:01 web-1 sshd[4312]: error: kex_exchange_identification: Connection closed",
        {"component": "sshd", "message": "error: kex_exchange_identification: Connection closed"},
    ),
    (
        "syslog_rfc3164",
        "<13>Mar 14 09:12:01 10.0.0.1 dhclient: DHCPACK of 10.0.0.9 from 10.0.0.1",
        {"host": "10.0.0.1", "component": "dhclient", "level": "INFO", "level_raw": "13"},
    ),
    (
        "syslog_rfc3164",
        "<191>Mar 14 09:12:01 fw01 filterlog[88]: 5,,,1000000103,igb0,match,block,in",
        {"level": "DEBUG", "message": "5,,,1000000103,igb0,match,block,in"},
    ),
    # syslog RFC 5424
    (
        "syslog_rfc5424",
        '<14>1 2026-03-14T09:12:01Z host app 1 - [meta@1 note="a \\] b" q="\\"x\\""] done',
        {"structured_data": '[meta@1 note="a \\] b" q="\\"x\\""]', "message": "done"},
    ),
    (
        "syslog_rfc5424",
        f"<11>1 2026-03-14T09:12:01.5+01:00 host app - - - {BOM}with a byte order mark",
        {
            "level": "ERROR",
            "message": "with a byte order mark",
            "timestamp": "2026-03-14T09:12:01.5+01:00",
        },
    ),
    # Apache / Nginx access
    (
        "apache_common",
        '10.0.0.5 - alice@example.com [14/Mar/2026:09:12:01 +0000] "GET /search?q=\\"x\\" HTTP/1.1" 200 12',  # noqa: E501 (verbatim log line)
        {"user": "alice@example.com", "path": '/search?q=\\"x\\"', "method": "GET"},
    ),
    (
        "apache_common",
        '10.0.0.5 - - [14/Mar/2026:09:12:01 +0000] "GET /a b c HTTP/1.1" 400 0',
        {"message": "GET /a b c HTTP/1.1", "level": "WARN", "method": None},
    ),
    (
        "apache_combined",
        '10.0.0.5 - - [14/Mar/2026:09:12:01 +0000] "GET / HTTP/1.1" 301 - "-" "Mozilla/5.0 (compatible; \\"Bot\\")"',  # noqa: E501 (verbatim log line)
        {
            "referer": None,
            "bytes": None,
            "user_agent": 'Mozilla/5.0 (compatible; \\"Bot\\")',
            "level": "INFO",
        },
    ),
    (
        "apache_combined",
        '10.0.0.5 - - [14/Mar/2026:09:12:01 +0000] "-" - - "-" "-"',
        {"status": None, "level": None, "level_raw": None},
    ),
    # Nginx error
    (
        "nginx_error",
        "2026/03/14 09:12:01 [crit] 7#7: *9 SSL_do_handshake() failed, client: 203.0.113.7, server: 0.0.0.0:443",  # noqa: E501 (verbatim log line)
        {"level": "FATAL", "connection": "9", "client": "203.0.113.7", "server": "0.0.0.0:443"},
    ),
    (
        "nginx_error",
        '2026/03/14 09:12:01 [error] 7#7: *10 open() "/srv/a,b.html" failed, client: 10.0.0.5, request: "GET /a,b.html HTTP/1.1"',  # noqa: E501 (verbatim log line)
        {"request": "GET /a,b.html HTTP/1.1", "client": "10.0.0.5"},
    ),
    # log4j / logback
    (
        "log4j",
        "2026-03-14 09:12:01,114 WARN  [pool-1-thread-2 worker] com.acme.Outer$Inner - retry 2/3",
        {"thread": "pool-1-thread-2 worker", "component": "com.acme.Outer$Inner", "level": "WARN"},
    ),
    (
        "log4j",
        "2026-03-14 09:12:01.114 SEVERE [main] com.acme.Legacy - java.util.logging level",
        {"level": "ERROR", "level_raw": "SEVERE"},
    ),
    # Python logging
    (
        "python_logging",
        "2026-03-14 09:12:01,114 - my-app.db - WARNING - pool exhausted - waiting 5s",
        {"component": "my-app.db", "message": "pool exhausted - waiting 5s", "level": "WARN"},
    ),
    # Go log
    (
        "go_log",
        "api.v2: 2026/03/14 09:12:01.000001 /home/dev/api/server.go:88: [WARN] slow query",
        {
            "component": "api.v2",
            "file": "/home/dev/api/server.go:88",
            "level": "WARN",
            "message": "slow query",
        },
    ),
    (
        "go_log",
        "2026/03/14 09:12:01 [info] lowercase tags are not levels",
        {"level": None, "message": "[info] lowercase tags are not levels"},
    ),
]


@pytest.mark.parametrize(
    ("format_name", "line", "expected"), TRICKY, ids=[f"{t[0]}-{i}" for i, t in enumerate(TRICKY)]
)
def test_tricky_line(format_name: str, line: str, expected: dict[str, str | None]) -> None:
    event = _parse(format_name, line)
    assert event.confidence == 1.0, event.error
    fields = _fields(event)
    assert {key: fields.get(key) for key in expected} == expected


NOT_MATCHING = [
    ("syslog_rfc3164", "Mar 14 2026 09:12:01 web-1 sshd[1]: the year is not part of the format"),
    ("syslog_rfc3164", "2026-03-14T09:12:01Z INFO payments started"),  # ISO needs "tag:"
    ("syslog_rfc5424", "<14>1 2026-03-14 09:12:01 host app - - - space instead of T"),
    ("apache_common", '10.0.0.5 - - [14/Mar/2026:09:12:01 +0000] "GET / HTTP/1.1" 200'),
    ("nginx_error", "2026/03/14 09:12:01 [fatal] 1#1: fatal is not an nginx level"),
    ("log4j", "2026-03-14 09:12:01 INFO  [main] com.acme.App - no milliseconds"),
    ("python_logging", "warning:root:levels are upper case"),
    ("go_log", "2026-03-14 09:12:01 dashes instead of slashes"),
    ("journal_export", "MESSAGE=no cursor first"),
    ("windows_event_csv", "Informational,3/14/2026 9:12:01 AM,Source,1,None,msg"),
    ("windows_event_text", "Event 0:\n  Source: x"),
]


@pytest.mark.parametrize(("format_name", "line"), NOT_MATCHING, ids=[t[0] for t in NOT_MATCHING])
def test_non_matching_line_is_malformed_not_an_error(format_name: str, line: str) -> None:
    event = _parse(format_name, line)
    assert event.confidence == MALFORMED_CONFIDENCE
    assert event.message == line.strip()
    assert event.error == f"does not match the {format_name} format"


def test_nginx_error_ties_with_go_log_and_wins_on_priority() -> None:
    line = "2026/03/14 09:12:01 [error] 7#7: *1 upstream timed out"
    result = detect_format([line, line])
    assert result.format_name == "nginx_error"
    assert result.scores_by_format["go_log"] == 1.0


def test_json_payload_in_log4j_stays_log4j() -> None:
    line = '2026-03-14 09:12:01,114 INFO  [main] com.acme.Api - {"order": 1}'
    assert detect_format([line]).format_name == "log4j"


# --- Multi-line formats through the whole pipeline ----------------------------------------


def _pipeline(path: Path) -> tuple[str, list[tuple[Record, ParsedEvent]]]:
    split_report, detect_report = SplitReport(), DetectReport()
    records = iter_records(iter_lines(path), report=split_report)
    detected = list(iter_detected(records, report=detect_report))
    (result,) = detect_report.detections.values()
    parser = create_parser(result.format_name, result.details)
    assert parser is not None
    (signature,) = split_report.signatures.values()
    assert signature == result.format_name  # the format's record_start drove the split
    return result.format_name, [(d.record, parser.parse(d.record)) for d in detected]


def test_journal_export_file(tmp_path: Path) -> None:
    path = tmp_path / "journal.export"
    path.write_text(
        "__CURSOR=s=1;i=1\n__REALTIME_TIMESTAMP=1773479521114000\nPRIORITY=6\n"
        "SYSLOG_IDENTIFIER=sshd\n_PID=4312\nMESSAGE=Accepted publickey for deploy\n\n"
        "__CURSOR=s=1;i=2\n__REALTIME_TIMESTAMP=1773479522000000\nPRIORITY=4\n"
        "SYSLOG_IDENTIFIER=systemd\nMESSAGE=Unit backup.service entered failed state\n\n"
        "__CURSOR=s=1;i=3\n__REALTIME_TIMESTAMP=1773479523000000\n_TRANSPORT=kernel\n",
        encoding="utf-8",
    )
    format_name, pairs = _pipeline(path)
    assert format_name == "journal_export"
    assert [(r.line_start, r.line_end) for r, _ in pairs] == [(1, 7), (8, 13), (14, 16)]
    events = [event for _, event in pairs]
    assert [(e.level, e.component, e.message) for e in events[:2]] == [
        ("INFO", "sshd", "Accepted publickey for deploy"),
        ("WARN", "systemd", "Unit backup.service entered failed state"),
    ]
    # No MESSAGE field: the message is built from the other fields, nothing is lost
    # (values holding "=" are quoted to stay unambiguous).
    assert events[2].message == '__CURSOR="s=1;i=3" _TRANSPORT=kernel'
    assert events[0].extra["_PID"] == "4312"


def test_windows_event_viewer_csv_export(tmp_path: Path) -> None:
    path = tmp_path / "security.csv"
    path.write_text(
        "Level,Date and Time,Source,Event ID,Task Category\n"
        "Information,3/14/2026 9:12:01 AM,Microsoft-Windows-Security-Auditing,4624,Logon,"
        '"An account was successfully logged on.\n\nSubject:\n\tAccount Name:\t\tDC01$"\n'
        "Audit Failure,3/14/2026 9:12:02 AM,Microsoft-Windows-Security-Auditing,4625,Logon,"
        '"An account failed to log on, twice."\n'
        "Error,3/14/2026 9:12:05 AM,Service Control Manager,7031,None,Spooler stopped.\n",
        encoding="utf-8",
    )
    format_name, pairs = _pipeline(path)
    assert format_name == "windows_event_csv"
    header, *rows = [event for _, event in pairs]
    assert header.is_event is False
    # The first row's quoted message spans lines 2-5; the others fit on one line.
    assert [(r.line_start, r.line_end) for r, _ in pairs] == [(1, 1), (2, 5), (6, 6), (7, 7)]
    assert [(e.level, e.extra["event_id"]) for e in rows] == [
        ("INFO", "4624"),
        ("WARN", "4625"),
        ("ERROR", "7031"),
    ]
    assert rows[0].message.endswith("Account Name:\t\tDC01$")
    assert rows[1].message == "An account failed to log on, twice."


def test_wevtutil_text_export(tmp_path: Path) -> None:
    event = (
        "Event[{n}]:\n  Log Name: System\n  Source: Service Control Manager\n"
        "  Date: 2026-03-14T09:12:0{n}.000\n  Event ID: 7036\n  Task: N/A\n  Level: {level}\n"
        "  Opcode: N/A\n  Keyword: Classic\n  User: N/A\n  User Name: N/A\n"
        "  Computer: dc01\n  Description: \nThe {svc} service entered the {state} state.\n"
    )
    path = tmp_path / "system.txt"
    path.write_text(
        event.format(n=0, level="Information", svc="Print Spooler", state="running")
        + event.format(n=1, level="Error", svc="Backup", state="stopped"),
        encoding="utf-8",
    )
    format_name, pairs = _pipeline(path)
    assert format_name == "windows_event_text"
    assert [(e.level, e.component, e.message) for _, e in pairs] == [
        ("INFO", "Service Control Manager", "The Print Spooler service entered the running state."),
        ("ERROR", "Service Control Manager", "The Backup service entered the stopped state."),
    ]
    assert pairs[1][1].extra["Keyword"] == "Classic"
