"""Streaming primitives: replaying sniffed bytes and splitting decoded text into lines."""

import io
from collections.abc import Generator
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer


class ByteSource(Protocol):
    """Anything with a binary ``read(size)`` method: files, archive members, stdin."""

    def read(self, size: int = -1, /) -> bytes: ...


class TextLine(NamedTuple):
    """A decoded line (or a piece of an over-long line) with its 1-based number.

    ``continued`` is True for the second and later pieces of a line that was longer
    than the length cap.
    """

    number: int
    text: str
    continued: bool


def read_sample(source: ByteSource, size: int) -> bytes:
    """Read up to ``size`` bytes, fewer only at end of stream."""
    parts: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = source.read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


class ReplayStream(io.RawIOBase):
    """A raw stream that first replays an already-read prefix, then continues from the source.

    This lets us sniff the start of a non-seekable stream (stdin, archive members)
    without loading it whole. ``bytes_read`` counts every byte delivered.
    """

    def __init__(self, prefix: bytes, source: ByteSource) -> None:
        super().__init__()
        self._prefix = prefix
        self._offset = 0
        self._source = source
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: "WriteableBuffer", /) -> int:
        view = memoryview(buffer).cast("B")
        if self._offset < len(self._prefix):
            data = self._prefix[self._offset : self._offset + len(view)]
            self._offset += len(data)
            if self._offset == len(self._prefix):
                self._prefix, self._offset = b"", 0
        elif isinstance(self._source, io.BufferedIOBase):
            data = self._source.read1(len(view))  # don't block waiting for a full buffer
        else:
            data = self._source.read(len(view))
        view[: len(data)] = data
        self.bytes_read += len(data)
        return len(data)


def iter_text_lines(
    raw: io.RawIOBase, encoding: str, *, max_line_chars: int
) -> Generator[TextLine, None, None]:
    """Decode a raw byte stream lazily and yield its lines without terminators.

    ``\\n``, ``\\r\\n`` and ``\\r`` all end a line, even when split across reads.
    Undecodable bytes become U+FFFD. Memory stays bounded by ``max_line_chars``:
    longer lines are yielded in pieces that share the same line number.
    """
    text = io.TextIOWrapper(
        io.BufferedReader(raw), encoding=encoding, errors="replace", newline=None
    )
    with text:
        number = 0
        at_line_start = True
        while piece := text.readline(max_line_chars):
            ended = piece.endswith("\n")
            body = piece[:-1] if ended else piece
            if at_line_start:
                number += 1
                yield TextLine(number, body, continued=False)
            elif body:
                yield TextLine(number, body, continued=True)
            at_line_start = ended
