"""Tests for stream replay and lazy line splitting."""

import io

from hypothesis import given
from hypothesis import strategies as st

from loglens.ingest.streams import ReplayStream, TextLine, iter_text_lines, read_sample


class Trickle:
    """A byte source that returns at most ``step`` bytes per read, to hit every boundary."""

    def __init__(self, data: bytes, step: int) -> None:
        self._data = io.BytesIO(data)
        self._step = step

    def read(self, size: int = -1, /) -> bytes:
        limit = self._step if size < 0 else min(size, self._step)
        return self._data.read(limit)


def _lines(
    data: bytes, encoding: str = "utf-8", *, step: int = 1 << 16, cap: int = 1000
) -> list[str]:
    raw = ReplayStream(b"", Trickle(data, step))
    return [line.text for line in iter_text_lines(raw, encoding, max_line_chars=cap)]


def test_read_sample_collects_short_reads() -> None:
    source = Trickle(b"abcdefghij", step=3)
    assert read_sample(source, 7) == b"abcdefg"
    assert read_sample(source, 100) == b"hij"
    assert read_sample(source, 100) == b""


def test_replay_stream_replays_prefix_then_continues() -> None:
    source = io.BytesIO(b"0123456789")
    prefix = read_sample(source, 4)
    replay = ReplayStream(prefix, source)
    assert io.BufferedReader(replay, buffer_size=3).read() == b"0123456789"
    assert replay.bytes_read == 10


def test_line_endings_are_normalized() -> None:
    assert _lines(b"lf\ncrlf\r\ncr\rlast") == ["lf", "crlf", "cr", "last"]


def test_crlf_split_across_reads_is_one_line_break() -> None:
    assert _lines(b"a\r\nb\r\n", step=1) == ["a", "b"]
    assert _lines(b"a\r\r\nb", step=1) == ["a", "", "b"]


def test_blank_lines_and_trailing_newline() -> None:
    assert _lines(b"a\n\nb\n") == ["a", "", "b"]
    assert _lines(b"a\n\n") == ["a", ""]
    assert _lines(b"") == []


def test_invalid_bytes_are_replaced_not_raised() -> None:
    assert _lines(b"ok\n\xff\xfe bad\n") == ["ok", "�� bad"]


def test_other_unicode_separators_do_not_split_lines() -> None:
    text = "a\u2028b\x0bc\x0cd\x1ce\x85f\n"
    assert _lines(text.encode()) == [text[:-1]]


def test_long_lines_are_split_into_numbered_pieces() -> None:
    raw = ReplayStream(b"", io.BytesIO(b"abcdefgh\nxy\n"))
    pieces = list(iter_text_lines(raw, "utf-8", max_line_chars=3))
    assert pieces == [
        TextLine(1, "abc", continued=False),
        TextLine(1, "def", continued=True),
        TextLine(1, "gh", continued=True),
        TextLine(2, "xy", continued=False),
    ]


def test_line_exactly_at_cap_has_no_empty_continuation() -> None:
    raw = ReplayStream(b"", io.BytesIO(b"abc\nd\n"))
    pieces = list(iter_text_lines(raw, "utf-8", max_line_chars=3))
    assert pieces == [TextLine(1, "abc", False), TextLine(2, "d", False)]


def test_utf16_decoding_with_bom() -> None:
    assert _lines("un\r\ndeux\r\n".encode("utf-16"), "utf-16", step=3) == ["un", "deux"]


_line_text = st.text(
    alphabet=st.characters(exclude_categories=("Cs",), exclude_characters="\r\n"), max_size=40
)


@given(
    lines=st.lists(_line_text, min_size=1, max_size=30),
    ending=st.sampled_from(["\n", "\r\n", "\r"]),
    encoding=st.sampled_from(["utf-8", "utf-16", "utf-16-le"]),
    step=st.integers(min_value=1, max_value=17),
)
def test_round_trip_any_endings_encodings_and_chunking(
    lines: list[str], ending: str, encoding: str, step: int
) -> None:
    data = "".join(line + ending for line in lines).encode(encoding)
    assert _lines(data, encoding, step=step) == lines
