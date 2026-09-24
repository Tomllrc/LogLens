"""Tests for byte-level sniffing: containers, binary detection, encodings."""

import bz2
import codecs
import gzip
import io
import tarfile
import zipfile

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loglens.ingest import sniff
from loglens.ingest.sniff import (
    ContainerKind,
    Encoding,
    detect_encoding,
    encoding_from_bom,
    guess_bomless_utf16,
    is_binary,
    sniff_container,
    sniff_text,
)

TEXT = "2026-01-01 10:00:00 INFO user logged in from 10.0.0.1\n" * 20


def _tar_bytes() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        data = TEXT.encode()
        info = tarfile.TarInfo("app.log")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("app.log", TEXT)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(gzip.compress(TEXT.encode()), ContainerKind.GZIP, id="gzip"),
        pytest.param(bz2.compress(TEXT.encode()), ContainerKind.BZIP2, id="bzip2"),
        pytest.param(bz2.compress(b""), ContainerKind.BZIP2, id="bzip2-empty"),
        pytest.param(_zip_bytes(), ContainerKind.ZIP, id="zip"),
        pytest.param(_tar_bytes(), ContainerKind.TAR, id="tar"),
        pytest.param(TEXT.encode(), None, id="text"),
        pytest.param(b"", None, id="empty"),
        pytest.param(b"BZh9 is not a bzip2 stream\n", None, id="bzip2-lookalike"),
        pytest.param(b"PK but not a zip\n", None, id="zip-lookalike"),
    ],
)
def test_sniff_container(data: bytes, expected: ContainerKind | None) -> None:
    assert sniff_container(data) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (codecs.BOM_UTF8 + b"x", "utf-8-sig"),
        (codecs.BOM_UTF16_LE + b"x\x00", "utf-16"),
        (codecs.BOM_UTF16_BE + b"\x00x", "utf-16"),
        (codecs.BOM_UTF32_LE + b"x\x00\x00\x00", "utf-32"),
        (codecs.BOM_UTF32_BE + b"\x00\x00\x00x", "utf-32"),
        (b"no bom", None),
    ],
)
def test_encoding_from_bom(data: bytes, expected: str | None) -> None:
    assert encoding_from_bom(data) == expected


def test_guess_bomless_utf16() -> None:
    assert guess_bomless_utf16(TEXT.encode("utf-16-le")) == "utf-16-le"
    assert guess_bomless_utf16(TEXT.encode("utf-16-be")) == "utf-16-be"
    assert guess_bomless_utf16(TEXT.encode("utf-8")) is None
    assert guess_bomless_utf16(b"a\x00") is None  # too short to judge


def test_is_binary() -> None:
    assert is_binary(b"\x7fELF\x02\x01\x01\x00\x00\x00")
    assert is_binary(bytes(range(1, 32)) * 10)  # control characters, no NUL
    assert not is_binary(TEXT.encode())
    assert not is_binary(b"")
    assert not is_binary(b"\x1b[31mERROR\x1b[0m colored output\n")  # ANSI escapes are text


def test_is_binary_only_inspects_first_8_kb() -> None:
    assert not is_binary(b"a" * 8192 + b"\x00")


@given(st.text(alphabet=st.characters(exclude_categories=("Cc", "Cs"))))
def test_printable_utf8_text_is_never_binary(text: str) -> None:
    assert not is_binary(text.encode("utf-8"))


def test_detect_encoding_utf8_and_ascii() -> None:
    assert detect_encoding(b"plain ascii", complete=True) == Encoding("utf-8", True)
    assert detect_encoding("café ✓".encode(), complete=True) == Encoding("utf-8", True)


def test_detect_encoding_tolerates_char_cut_at_end_of_partial_sample() -> None:
    cut = "café ✓".encode()[:-1]  # last 3-byte character truncated
    assert detect_encoding(cut, complete=False).name == "utf-8"


def test_detect_encoding_latin1_decodes_accents_correctly() -> None:
    text = "Démarrage du serveur à Montréal, requête refusée pour François\n" * 3
    encoding = detect_encoding(text.encode("latin-1"), complete=True)
    assert encoding.confident
    assert text.encode("latin-1").decode(encoding.name) == text


def test_detect_encoding_falls_back_to_unconfident_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    from charset_normalizer import CharsetMatches

    monkeypatch.setattr(sniff, "from_bytes", lambda _: CharsetMatches())
    assert detect_encoding(b"\xff\xfe\xfd", complete=True) == Encoding("utf-8", False)


def test_sniff_text() -> None:
    assert sniff_text(TEXT.encode("utf-16"), complete=True) == Encoding("utf-16", True)
    assert sniff_text(TEXT.encode("utf-16-le"), complete=True) == Encoding("utf-16-le", True)
    assert sniff_text(b"\x00\x01\x02\x03binary", complete=True) is None
    assert sniff_text(b"", complete=True) == Encoding("utf-8", True)
