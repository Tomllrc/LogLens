"""Realistic files through the whole pipeline: ingest -> split -> detect -> parse.

Each fixture mixes well-formed records with malformed ones, as real logs do.
"""

from pathlib import Path

import pytest

from loglens.detect import DetectReport, iter_detected
from loglens.ingest import iter_lines
from loglens.parsers import MALFORMED_CONFIDENCE, ParsedEvent, create_parser, safe_parse
from loglens.split import Record, iter_records

FIXTURES = Path(__file__).parent / "fixtures" / "parsers"


def _pipeline(name: str) -> tuple[str, list[tuple[Record, ParsedEvent]]]:
    """The detected format of a fixture and every (record, event) pair, events only."""
    report = DetectReport()
    detected = list(iter_detected(iter_records(iter_lines(FIXTURES / name)), report=report))
    (result,) = report.detections.values()
    parser = create_parser(result.format_name, result.details)
    assert parser is not None, f"no parser for {result.format_name}"
    pairs = [(item.record, safe_parse(parser, item.record)) for item in detected]
    return result.format_name, [(record, event) for record, event in pairs if event.is_event]


def _summary(event: ParsedEvent) -> tuple[str | None, str | None, str | None, str]:
    return (event.timestamp_raw, event.level_raw, event.component, event.message)


# --- Structured application logs (JSON) -------------------------------------------------


def test_app_jsonl() -> None:
    format_name, pairs = _pipeline("app.jsonl")
    events = [event for _, event in pairs]
    assert format_name == "json"
    assert [_summary(e) for e in events] == [
        ("2026-03-14T09:12:01.114Z", "info", "orders.api", "Order created"),
        ("2026-03-14T09:12:01.532Z", "error", "payments", "Payment declined"),
        ("1773479521700", "30", "checkout", "cart loaded"),
        ("2026-03-14T09:12:02.007Z", "WARNING", "inventory", "stock low"),
        (
            "2026-03-14T09:12:02.450Z",
            "debug",
            "orders.db",
            'query="SELECT * FROM orders WHERE id = ?" rows=1',
        ),
        (None, None, None, events[5].message),
        ("2026-03-14T09:12:04.120Z", "info", "orders.api", "Order shipped"),
    ]
    assert events[0].extra == {
        "order_id": "8812",
        "user.id": "42",
        "user.plan": "pro",
        "duration_ms": "12.5",
    }
    assert events[1].extra == {
        "error.type": "CardDeclined",
        "error.code": "insufficient_funds",
        "ecs.version": "8.11.0",
    }
    assert events[6].extra == {"order_id": "8812", "carrier": "null", "tags": '["express", "gift"]'}


def test_app_jsonl_malformed_lines() -> None:
    _, pairs = _pipeline("app.jsonl")
    (stray_record, stray), (cut_record, cut) = pairs[4], pairs[5]
    # A non-JSON line after a good object: the splitter attached it; the object survives.
    assert (stray_record.line_start, stray_record.line_end) == (5, 6)
    assert stray.extra["trailing_text"] == "not a json line at all"
    assert stray.confidence == 0.5
    # A truncated object: low confidence, raw text kept as the message.
    assert cut.confidence == MALFORMED_CONFIDENCE
    assert cut.message == cut_record.text
    assert cut.error is not None and cut.error.startswith("invalid JSON")


# --- Kubernetes-style JSON logs ---------------------------------------------------------


def test_kubernetes_json() -> None:
    format_name, pairs = _pipeline("k8s.jsonl")
    events = [event for _, event in pairs]
    assert format_name == "json"
    assert [_summary(e) for e in events] == [
        (
            "2026-03-14T09:12:01.114623Z",
            None,
            "checkout",
            "2026-03-14T09:12:01.114Z INFO  Started checkout service on :8080",
        ),
        ("2026-03-14T09:12:05.002Z", None, "checkout", "GET /healthz 200 1ms"),
        (
            "2026-03-14T09:12:07.431Z",
            None,
            "payments",
            "panic: runtime error: invalid memory address or nil pointer dereference",
        ),
        ("1773479528.123", None, None, "SyncLoop (PLEG): event for pod"),
        ("2026-03-14T09:12:09.900Z", "error", "controller.deployment", "Reconciler error"),
    ]
    assert events[0].extra == {
        "stream": "stdout",
        "kubernetes.pod_name": "checkout-7d9f8b6c4-x2klm",
        "kubernetes.namespace_name": "shop",
        "kubernetes.labels.app": "checkout",
        "kubernetes.labels.pod-template-hash": "7d9f8b6c4",
        "kubernetes.host": "node-a",
    }
    assert events[3].extra["event.Type"] == "ContainerStarted"
    assert events[4].extra["name"] == "checkout"  # "name" is not the component: logger won
    assert all(e.confidence == 1.0 for e in events)


# --- logfmt from a web server -----------------------------------------------------------


def test_web_server_logfmt() -> None:
    format_name, pairs = _pipeline("web.log")
    events = [event for _, event in pairs]
    assert format_name == "logfmt"
    assert [_summary(e)[:3] for e in events] == [
        ("2026-03-14T09:12:01Z", "info", None),
        ("2026-03-14T09:12:02Z", "warning", None),
        ("2026-03-14T09:12:03Z", "error", None),
        (None, "info", None),  # Heroku router: at=info, and service=13ms is not a component
        ("2026-03-14T09:12:05Z", "info", "cache"),
        (None, None, None),
        ("2026-03-14T09:12:06Z", "info", None),
    ]
    assert events[2].message == "upstream timeout"
    assert events[2].extra["error"] == "dial tcp 10.0.1.4:443: i/o timeout"
    assert events[3].message.startswith("method=GET path=/ host=shop.example.com")
    assert events[3].extra["service"] == "13ms"
    assert events[4].extra["note"] == 'said "hello" twice'
    assert (events[5].message, events[5].confidence) == (
        "garbage line without any pairs",
        MALFORMED_CONFIDENCE,
    )


# --- CSV exports ------------------------------------------------------------------------


def test_csv_export() -> None:
    format_name, pairs = _pipeline("export.csv")
    events = [event for _, event in pairs]
    assert format_name == "csv"
    assert [_summary(e) for e in events] == [
        ("2026-03-14T09:12:01Z", "INFO", "orders", "Order created, id 8812"),
        ("2026-03-14T09:12:02Z", "WARN", "payments", "Retrying charge, attempt 2"),
        ("2026-03-14T09:12:03Z", "ERROR", "payments", 'Charge failed: "card declined"'),
        ("2026-03-14T09:12:04Z", "INFO", "orders", "Order shipped"),
        (None, None, None, '2026-03-14T09:12:05Z,ERROR,orders,"broken quote,7f3e,3'),
        ("2026-03-14T09:12:06Z", "INFO", "orders", "Order delivered"),
    ]
    assert events[0].extra == {"request_id": "7f3a", "latency_ms": "12"}
    assert [e.confidence for e in events] == [1.0, 1.0, 1.0, 0.5, MALFORMED_CONFIDENCE, 1.0]
    assert events[3].error == "expected 6 columns, got 5"


def test_csv_header_is_not_an_event() -> None:
    _, pairs = _pipeline("export.csv")
    assert all(record.line_start != 1 for record, _ in pairs)


# --- XML events -------------------------------------------------------------------------


def test_windows_event_xml() -> None:
    format_name, pairs = _pipeline("events.xml")
    events = [event for _, event in pairs]
    assert format_name == "xml"
    security = "Microsoft-Windows-Security-Auditing"
    assert [_summary(e) for e in events] == [
        ("2026-03-14T09:12:01.114Z", "4", security, "An account was successfully logged on."),
        ("2026-03-14T09:12:02.001Z", "2", security, "An account failed to log on."),
        (None, None, None, events[2].message),
        (
            "2026-03-14T09:12:03.500Z",
            "4",
            "Service Control Manager",
            "System.EventID=7036 System.Computer=dc01.corp.example"
            ' EventData.param1="Windows Update" EventData.param2=running',
        ),
        (
            "2026-03-14T09:12:04.000Z",
            "1",
            "Microsoft-Windows-Kernel-Power",
            "The system has rebooted without cleanly shutting down first.",
        ),
        ("2026-03-14T09:12:05.250Z", "4", security, "An account was logged off."),
        ("2026-03-14T09:12:06.800Z", "4", security, "An account was successfully logged on."),
    ]
    assert events[1].extra["EventData.TargetUserName"] == "bob"
    assert events[2].confidence == MALFORMED_CONFIDENCE  # an event cut off mid-way
    assert events[6].confidence == 1.0  # the wrapper's </Events> attached to it is ignored


@pytest.mark.parametrize("name", ["app.jsonl", "k8s.jsonl", "web.log", "export.csv", "events.xml"])
def test_every_line_is_accounted_for(name: str) -> None:
    """Nothing is lost: events and non-events together cover every line exactly once."""
    report = DetectReport()
    records = [
        d.record for d in iter_detected(iter_records(iter_lines(FIXTURES / name)), report=report)
    ]
    covered = [n for r in records for n in range(r.line_start, r.line_end + 1)]
    total = len((FIXTURES / name).read_text(encoding="utf-8").splitlines())
    assert covered == list(range(1, total + 1))
