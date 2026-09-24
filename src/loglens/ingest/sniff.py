"""Byte-level sniffing of a sample: archive formats, binary content, and text encoding."""

import codecs
from enum import StrEnum
from typing import Final, NamedTuple

from charset_normalizer import from_bytes

BINARY_SAMPLE_SIZE: Final = 8 * 1024
"""Only this many leading bytes are inspected to decide whether content is binary."""

_MAX_CONTROL_RATIO: Final = 0.30
_TEXT_CONTROLS: Final = frozenset(b"\t\n\v\f\r\b\x1b")  # \x1b: ANSI colour codes
_CONTROL_BYTES: Final = bytes(b for b in (*range(0x20), 0x7F) if b not in _TEXT_CONTROLS)
_NON_CONTROL_BYTES: Final = bytes(b for b in range(256) if b not in _CONTROL_BYTES)
_SUSPICIOUS_CHARS: Final = frozenset(chr(b) for b in _CONTROL_BYTES) | {"�"}

_BOMS: Final = (
    # UTF-32 LE must be tested before UTF-16 LE: its BOM starts with the same two bytes.
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_TIE_BREAK_PREFERENCE: Final = ("utf_8", "cp1252", "latin_1")
_BZIP2_BLOCK_MAGICS: Final = (b"\x31\x41\x59\x26\x53\x59", b"\x17\x72\x45\x38\x50\x90")


class ContainerKind(StrEnum):
    """Compressed or archive formats that are unpacked transparently."""

    GZIP = "gzip"
    BZIP2 = "bzip2"
    ZIP = "zip"
    TAR = "tar"


class Encoding(NamedTuple):
    """A detected text encoding (a Python codec name)."""

    name: str
    confident: bool


def sniff_container(sample: bytes) -> ContainerKind | None:
    """Identify a compressed or archive format from its magic bytes, if any."""
    if sample.startswith(b"\x1f\x8b\x08"):
        return ContainerKind.GZIP
    if (
        len(sample) >= 10
        and sample.startswith(b"BZh")
        and 0x31 <= sample[3] <= 0x39
        and sample[4:10] in _BZIP2_BLOCK_MAGICS
    ):
        return ContainerKind.BZIP2
    if sample.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return ContainerKind.ZIP
    if sample[257:262] == b"ustar":
        return ContainerKind.TAR
    return None


def encoding_from_bom(sample: bytes) -> str | None:
    """Return the codec implied by a leading byte-order mark; the codec strips the BOM."""
    for bom, codec in _BOMS:
        if sample.startswith(bom):
            return codec
    return None


def guess_bomless_utf16(sample: bytes) -> str | None:
    """Recognise UTF-16 without a BOM from the pattern of zero bytes in mostly-ASCII text."""
    pairs = len(sample) // 2
    if pairs < 4:
        return None
    body = sample[: pairs * 2]
    even_zeros = body[0::2].count(0)
    odd_zeros = body[1::2].count(0)
    if odd_zeros >= 0.6 * pairs and even_zeros <= 0.05 * pairs:
        codec = "utf-16-le"
    elif even_zeros >= 0.6 * pairs and odd_zeros <= 0.05 * pairs:
        codec = "utf-16-be"
    else:
        return None
    decoded = body.decode(codec, errors="replace")
    return codec if _text_control_ratio(decoded) <= _MAX_CONTROL_RATIO else None


def is_binary(sample: bytes) -> bool:
    """Return True if the first 8 KB contain a NUL byte or too many control characters."""
    head = sample[:BINARY_SAMPLE_SIZE]
    if not head:
        return False
    if 0 in head:
        return True
    controls = len(head.translate(None, _NON_CONTROL_BYTES))
    return controls / len(head) > _MAX_CONTROL_RATIO


def detect_encoding(sample: bytes, *, complete: bool) -> Encoding:
    """Detect the encoding of BOM-less, non-UTF-16 text.

    Valid UTF-8 (including plain ASCII) wins outright; otherwise charset-normalizer
    decides. ``complete`` says whether the sample is the whole content; if not, a
    multi-byte character cut off at the end of the sample is not an error.
    Falls back to non-confident UTF-8.
    """
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=complete)
    except UnicodeDecodeError:
        pass
    else:
        return Encoding("utf-8", confident=True)

    matches = from_bytes(sample)
    best = matches.best()
    if best is None:
        return Encoding("utf-8", confident=False)
    # Short samples often tie exactly between several single-byte code pages, and the
    # first of them (e.g. cp1250) is arbitrary. Break exact ties toward the common ones.
    tied = [m for m in matches if (m.chaos, m.coherence) == (best.chaos, best.coherence)]
    chosen = next(
        (
            preferred
            for preferred in _TIE_BREAK_PREFERENCE
            for match in tied
            if preferred in match.could_be_from_charset
        ),
        best.encoding,
    )
    try:
        name = codecs.lookup(chosen).name
    except LookupError:
        return Encoding("utf-8", confident=False)
    return Encoding(name, confident=True)


def sniff_text(sample: bytes, *, complete: bool) -> Encoding | None:
    """Return the encoding of a text sample, or None if the content is binary.

    UTF-16/32 checks run first, since their NUL bytes would otherwise look binary.
    """
    bom_codec = encoding_from_bom(sample)
    if bom_codec is not None:
        return Encoding(bom_codec, confident=True)
    utf16_codec = guess_bomless_utf16(sample[:BINARY_SAMPLE_SIZE])
    if utf16_codec is not None:
        return Encoding(utf16_codec, confident=True)
    if is_binary(sample):
        return None
    return detect_encoding(sample, complete=complete)


def _text_control_ratio(text: str) -> float:
    """Share of characters that are neither printable nor usual in text files."""
    if not text:
        return 0.0
    suspicious = sum(1 for ch in text if ch in _SUSPICIOUS_CHARS)
    return suspicious / len(text)
