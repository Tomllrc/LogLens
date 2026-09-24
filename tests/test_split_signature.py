"""Tests for record-start signatures and signature learning."""

import pytest

from loglens.split.signature import (
    BRACKET_START,
    LEVEL_START,
    TIMESTAMP_START,
    is_strong_continuation,
    learn_signature,
    looks_like_continuation,
)


@pytest.mark.parametrize(
    "text",
    [
        "2026-01-31 10:00:00 INFO x",
        "2026-01-31T10:00:00.123Z INFO x",
        "2026/01/31 10:00:00 x",
        "[2026-01-31 10:00:00] x",
        "(2026-01-31) x",
        "31/01/2026 10:00:00 x",
        "31.01.26 10:00 x",
        "31/Jan/2026:10:00:00 +0000 x",
        "31 Jan 2026 10:00:00 x",
        "Jan 31 10:00:00 host sshd[1]: x",
        "Jan  1 10:00:00 host x",
        "Mon Jan 31 10:00:00 2026 x",
        "Mon, 31 Jan 2026 10:00:00 GMT x",
        "10:00:00.123 INFO x",
        "I0131 10:00:00.123456  1 main.go:10] x",
        "1767225600 x",
        "1767225600.123 x",
        "1767225600123 x",
    ],
)
def test_timestamp_start_matches(text: str) -> None:
    assert TIMESTAMP_START.match(text)


@pytest.mark.parametrize(
    "text",
    [
        "  2026-01-31 indented",
        "INFO 2026-01-31 level first",
        "Decided 3 times",
        "May 3 things happened",
        "Monitoring 5 hosts",
        "1.2.3 version",
        "123456789 nine digits",
        "12345678901234 fourteen digits",
        "user=bob 2026-01-31",
        "",
    ],
)
def test_timestamp_start_rejects(text: str) -> None:
    assert not TIMESTAMP_START.match(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("INFO starting", True),
        ("[ERROR] failed", True),
        ("<warn> x", True),
        ("WARNING:root:message", True),
        ("Error: boom", True),
        ("debug", True),
        ("E1 not a level", False),
        ("information about x", False),
        ("Errors occurred", False),
        ("  INFO indented", False),
    ],
)
def test_level_start(text: str, expected: bool) -> None:
    assert (LEVEL_START.match(text) is not None) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[main] starting", True),
        ("<34>Oct 11 22:14:15 host x", True),
        ("[unterminated", False),
        ("x [main]", False),
    ],
)
def test_bracket_start(text: str, expected: bool) -> None:
    assert (BRACKET_START.match(text) is not None) is expected


@pytest.mark.parametrize(
    ("text", "strong", "any_evidence"),
    [
        ("\tat com.acme.App.main(App.java:10)", True, True),
        ("    at x", True, True),
        ("at x", True, True),
        ("Caused by: java.io.IOException: x", True, True),
        ("\t... 5 more", True, True),
        ("... 5 more", True, True),
        ("}", True, True),
        ("]", True, True),
        ("", True, True),
        ("Traceback (most recent call last):", False, True),
        ("ValueError: boom", False, True),
        ("java.lang.IllegalStateException: boom", False, True),
        ("During handling of the above exception, another exception occurred:", False, True),
        ("SELECT * FROM users", False, False),
        ("2026-01-31 INFO x", False, False),
    ],
)
def test_continuation_evidence(text: str, strong: bool, any_evidence: bool) -> None:
    assert is_strong_continuation(text) is strong
    assert looks_like_continuation(text) is any_evidence


def _learn(*lines: str) -> str:
    return learn_signature(lines).name


def test_learn_timestamp_with_stack_trace() -> None:
    assert (
        _learn(
            "2026-01-31 10:00:00 ERROR failed",
            "java.lang.IllegalStateException: boom",
            "\tat A.b(A.java:1)",
            "2026-01-31 10:00:01 INFO ok",
        )
        == "timestamp"
    )


def test_learn_timestamp_with_unindented_sql() -> None:
    assert (
        _learn(
            "2026-01-31 10:00:00 DEBUG executing:",
            "SELECT *",
            "FROM users",
            "WHERE id = 1",
            "2026-01-31 10:00:01 DEBUG done",
        )
        == "timestamp"
    )


def test_learn_prefers_timestamp_over_bracket_on_tie() -> None:
    assert _learn("[2026-01-31 10:00:00] a", "[2026-01-31 10:00:01] b") == "timestamp"


def test_learn_prefers_level_over_bracket_on_tie() -> None:
    assert _learn("[INFO] a", "[WARN] b") == "level"


def test_learn_bracket() -> None:
    assert _learn("[main] a", "[worker-1] b", "\tat x") == "bracket"


def test_learn_json() -> None:
    assert _learn("{", '  "a": 1', "}", "{", '  "a": 2', "}") == "json"
    assert _learn('{"a": 1}', '{"a": 2}') == "json"


def test_learn_level_first_python_format() -> None:
    lines = ("INFO:root:started", "ERROR:root:failed", "Traceback (most recent call last):")
    assert _learn(*lines) == "level"


def test_learn_rejects_rare_accidental_match() -> None:
    lines = ["some free text"] * 20 + ["2026-01-31 happens to start with a date"]
    assert _learn(*lines) == "unindented"


def test_learn_fallbacks() -> None:
    assert _learn("alpha", "beta", "gamma") == "unindented"
    assert _learn("  a", "  b", "  c", "d") == "every-line"
    assert _learn() == "unindented"
    assert _learn("", "   ") == "unindented"
