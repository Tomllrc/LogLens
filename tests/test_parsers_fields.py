"""Tests for automatic field mapping: flattening, role lookup, fallbacks."""

import pytest

from loglens.parsers.fields import (
    COMPONENT,
    LEVEL,
    MESSAGE,
    TIMESTAMP,
    describe_fields,
    find_field,
    flatten,
    map_by_value,
    map_fields,
    normalize_key,
    render_value,
)


@pytest.mark.parametrize(
    ("key", "normalized"),
    [
        ("@timestamp", "timestamp"),
        ("eventTime", "eventtime"),
        ("event_time", "eventtime"),
        ("Event-Time", "eventtime"),
        ("log.level", "log.level"),
        ("kubernetes.container_name", "kubernetes.containername"),
    ],
)
def test_normalize_key(key: str, normalized: str) -> None:
    assert normalize_key(key) == normalized


def test_flatten_nested_mappings() -> None:
    data = {"a": 1, "b": {"c": {"d": "x"}, "e": [1, 2]}, "empty": {}}
    assert flatten(data) == {"a": 1, "b.c.d": "x", "b.e": [1, 2], "empty": {}}


def test_flatten_collision_keeps_first() -> None:
    assert flatten({"log.level": "error", "log": {"level": "info"}}) == {"log.level": "error"}


@pytest.mark.parametrize(
    ("value", "text"),
    [("x", "x"), (12, "12"), (1.5, "1.5"), (True, "true"), (None, "null"), ([1, "a"], '[1, "a"]')],
)
def test_render_value(value: object, text: str) -> None:
    assert render_value(value) == text


# --- Role lookup ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({"@timestamp": "2026-01-01T10:00:00Z"}, "@timestamp"),
        ({"ts": 1767225600.5}, "ts"),
        ({"eventTime": "2026-01-01"}, "eventTime"),
        ({"time": "2026-01-01", "timestamp": "2026-01-02"}, "timestamp"),  # priority order
        (
            {"System.TimeCreated.SystemTime": "2026-01-01T10:00:00Z"},
            "System.TimeCreated.SystemTime",
        ),
        ({"time": "soon"}, None),  # no digit: not a timestamp
        ({"date": ""}, None),
    ],
)
def test_find_timestamp(fields: dict[str, object], key: str | None) -> None:
    assert find_field(fields, TIMESTAMP) == key


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({"level": "info"}, "level"),
        ({"log.level": "error"}, "log.level"),
        ({"log": {"level": "error"}}, None),  # not flattened: a mapping is not a value
        ({"severity": "WARNING"}, "severity"),
        ({"levelname": "DEBUG"}, "levelname"),
        ({"level": 30}, "level"),  # pino/bunyan numeric levels
        ({"at": "error"}, "at"),  # Heroku
        ({"priority": "high"}, None),  # not a level word
        ({"details.severity": "error", "severity": "info"}, "severity"),  # exact beats nested
        ({"a.b.level": "info", "a.level": "warn"}, "a.level"),  # shallowest nested wins
        ({"level": True}, None),
    ],
)
def test_find_level(fields: dict[str, object], key: str | None) -> None:
    assert find_field(fields, LEVEL) == key


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({"logger": "orders.api"}, "logger"),
        ({"service.name": "payments"}, "service.name"),
        ({"kubernetes.container_name": "checkout"}, "kubernetes.container_name"),
        ({"System.Provider.Name": "Security"}, "System.Provider.Name"),
        ({"name": "checkout"}, "name"),
        ({"user.name": "alice"}, None),  # "name" only matches at the top level
        ({"service": "13ms"}, None),  # a duration, not a service name
        ({"logger": "x", "service": "y"}, "logger"),
    ],
)
def test_find_component(fields: dict[str, object], key: str | None) -> None:
    assert find_field(fields, COMPONENT) == key


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({"msg": "hello"}, "msg"),
        ({"message": "a", "msg": "b"}, "message"),
        ({"log": "line\n"}, "log"),
        ({"short_message": "gelf"}, "short_message"),
        ({"RenderingInfo.Message": "Logon failed."}, "RenderingInfo.Message"),
        ({"error.message": "boom"}, "error.message"),
        ({"message": "   "}, None),
    ],
)
def test_find_message(fields: dict[str, object], key: str | None) -> None:
    assert find_field(fields, MESSAGE) == key


def test_taken_keys_are_skipped() -> None:
    assert find_field({"level": "info"}, LEVEL, frozenset({"level"})) is None


# --- map_fields -----------------------------------------------------------------------


def test_map_fields_ecs() -> None:
    mapped = map_fields(
        flatten(
            {
                "@timestamp": "2026-03-14T09:12:01Z",
                "log": {"level": "error", "logger": "com.acme.Pay"},
                "message": "declined\n",
                "service": {"name": "payments"},
                "trace": {"id": "abc"},
            }
        )
    )
    assert mapped.timestamp_raw == "2026-03-14T09:12:01Z"
    assert mapped.level_raw == "error"
    assert mapped.message == "declined"  # trailing newline stripped
    assert mapped.component == "com.acme.Pay"  # log.logger: nested "logger" beats service.name
    assert mapped.extra == {"service.name": "payments", "trace.id": "abc"}


def test_map_fields_opentelemetry() -> None:
    mapped = map_fields(
        flatten(
            {
                "timeUnixNano": "1773479521000000000",
                "severityText": "WARN",
                "body": "disk almost full",
                "resource": {"service.name": "storage"},
            }
        )
    )
    assert (mapped.level_raw, mapped.message, mapped.component) == (
        "WARN",
        "disk almost full",
        "storage",
    )
    assert mapped.timestamp_raw is None  # unknown name: kept in extra rather than guessed
    assert "timeUnixNano" in mapped.extra


def test_map_fields_keeps_unmapped_fields_in_order() -> None:
    mapped = map_fields({"z": 1, "level": "info", "a": None, "b": [1]})
    assert list(mapped.extra.items()) == [("z", "1"), ("a", "null"), ("b", "[1]")]
    assert mapped.message is None


def test_map_by_value_for_headerless_columns() -> None:
    mapped = map_by_value(
        {"col1": "2026-03-14T09:12:01Z", "col2": "ERROR", "col3": "alice", "col4": "Payment failed"}
    )
    assert (mapped.timestamp_raw, mapped.level_raw, mapped.message) == (
        "2026-03-14T09:12:01Z",
        "ERROR",
        "Payment failed",
    )
    assert mapped.component is None
    assert mapped.extra == {"col3": "alice"}


def test_map_by_value_needs_a_multi_word_message() -> None:
    mapped = map_by_value({"col1": "1773479521", "col2": "alice", "col3": "12"})
    assert mapped.timestamp_raw == "1773479521"
    assert mapped.message is None
    assert mapped.extra == {"col2": "alice", "col3": "12"}


def test_describe_fields_quotes_when_needed() -> None:
    assert describe_fields({"a": "1", "b": "two words", "c": "", "d": 'say "hi"', "e": "k=v"}) == (
        'a=1 b="two words" c="" d="say \\"hi\\"" e="k=v"'
    )
