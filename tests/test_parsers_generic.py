"""Tests for the generic free-text parser."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loglens.parsers.generic import GenericParser, find_timestamp, strip_ansi
from loglens.split import Record


def _parse(text: str) -> tuple[str | None, str | None, str | None, str, float]:
    event = GenericParser().parse(Record("f", 1, 1, text))
    return event.timestamp_raw, event.level_raw, event.component, event.message, event.confidence


# Layouts of the Loghub datasets (synthetic lines in their formats) and a few others.
@pytest.mark.parametrize(
    ("line", "timestamp", "level", "component", "message"),
    [
        (
            "081109 203615 148 INFO dfs.DataNode$PacketResponder: PacketResponder 1 terminating",
            "081109 203615",
            "INFO",
            "dfs.DataNode$PacketResponder",
            "PacketResponder 1 terminating",
        ),
        (
            "2015-10-18 18:01:47,978 INFO [main] org.apache.hadoop.app.MRAppMaster: Created app",
            "2015-10-18 18:01:47,978",
            "INFO",
            "org.apache.hadoop.app.MRAppMaster",
            "Created app",
        ),
        (
            "17/06/09 20:10:40 INFO executor.Backend: Registered signal handlers",
            "17/06/09 20:10:40",
            "INFO",
            "executor.Backend",
            "Registered signal handlers",
        ),
        (
            "- 1117838570 2005.06.03 R02-M1-N0 2005-06-03-15.42.50.675872 R02 RAS KERNEL INFO "
            "cache parity error corrected",
            "1117838570",
            "INFO",
            None,
            "cache parity error corrected",
        ),
        (
            "Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure; uid=0",
            "Jun 14 15:16:01",
            None,
            "sshd(pam_unix)",
            "authentication failure; uid=0",
        ),
        (
            "[Sun Dec 04 04:47:44 2005] [notice] workerEnv.init() ok",
            "Sun Dec 04 04:47:44 2005",
            "notice",
            None,
            "workerEnv.init() ok",
        ),
        (
            "2016-09-28 04:30:30, Info                  CBS    Loaded Servicing Stack",
            "2016-09-28 04:30:30",
            "Info",
            None,
            "CBS    Loaded Servicing Stack",
        ),
        (
            "[10.30 16:49:06] chrome.exe - proxy.example.com:5070 close, 0 bytes sent",
            "10.30 16:49:06",
            None,
            "chrome.exe",  # "program - message" header (before Phase 2: left in the message)
            "proxy.example.com:5070 close, 0 bytes sent",
        ),
        (
            "20171223-22:15:29:606|Step_LSC|30002312|onStandStepChanged 3579",
            "20171223-22:15:29:606",
            None,
            None,
            "Step_LSC|30002312|onStandStepChanged 3579",
        ),
        (
            "10.0.0.5 - - [14/Mar/2026:09:12:01 +0000] request served",
            "14/Mar/2026:09:12:01 +0000",
            None,
            None,
            "request served",
        ),
        (
            "2026-03-14 09:12:01 something failed: timeout reached",
            "2026-03-14 09:12:01",
            None,
            None,
            "something failed: timeout reached",
        ),
    ],
)
def test_layouts(
    line: str, timestamp: str, level: str | None, component: str | None, message: str
) -> None:
    assert _parse(line)[:4] == (timestamp, level, component, message)


def test_prefix_before_the_timestamp_is_kept() -> None:
    event = GenericParser().parse(
        Record("f", 1, 1, "nova-api.log.1.2017-05-16_13:53:08 2017-05-16 00:00:00.008 25746 INFO x")
    )
    assert event.timestamp_raw == "2017-05-16 00:00:00.008"
    assert event.extra == {"prefix": "nova-api.log.1.2017-05-16_13:53:08"}


def test_lowercase_level_words_far_from_the_start_are_message_text() -> None:
    _, level, _, message, _ = _parse("2026-03-14 09:12:01 user bob reported an error today")
    assert level is None
    assert message == "user bob reported an error today"


def test_continuation_lines_are_kept_in_the_message() -> None:
    event = GenericParser().parse(
        Record("f", 1, 3, "2026-03-14 09:12:01 ERROR app: failed\nTraceback\n  File x")
    )
    assert event.message == "failed\nTraceback\n  File x"
    assert event.component == "app"


def test_confidence_reflects_what_was_found() -> None:
    assert _parse("2026-03-14 09:12:01 ERROR app: x")[4] == 0.9
    assert _parse("2026-03-14 09:12:01 x")[4] == 0.65
    assert _parse("ERROR x")[4] == 0.45
    assert _parse("nothing recognizable here")[4] == 0.3


def test_free_text_is_left_whole() -> None:
    assert _parse("just some words: here")[:4] == (None, None, None, "just some words: here")


def test_ansi_codes_are_stripped() -> None:
    assert strip_ansi("\x1b[31mERROR\x1b[0m done \x1b]0;title\x07") == "ERROR done "
    assert _parse("\x1b[1;33mWARN\x1b[0m disk")[1:4] == ("WARN", None, "disk")


def test_timestamps_inside_words_are_ignored() -> None:
    assert find_timestamp("build-2026-03-14 version") is None
    assert find_timestamp("id1234567890123 x") is None


@given(st.text(max_size=300))
def test_never_raises(text: str) -> None:
    event = GenericParser().parse(Record("f", 1, 1, text))
    assert 0.0 <= event.confidence <= 1.0
    assert isinstance(event.message, str)
