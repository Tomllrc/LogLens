"""Tests for the JSON, XML, logfmt and CSV detectors."""

import pytest

from loglens.detect import CsvDialect, detect_format, sniff_csv, split_prefix_json
from loglens.detect.structured import (
    CSV_DETECTOR,
    JSON_DETECTOR,
    LOGFMT_DETECTOR,
    PREFIX_JSON_DETECTOR,
    XML_DETECTOR,
    is_json_object,
    is_logfmt,
    is_prefix_json,
    is_xml_element,
)

# --- Text prefix + JSON -----------------------------------------------------------


def test_split_prefix_json() -> None:
    text = '2026-01-01T10:00:00Z INFO payments {"order": 1, "ok": true}'
    assert split_prefix_json(text) == (
        "2026-01-01T10:00:00Z INFO payments ",
        {"order": 1, "ok": True},
    )


def test_split_prefix_json_skips_braces_inside_the_prefix() -> None:
    assert split_prefix_json('[{main}] ctx={a} {"k": "v"}') == ("[{main}] ctx={a} ", {"k": "v"})


def test_split_prefix_json_payload_may_span_lines() -> None:
    text = 'ERROR payload follows:\n{\n  "order": 1,\n  "items": [1, 2]\n}'
    assert split_prefix_json(text) == ("ERROR payload follows:\n", {"order": 1, "items": [1, 2]})


@pytest.mark.parametrize(
    "text",
    [
        '{"plain": "json, no prefix"}',
        '   {"only": "whitespace before"}',
        'INFO {"trailing": "text"} after',
        "INFO [1, 2, 3]",
        'INFO {"broken": }',
        "INFO no json at all",
        "{} {} {} {} {} {bad",
        "",
    ],
)
def test_split_prefix_json_rejects(text: str) -> None:
    assert split_prefix_json(text) is None
    assert not is_prefix_json(text)


def test_split_prefix_json_gives_up_after_a_few_braces() -> None:
    prefix = "{x} " * 5
    assert split_prefix_json(prefix + '{"k": 1}') is None


def test_prefix_json_detector_scores_share() -> None:
    sample = ['INFO a {"n": 1}', 'INFO b {"n": 2}', "INFO c plain", "  "]
    assert PREFIX_JSON_DETECTOR.score(sample) == pytest.approx(2 / 3)


def test_known_text_format_beats_prefix_json_on_a_tie() -> None:
    log4j_with_json = [
        f'2026-01-01 10:00:0{i},123 INFO  [main] com.acme.App - {{"order": {i}}}' for i in range(5)
    ]
    result = detect_format(log4j_with_json)
    assert result.format_name == "log4j"
    assert result.scores_by_format["prefix_json"] == 1.0


def test_prefix_json_beats_plain_json_only_when_there_is_a_prefix() -> None:
    assert detect_format(['{"a": 1}', '{"a": 2}']).format_name == "json"
    assert detect_format(['app {"a": 1}', 'app {"a": 2}']).format_name == "prefix_json"


# --- JSON -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '  {"level": "info", "msg": "x"}  ',
        "{}",
        '{\n  "time": "2026-01-01T10:00:00Z",\n  "nested": {"v": [1, 2]}\n}',
        '{"unicode": "caf\\u00e9 日本"}',
    ],
)
def test_json_object_accepted(text: str) -> None:
    assert is_json_object(text)


@pytest.mark.parametrize(
    "text",
    [
        "[1, 2, 3]",
        '[{"a": 1}]',
        '"just a string"',
        "42",
        "null",
        "true",
        '{"a": 1',
        '{"a": 1} trailing',
        '{"a": 1}{"b": 2}',
        "{not json}",
        '2026-01-01 INFO {"a": 1}',
        "",
        "{" * 100_000,
    ],
    ids=[
        "array",
        "array-of-objects",
        "string",
        "number",
        "null",
        "bool",
        "unterminated",
        "trailing",
        "two-objects",
        "not-json",
        "prefixed",
        "empty",
        "deep-nesting",
    ],
)
def test_json_non_objects_rejected(text: str) -> None:
    assert not is_json_object(text)


def test_json_detector_scores_share_of_objects() -> None:
    sample = ['{"a": 1}', '{"a": 2}', "garbage", '{"a": 3}', "", "   "]
    assert JSON_DETECTOR.score(sample) == 0.75  # blank records are ignored


def test_json_detector_empty_sample() -> None:
    assert JSON_DETECTOR.score([]) == 0.0
    assert JSON_DETECTOR.score(["", "  "]) == 0.0


# --- XML --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '<event level="info">started</event>',
        "<br/>",
        '<Event xmlns="http://example.com/ns"><System><EventID>1</EventID></System></Event>',
        '<?xml version="1.0" encoding="utf-8"?>',
        "</Event>",
        "</Data>\n</EventData>",
        "<Events>\n  <Event>\n    <Data>x</Data>",  # multi-line prefix: wrapper or capped
        "<a>&amp; &lt; &#233;</a>",
    ],
)
def test_xml_element_accepted(text: str) -> None:
    assert is_xml_element(text)


@pytest.mark.parametrize(
    "text",
    [
        "<34>Oct 11 22:14:15 host su: 'su root' failed",
        "<not really xml> just a note",
        "<INFO> server started <ok>",
        "<b>bold</b> and more text",
        "<a><b></a>",
        "<a>1</a><b>2</b>",
        "<a>&undefined;</a>",
        "<html",
        "plain text",
        "",
        '<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        '<!DOCTYPE x SYSTEM "http://example.com/evil.dtd"><x/>',
        '<!doctype x [<!entity a "b">]><x/>',
        "<a>\n  <b>\n</c>",
    ],
    ids=[
        "syslog-pri",
        "angle-prose",
        "single-line-prefix",
        "trailing-text",
        "mismatched",
        "two-roots",
        "undefined-entity",
        "unterminated",
        "plain",
        "empty",
        "entity-decl",
        "external-dtd",
        "lowercase-doctype",
        "multiline-mismatch",
    ],
)
def test_xml_non_elements_rejected(text: str) -> None:
    assert not is_xml_element(text)


def test_xml_billion_laughs_is_rejected_without_expansion() -> None:
    lol = "".join(f'<!ENTITY l{i} "{f"&l{i - 1};" * 10}">' for i in range(1, 10))
    text = f'<!DOCTYPE x [<!ENTITY l0 "lol">{lol}]>\n<x>&l9;</x>'
    assert not is_xml_element(text)


def test_xml_detector_score() -> None:
    assert XML_DETECTOR.score(["<a/>", "<b>x</b>", "nope", "<c/>"]) == 0.75


# --- logfmt -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'ts=2026-01-01T10:00:00Z level=info msg="hello world"',
        "at=info method=GET path=/ host=example.com status=200",
        'level=error msg="quote \\" inside" err=',
        "2026-01-01T10:00:00Z level=info user=bob msg=done",  # one leading non-pair token
        "a=1 b=2",
    ],
)
def test_logfmt_accepted(text: str) -> None:
    assert is_logfmt(text)


@pytest.mark.parametrize(
    "text",
    [
        "the user logged in with id=5 from the office",
        "x=1 y=2 happened",
        "set a=1 and b=2 now",
        "a=b",
        "plain text",
        "",
        "a=1,b=2,c=3",
        "level=info msg=first\nlevel=info msg=second",
        'msg="unterminated quote level=info',
    ],
    ids=[
        "one-pair-in-prose",
        "pairs-then-word",
        "pairs-in-sentence",
        "single-pair",
        "plain",
        "empty",
        "comma-joined",
        "multi-line",
        "unterminated-quote",
    ],
)
def test_logfmt_rejected(text: str) -> None:
    assert not is_logfmt(text)


def test_logfmt_detector_score() -> None:
    sample = ["a=1 b=2", "a=3 b=4", "prose with x=1 inside it", "a=5 b=6"]
    assert LOGFMT_DETECTOR.score(sample) == 0.75


# --- CSV --------------------------------------------------------------------------

CSV_WITH_HEADER = [
    "timestamp,level,component,message",
    '2026-01-01T10:00:00Z,INFO,auth,"user bob logged in, id 1"',
    "2026-01-01T10:00:01Z,WARN,auth,slow login",
    "2026-01-01T10:00:02Z,ERROR,db,timeout",
]


def test_csv_with_header() -> None:
    assert sniff_csv(CSV_WITH_HEADER) == CsvDialect(",", 4, True, 1.0)


def test_csv_without_header() -> None:
    dialect = sniff_csv(CSV_WITH_HEADER[1:])
    assert dialect == CsvDialect(",", 4, False, 1.0)


@pytest.mark.parametrize(("delimiter", "name"), [("\t", "tab"), (";", "semi"), ("|", "pipe")])
def test_other_delimiters(delimiter: str, name: str) -> None:
    rows = [delimiter.join(["2026-01-01", "INFO", f"worker-{i}", f"job {i}"]) for i in range(5)]
    dialect = sniff_csv(rows)
    assert dialect is not None, name
    assert (dialect.delimiter, dialect.columns, dialect.has_header) == (delimiter, 4, False)


def test_header_needs_identifier_like_unique_names() -> None:
    rows = ["a,a,b", "1,2,3", "4,5,6"]
    assert sniff_csv(rows) == CsvDialect(",", 3, False, 1.0)
    rows = ["1st,second,third", "1,2,3", "4,5,6"]
    assert sniff_csv(rows) == CsvDialect(",", 3, False, 1.0)
    rows = ["name,city,team", "bob,paris,red", "alice,rome,blue"]  # no digits anywhere
    assert sniff_csv(rows) == CsvDialect(",", 3, False, 1.0)


def test_inconsistent_rows_lower_the_score() -> None:
    rows = ["a,b,c", "1,2,3", "4,5,6", "7,8"]
    dialect = sniff_csv(rows)
    assert dialect is not None
    assert dialect.score == 0.75


def test_prose_with_commas_is_not_csv() -> None:
    prose = [
        "Connected to the server, checking quota, then copying files.",
        "Hello, world",
        "Backup finished; everything looks fine",
        "User bob asked for a restore, so we restored it, checked it, and closed it.",
        "Nothing to report",
        "First, second, third, fourth, fifth, and sixth items were processed",
    ]
    assert CSV_DETECTOR.score(prose) < 0.6


def test_two_columns_are_not_enough() -> None:
    assert sniff_csv(["a,b", "c,d", "e,f"]) is None


def test_single_record_is_not_enough() -> None:
    assert sniff_csv(["a,b,c"]) is None
    assert sniff_csv(["a,b,c", "", "  "]) is None


def test_multi_line_records_do_not_count() -> None:
    rows = ["a,b,c", "d,e,f\ng,h,i", "j,k,l"]
    dialect = sniff_csv(rows)
    assert dialect is not None
    assert dialect.score == pytest.approx(2 / 3)


def test_quoted_newline_is_one_row() -> None:
    rows = ["a,b,c", 'd,"multi\nline",f']
    assert sniff_csv(rows) == CsvDialect(",", 3, False, 1.0)


def test_csv_errors_are_mismatches() -> None:
    huge = "x" * 200_000  # beyond csv.field_size_limit()
    rows = ["a,b,c", "d,e,f", f"{huge},y,z"]
    dialect = sniff_csv(rows)
    assert dialect is not None
    assert dialect.score == pytest.approx(2 / 3)
    assert sniff_csv([f"{huge};1", f"{huge};2"]) is None


def test_csv_detector_score_zero_without_dialect() -> None:
    assert CSV_DETECTOR.score(["plain", "text"]) == 0.0
