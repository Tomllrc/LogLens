"""Unit tests for each structured parser, the registry, and the never-raise guarantee."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loglens.parsers import (
    MALFORMED_CONFIDENCE,
    CsvParser,
    JsonParser,
    LogfmtParser,
    ParsedEvent,
    Parser,
    PrefixJsonParser,
    TextFormatParser,
    XmlParser,
    create_parser,
    safe_parse,
)
from loglens.parsers.values import split_text_prefix
from loglens.split import Record


def _rec(text: str) -> Record:
    return Record("app.log", 1, text.count("\n") + 1, text)


def _parse(parser: Parser, text: str) -> ParsedEvent:
    return parser.parse(_rec(text))


# --- JSON -----------------------------------------------------------------------------


def test_json_maps_nested_fields() -> None:
    event = _parse(
        JsonParser(),
        '{"time": "2026-03-14T09:12:01Z", "lvl": "warn", "service": {"name": "api"},'
        ' "msg": "slow", "http": {"status": 504}}',
    )
    assert event == ParsedEvent(
        message="slow",
        timestamp_raw="2026-03-14T09:12:01Z",
        level_raw="warn",
        component="api",
        extra={"http.status": "504"},
    )


def test_json_pretty_printed_multi_line_record() -> None:
    event = _parse(JsonParser(), '{\n  "level": "info",\n  "message": "multi\\nline"\n}')
    assert (event.level_raw, event.message) == ("info", "multi\nline")


def test_json_without_message_builds_one() -> None:
    event = _parse(JsonParser(), '{"level": "info", "user": "bob", "action": "login"}')
    assert event.message == "user=bob action=login"
    assert event.extra == {"user": "bob", "action": "login"}
    assert event.confidence == 1.0


def test_json_trailing_text_is_kept_with_lower_confidence() -> None:
    event = _parse(JsonParser(), '{"msg": "ok"}\nstray line')
    assert event.message == "ok"
    assert event.extra == {"trailing_text": "stray line"}
    assert event.confidence == 0.5
    assert event.error == "text after the JSON object"


@pytest.mark.parametrize(
    "text",
    ['{"cut', "[1, 2]", '"just a string"', "plain text", "", "{" * 5000],
)
def test_json_malformed(text: str) -> None:
    event = _parse(JsonParser(), text)
    assert event.confidence == MALFORMED_CONFIDENCE
    assert event.message == (text.strip() or text)
    assert event.error


# --- Prefix + JSON --------------------------------------------------------------------


def test_prefix_json_fills_gaps_from_the_prefix() -> None:
    event = _parse(
        PrefixJsonParser(), '2026-03-14T09:12:01Z INFO payments {"order": 1, "msg": "charged"}'
    )
    assert (event.timestamp_raw, event.level_raw, event.component, event.message) == (
        "2026-03-14T09:12:01Z",
        "INFO",
        "payments",
        "charged",
    )
    assert event.extra == {"order": "1"}


def test_prefix_json_payload_fields_win_over_prefix() -> None:
    event = _parse(
        PrefixJsonParser(),
        '2026-03-14T09:12:01Z WARN {"ts": "2026-03-14T09:12:00.5Z", "level": "error", "msg": "x"}',
    )
    assert (event.timestamp_raw, event.level_raw) == ("2026-03-14T09:12:00.5Z", "error")
    assert event.extra == {"prefix.timestamp": "2026-03-14T09:12:01Z", "prefix.level": "WARN"}


def test_prefix_json_kubernetes_cri_prefix() -> None:
    event = _parse(
        PrefixJsonParser(),
        '2026-03-14T09:12:01.114623Z stdout F {"level":"info","logger":"api","msg":"ready"}',
    )
    assert (event.timestamp_raw, event.component, event.message) == (
        "2026-03-14T09:12:01.114623Z",
        "api",
        "ready",
    )
    assert event.extra == {"prefix": "stdout F"}


def test_prefix_json_malformed() -> None:
    event = _parse(PrefixJsonParser(), '{"no": "prefix"}')
    assert event.confidence == MALFORMED_CONFIDENCE


@pytest.mark.parametrize(
    ("prefix", "parts"),
    [
        ("2026-03-14T09:12:01Z INFO payments ", ("2026-03-14T09:12:01Z", "INFO", "payments")),
        ("[2026-03-14 09:12:01,123] [ERROR] api: ", ("2026-03-14 09:12:01,123", "ERROR", "api")),
        ("Mar 14 09:12:01 host app[42]: ", ("Mar 14 09:12:01", None, "host app[42]")),
        ("1773479521.5 warning ", ("1773479521.5", "warning", "")),
        ("information only ", (None, "information", "only")),
        ("payments | ", (None, None, "payments")),
        ("", (None, None, "")),
    ],
)
def test_split_text_prefix(prefix: str, parts: tuple[str | None, str | None, str]) -> None:
    assert split_text_prefix(prefix) == parts


# --- CSV ------------------------------------------------------------------------------


def test_csv_with_header() -> None:
    parser = CsvParser(",", has_header=True)
    header = _parse(parser, "time,level,logger,msg,user")
    event = _parse(parser, '2026-03-14T09:12:01Z,ERROR,auth,"Login failed, locked",bob')
    assert header.is_event is False
    assert event == ParsedEvent(
        message="Login failed, locked",
        timestamp_raw="2026-03-14T09:12:01Z",
        level_raw="ERROR",
        component="auth",
        extra={"user": "bob"},
    )


def test_csv_tsv_from_detection_details() -> None:
    parser = create_parser("csv", {"delimiter": "tab", "columns": "3", "header": "yes"})
    assert parser is not None
    _parse(parser, "timestamp\tseverity\tmessage")
    event = _parse(parser, "2026-03-14T09:12:01Z\tWARN\tdisk, almost full")
    assert (event.level_raw, event.message) == ("WARN", "disk, almost full")


def test_csv_width_mismatch_is_partial() -> None:
    parser = CsvParser(",", has_header=True)
    _parse(parser, "time,level,msg")
    short = _parse(parser, "2026-03-14T09:12:01Z,INFO")
    long = _parse(parser, "2026-03-14T09:12:01Z,INFO,hello,surplus")
    assert (short.confidence, short.error) == (0.5, "expected 3 columns, got 2")
    assert (long.message, long.extra, long.confidence) == ("hello", {"col4": "surplus"}, 0.5)


def test_csv_header_names_are_cleaned() -> None:
    parser = CsvParser(",", has_header=True)
    _parse(parser, " time ,level,,level")
    event = _parse(parser, "2026-03-14T09:12:01Z,INFO,x,y")
    assert event.timestamp_raw == "2026-03-14T09:12:01Z"
    assert event.extra == {"col3": "x", "level_2": "y"}


def test_csv_without_header_guesses_from_values() -> None:
    parser = CsvParser(";")
    event = _parse(parser, "2026-03-14T09:12:01Z;WARN;orders;Stock is running low")
    assert (event.timestamp_raw, event.level_raw, event.message) == (
        "2026-03-14T09:12:01Z",
        "WARN",
        "Stock is running low",
    )
    assert event.extra == {"col3": "orders"}
    assert event.confidence == 0.8
    assert _parse(parser, "2026-03-14T09:12:02Z;INFO").confidence == 0.5  # narrower than row 1


@pytest.mark.parametrize("text", ['a,"unterminated', "", "a,b\nc,d"])
def test_csv_malformed(text: str) -> None:
    event = _parse(CsvParser(","), text)
    assert event.confidence == MALFORMED_CONFIDENCE


def test_csv_malformed_first_row_does_not_become_the_header() -> None:
    parser = CsvParser(",", has_header=True)
    assert _parse(parser, 'bad,"row').confidence == MALFORMED_CONFIDENCE
    assert _parse(parser, "time,msg").is_event is False
    assert _parse(parser, "2026-03-14T09:12:01Z,hello world").message == "hello world"


# --- logfmt ---------------------------------------------------------------------------


def test_logfmt_quoted_values_and_escapes() -> None:
    event = _parse(
        LogfmtParser(),
        'ts=2026-03-14T09:12:01Z level=info msg="said \\"hi\\"\\n" path=/a empty= logger=web',
    )
    assert (event.timestamp_raw, event.level_raw, event.component) == (
        "2026-03-14T09:12:01Z",
        "info",
        "web",
    )
    assert event.message == 'said "hi"'
    assert event.extra == {"path": "/a", "empty": ""}
    assert event.confidence == 1.0


def test_logfmt_loose_words() -> None:
    no_msg = _parse(LogfmtParser(), "2026-03-14 INFO user=bob action=login")
    with_msg = _parse(LogfmtParser(), "GET done msg=ok status=200")
    assert (no_msg.message, no_msg.confidence) == ("2026-03-14 INFO", 0.5)
    assert (with_msg.message, with_msg.extra) == ("ok", {"status": "200", "text": "GET done"})


def test_logfmt_repeated_keys() -> None:
    event = _parse(LogfmtParser(), "msg=x tag=a tag=b tag=c")
    assert event.component == "a"
    assert event.extra == {"tag[2]": "b", "tag[3]": "c"}


def test_logfmt_invalid_escape_keeps_the_raw_value() -> None:
    event = _parse(LogfmtParser(), 'msg="bad \\q escape" level=info')
    assert (event.message, event.confidence) == ("bad \\q escape", 1.0)


def test_logfmt_malformed() -> None:
    event = _parse(LogfmtParser(), "no pairs here")
    assert (event.message, event.confidence) == ("no pairs here", MALFORMED_CONFIDENCE)


# --- XML ------------------------------------------------------------------------------

WINDOWS_EVENT = """<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
  <System>
    <Provider Name="Microsoft-Windows-Security-Auditing" Guid="{5484}"/>
    <EventID>4625</EventID>
    <Level>2</Level>
    <TimeCreated SystemTime="2026-03-14T09:12:02.001Z"/>
  </System>
  <EventData>
    <Data Name="TargetUserName">bob</Data>
    <Data Name="SubjectLogonId"></Data>
  </EventData>
  <RenderingInfo Culture="en-US"><Message>An account failed to log on.</Message></RenderingInfo>
</Event>"""


def test_xml_windows_event() -> None:
    event = _parse(XmlParser(), WINDOWS_EVENT)
    assert (event.timestamp_raw, event.level_raw, event.component, event.message) == (
        "2026-03-14T09:12:02.001Z",
        "2",
        "Microsoft-Windows-Security-Auditing",
        "An account failed to log on.",
    )
    assert event.extra == {
        "System.Provider.Guid": "{5484}",
        "System.EventID": "4625",
        "EventData.TargetUserName": "bob",
        "EventData.SubjectLogonId": "",
        "RenderingInfo.Culture": "en-US",
        "element": "Event",
    }


def test_xml_attributes_and_root_text() -> None:
    event = _parse(
        XmlParser(),
        '<log4j:event logger="com.acme.App" level="ERROR" timestamp="1773479521700"'
        ' xmlns:log4j="http://jakarta.apache.org/log4j/">Payment failed</log4j:event>',
    )
    assert (event.timestamp_raw, event.level_raw, event.component, event.message) == (
        "1773479521700",
        "ERROR",
        "com.acme.App",
        "Payment failed",
    )


def test_xml_stray_wrapper_closer_is_ignored() -> None:
    event = _parse(XmlParser(), '<e level="info">ok</e>\n</Events>\n</Log>')
    assert (event.message, event.confidence) == ("ok", 1.0)


def test_xml_too_many_stray_closers_is_malformed() -> None:
    event = _parse(XmlParser(), "<e>ok</e></a></b></c></d>")
    assert event.confidence == MALFORMED_CONFIDENCE


def test_xml_repeated_children_are_numbered() -> None:
    event = _parse(XmlParser(), "<e><item>a</item><item>b</item><msg>x</msg></e>")
    assert event.extra == {"item": "a", "item[2]": "b", "element": "e"}


@pytest.mark.parametrize(
    "text", ['<?xml version="1.0" encoding="utf-8"?>', "<Events>", "</Events>", "</a>\n</b>", "  "]
)
def test_xml_structural_records(text: str) -> None:
    assert _parse(XmlParser(), text).is_event is False


@pytest.mark.parametrize(
    "text",
    [
        '<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        "<a><b>unclosed</a>",
        "not xml",
        "<a>1</a><b>2</b>",
    ],
)
def test_xml_malformed(text: str) -> None:
    assert _parse(XmlParser(), text).confidence == MALFORMED_CONFIDENCE


# --- Registry and safety --------------------------------------------------------------


@pytest.mark.parametrize(
    ("format_name", "parser_type"),
    [
        ("json", JsonParser),
        ("prefix_json", PrefixJsonParser),
        ("csv", CsvParser),
        ("logfmt", LogfmtParser),
        ("xml", XmlParser),
    ],
)
def test_create_parser(format_name: str, parser_type: type) -> None:
    parser = create_parser(format_name)
    assert isinstance(parser, parser_type)
    assert parser.format_name == format_name


def test_create_parser_unknown_format() -> None:
    assert create_parser("no_such_format") is None
    assert create_parser("free_text") is None


def test_create_parser_text_formats_come_from_yaml() -> None:
    parser = create_parser("log4j")  # had no parser before step 5b
    assert isinstance(parser, TextFormatParser)
    assert parser.format_name == "log4j"


def test_create_parser_returns_fresh_stateful_parsers() -> None:
    details = {"header": "yes"}
    assert create_parser("csv", details) is not create_parser("csv", details)


def test_safe_parse_turns_a_crash_into_a_malformed_event() -> None:
    class Broken:
        format_name = "broken"

        def parse(self, record: Record) -> ParsedEvent:
            raise ZeroDivisionError("bug")

    event = safe_parse(Broken(), _rec("raw text"))
    assert (event.message, event.confidence) == ("raw text", MALFORMED_CONFIDENCE)
    assert event.error == "broken parser failed: ZeroDivisionError('bug')"


PARSERS = (JsonParser, PrefixJsonParser, LogfmtParser, XmlParser)


@given(text=st.text(max_size=300))
def test_parsers_never_raise(text: str) -> None:
    for parser in (*(cls() for cls in PARSERS), CsvParser(",", has_header=True), CsvParser("\t")):
        event = parser.parse(_rec(text))
        assert 0.0 <= event.confidence <= 1.0
        assert isinstance(event.message, str)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in event.extra.items())


structured_text = st.one_of(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
        lambda children: st.lists(children) | st.dictionaries(st.text(), children),
        max_leaves=20,
    ).map(json.dumps),
    st.lists(st.tuples(st.from_regex(r"[a-z_]{1,8}", fullmatch=True), st.text()), max_size=6).map(
        lambda pairs: " ".join(f"{key}={json.dumps(value)}" for key, value in pairs)
    ),
)


@given(text=structured_text)
def test_parsers_never_raise_on_structured_looking_input(text: str) -> None:
    for parser in (*(cls() for cls in PARSERS), CsvParser(",")):
        assert 0.0 <= parser.parse(_rec(text)).confidence <= 1.0
