"""End-to-end tests for iter_lines: fixtures, folders, archives, stdin, filters, bad input."""

import bz2
import gzip
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from loglens.ingest import FileIssue, IngestOptions, IngestReport, Line, iter_lines, reader
from loglens.ingest.sniff import Encoding

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"

UTF8_LINES = [
    "2026-01-01T10:00:00Z INFO café service started ✓",
    "2026-01-01T10:00:01Z WARN naïve retry \u2013 attempt 2",
    "2026-01-01T10:00:02Z ERROR 日本語 request failed",
]
LATIN1_LINES = [
    "2026-01-01 10:00:00 INFO Démarrage du serveur à Montréal",
    "2026-01-01 10:00:01 WARN Connexion refusée pour l'utilisateur François",
    "2026-01-01 10:00:02 ERROR Échec de la requête : délai dépassé",
]
CRLF_LINES = [
    "2026-01-01 10:00:00 INFO first",
    "2026-01-01 10:00:01 INFO second",
    "",
    "2026-01-01 10:00:02 INFO third",
]


def _read(
    inputs: str | Path | list[str | Path], **options: object
) -> tuple[list[Line], IngestReport]:
    report = IngestReport()
    lines = list(iter_lines(inputs, options=IngestOptions(**options), report=report))  # type: ignore[arg-type]
    return lines, report


def _texts(lines: list[Line]) -> list[str]:
    return [line.text for line in lines]


def _src(path: Path) -> str:
    return path.as_posix()


# --- Fixtures --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("utf8.log", UTF8_LINES),
        ("latin1.log", LATIN1_LINES),
        ("utf16_bom.log", UTF8_LINES),
        ("crlf.log", CRLF_LINES),
        ("app.log.gz", ["gz line 1", "gz line 2"]),
    ],
)
def test_fixture_lines(fixture: str, expected: list[str]) -> None:
    path = FIXTURES / fixture
    lines, report = _read(path)
    assert lines == [Line(_src(path), i, text) for i, text in enumerate(expected, start=1)]
    assert report.files_read == [_src(path)]
    assert report.files_skipped == []
    assert report.warnings == []


def test_fixture_encodings_reported() -> None:
    names = ["utf8.log", "latin1.log", "utf16_bom.log", "crlf.log"]
    _, report = _read([FIXTURES / n for n in names])
    encodings = {Path(k).name: v for k, v in report.encodings.items()}
    assert encodings["utf8.log"] == "utf-8"
    assert encodings["latin1.log"] == "cp1252"
    assert encodings["utf16_bom.log"] == "utf-16"
    assert encodings["crlf.log"] == "utf-8"
    assert report.encoding_counts["utf-8"] == 2


def test_nested_zip_sources_and_lines() -> None:
    path = FIXTURES / "nested.zip"
    lines, report = _read(path)
    zip_src = _src(path)
    assert lines == [
        Line(f"{zip_src}!/inner/app.log", 1, "zip line 1"),
        Line(f"{zip_src}!/inner/app.log", 2, "zip line 2"),
        Line(f"{zip_src}!/inner/deeper.zip!/deep.log", 1, "deep line 1"),
    ]
    assert report.files_read == [
        f"{zip_src}!/inner/app.log",
        f"{zip_src}!/inner/deeper.zip!/deep.log",
    ]


def test_binary_fixture_is_skipped() -> None:
    path = FIXTURES / "binary.bin"
    lines, report = _read(path)
    assert lines == []
    assert report.files_read == []
    assert report.files_skipped == [FileIssue(_src(path), "binary content")]


def test_empty_fixture_is_read_with_no_lines() -> None:
    path = FIXTURES / "empty.log"
    lines, report = _read(path)
    assert lines == []
    assert report.files_read == [_src(path)]
    assert report.bytes_read == 0


def test_whole_fixture_folder() -> None:
    _, report = _read(FIXTURES)
    read = {s.removeprefix(_src(FIXTURES) + "/") for s in report.files_read}
    assert {
        "utf8.log",
        "latin1.log",
        "utf16_bom.log",
        "crlf.log",
        "empty.log",
        "app.log.gz",
        "nested.zip!/inner/app.log",
        "nested.zip!/inner/deeper.zip!/deep.log",
    } <= read
    assert FileIssue(_src(FIXTURES / "binary.bin"), "binary content") in report.files_skipped


def test_bytes_read_counts_decompressed_text() -> None:
    _, report = _read([FIXTURES / "utf8.log", FIXTURES / "app.log.gz"])
    expected = (FIXTURES / "utf8.log").stat().st_size + gzip.decompress(
        (FIXTURES / "app.log.gz").read_bytes()
    ).__len__()
    assert report.bytes_read == expected


# --- Folders, archives, stdin ------------------------------------------------------------


@pytest.fixture
def log_tree(tmp_path: Path) -> Path:
    """logs/ with nested folders, an archive inside a folder, and a non-log file."""
    root = tmp_path / "logs"
    (root / "b" / "deep").mkdir(parents=True)
    (root / "a.log").write_text("a1\na2\n", encoding="utf-8")
    (root / "b" / "b.log").write_text("b1\n", encoding="utf-8")
    (root / "b" / "deep" / "c.txt").write_text("c1\n", encoding="utf-8")
    (root / "b" / "old.log.gz").write_bytes(gzip.compress(b"gz1\n"))
    return root


def test_folder_is_read_recursively_in_stable_order(log_tree: Path) -> None:
    lines, report = _read(log_tree)
    root = _src(log_tree)
    assert [(line.source_file.removeprefix(root), line.text) for line in lines] == [
        ("/a.log", "a1"),
        ("/a.log", "a2"),
        ("/b/b.log", "b1"),
        ("/b/old.log.gz", "gz1"),
        ("/b/deep/c.txt", "c1"),
    ]
    assert report.files_skipped == []


def test_include_patterns_filter_discovered_files(log_tree: Path) -> None:
    lines, report = _read(log_tree, include=("*.log",))
    assert _texts(lines) == ["a1", "a2", "b1"]
    reasons = {Path(i.source_file).name: i.reason for i in report.files_skipped}
    assert reasons == {
        "c.txt": "not matched by include patterns",
        "old.log.gz": "not matched by include patterns",
    }


def test_exclude_prunes_directories(log_tree: Path) -> None:
    lines, report = _read(log_tree, exclude=("deep",))
    assert _texts(lines) == ["a1", "a2", "b1", "gz1"]  # c.txt, under deep/, is gone
    assert report.files_skipped == [FileIssue(_src(log_tree / "b" / "deep"), "excluded by pattern")]


def test_explicit_files_ignore_filters(log_tree: Path) -> None:
    lines, _ = _read(log_tree / "b" / "deep" / "c.txt", include=("*.log",), exclude=("*.txt",))
    assert _texts(lines) == ["c1"]


def test_filters_apply_to_archive_members(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app.log", "keep\n")
        zf.writestr("debug.trace", "drop\n")
    lines, report = _read(archive, include=("*.log",))
    assert _texts(lines) == ["keep"]
    assert report.files_skipped == [
        FileIssue(f"{_src(archive)}!/debug.trace", "not matched by include patterns")
    ]


def _tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "compress"),
    [
        ("bundle.tar", lambda data: data),
        ("bundle.tar.gz", gzip.compress),
        ("bundle.tar.bz2", bz2.compress),
    ],
)
def test_tar_archives(tmp_path: Path, filename: str, compress: object) -> None:
    archive = tmp_path / filename
    archive.write_bytes(compress(_tar({"x/one.log": b"one\n", "two.log": b"two\n"})))  # type: ignore[operator]
    lines, report = _read(archive)
    assert [(line.source_file, line.text) for line in lines] == [
        (f"{_src(archive)}!/x/one.log", "one"),
        (f"{_src(archive)}!/two.log", "two"),
    ]
    assert report.warnings == []


def test_bz2_file(tmp_path: Path) -> None:
    path = tmp_path / "app.log.bz2"
    path.write_bytes(bz2.compress(b"bz1\nbz2\n"))
    lines, _ = _read(path)
    assert _texts(lines) == ["bz1", "bz2"]


def test_archive_detected_by_content_not_extension(tmp_path: Path) -> None:
    path = tmp_path / "rotated.1"
    path.write_bytes(gzip.compress(b"still gzip\n"))
    assert _texts(_read(path)[0]) == ["still gzip"]


def test_zip_inside_tar_inside_gzip(tmp_path: Path) -> None:
    inner_zip = io.BytesIO()
    with zipfile.ZipFile(inner_zip, "w") as zf:
        zf.writestr("z.log", "zipped\n")
    path = tmp_path / "mixed.tgz"
    path.write_bytes(gzip.compress(_tar({"in.zip": inner_zip.getvalue()})))
    lines, _ = _read(path)
    assert lines == [Line(f"{_src(path)}!/in.zip!/z.log", 1, "zipped")]


def test_archive_depth_limit() -> None:
    path = FIXTURES / "nested.zip"
    lines, report = _read(path, max_archive_depth=1)
    assert _texts(lines) == ["zip line 1", "zip line 2"]
    assert report.files_skipped == [
        FileIssue(f"{_src(path)}!/inner/deeper.zip", "zip nested deeper than 1 levels")
    ]


def test_stdin() -> None:
    report = IngestReport()
    lines = list(iter_lines("-", report=report, stdin=io.BytesIO(b"s1\r\ns2")))
    assert lines == [Line("<stdin>", 1, "s1"), Line("<stdin>", 2, "s2")]
    assert report.files_read == ["<stdin>"]


def test_stdin_compressed() -> None:
    lines = list(iter_lines(["-"], stdin=io.BytesIO(gzip.compress(b"zipped stdin\n"))))
    assert lines == [Line("<stdin>", 1, "zipped stdin")]


def test_stdin_zip_is_spooled(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("m.log", "member\n")
    lines = list(iter_lines("-", stdin=io.BytesIO(buffer.getvalue())))
    assert lines == [Line("<stdin>!/m.log", 1, "member")]


def test_multiple_inputs_in_order(tmp_path: Path) -> None:
    first, second = tmp_path / "1.log", tmp_path / "2.log"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    lines, _ = _read([second, first])
    assert _texts(lines) == ["two", "one"]


def test_utf16_without_bom(tmp_path: Path) -> None:
    path = tmp_path / "windows.log"
    path.write_bytes("2026-01-01 INFO no bom here\r\n".encode("utf-16-le"))
    lines, report = _read(path)
    assert _texts(lines) == ["2026-01-01 INFO no bom here"]
    assert report.encodings[_src(path)] == "utf-16-le"


# --- Bad input never crashes ----------------------------------------------------------------


def test_missing_path_is_skipped(tmp_path: Path) -> None:
    missing = tmp_path / "nope.log"
    lines, report = _read(missing)
    assert lines == []
    assert report.files_skipped == [FileIssue(_src(missing), "not found")]


def test_corrupt_gzip_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "broken.gz"
    path.write_bytes(b"\x1f\x8b\x08" + b"definitely not deflate data" * 10)
    lines, report = _read(path)
    assert lines == []
    assert [i.source_file for i in report.files_skipped] == [_src(path)]


def test_truncated_gzip_keeps_lines_read_so_far(tmp_path: Path) -> None:
    data = gzip.compress(b"".join(b"line %d\n" % i for i in range(100_000)))
    path = tmp_path / "truncated.gz"
    path.write_bytes(data[: len(data) // 2])
    lines, report = _read(path)
    assert len(lines) > 1000
    assert _texts(lines[:2]) == ["line 0", "line 1"]
    assert len(report.warnings) == 1
    assert report.warnings[0].source_file == _src(path)
    assert "rest of file skipped" in report.warnings[0].reason


def test_corrupt_zip_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
    lines, report = _read(path)
    assert lines == []
    assert report.files_skipped[0].reason.startswith("corrupt zip archive")


def test_truncated_tar_is_reported(tmp_path: Path) -> None:
    data = _tar({"a.log": b"a\n", "b.log": b"b" * 5000 + b"\n"})
    path = tmp_path / "cut.tar"
    path.write_bytes(data[:2048])
    lines, report = _read(path)
    assert _texts(lines)[:1] == ["a"]
    assert report.warnings  # truncation is reported, not raised


def test_high_bytes_soup_is_decoded_without_crashing(tmp_path: Path) -> None:
    path = tmp_path / "garbled.log"
    path.write_bytes(bytes(range(0x80, 0x100)) * 4 + b"\n")
    lines, report = _read(path)
    assert len(lines) == 1
    assert report.files_read == [_src(path)]


def test_unconfident_encoding_is_warned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "sniff_text", lambda *_, **__: Encoding("utf-8", False))
    path = tmp_path / "a.log"
    path.write_bytes(b"x\n")
    lines, report = _read(path)
    assert _texts(lines) == ["x"]
    assert [w.reason for w in report.warnings] == [
        "encoding not detected; decoded as UTF-8 with replacement characters"
    ]


def test_unopenable_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "locked.log"
    path.write_text("x\n", encoding="utf-8")

    def deny(*_: object, **__: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", deny)
    lines, report = _read(path)
    assert lines == []
    assert report.files_skipped == [FileIssue(_src(path), "cannot open: Permission denied")]


def test_unopenable_zip_member_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = tmp_path / "secret.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.log", "a\n")

    def encrypted(*_: object, **__: object) -> None:
        raise RuntimeError("File is encrypted, password required for extraction")

    monkeypatch.setattr(zipfile.ZipFile, "open", encrypted)
    lines, report = _read(archive)
    assert lines == []
    assert report.files_skipped[0].source_file == f"{_src(archive)}!/a.log"
    assert "encrypted" in report.files_skipped[0].reason


def test_exclude_patterns_on_files_and_archive_members(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "keep.log").write_text("keep\n", encoding="utf-8")
    (root / "noise.log").write_text("noise\n", encoding="utf-8")
    with zipfile.ZipFile(root / "z.zip", "w") as zf:
        zf.writestr("keep.log", "zkeep\n")
        zf.writestr("noise.log", "znoise\n")
    (root / "t.tar").write_bytes(_tar({"keep.log": b"tkeep\n", "noise.log": b"tnoise\n"}))

    lines, report = _read(root, exclude=("noise.log",))

    assert _texts(lines) == ["keep", "tkeep", "zkeep"]
    assert sorted(i.source_file.removeprefix(_src(root)) for i in report.files_skipped) == [
        "/noise.log",
        "/t.tar!/noise.log",
        "/z.zip!/noise.log",
    ]


def test_tar_directories_and_links_are_ignored(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directory = tarfile.TarInfo("dir")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        link = tarfile.TarInfo("dir/link.log")
        link.type = tarfile.SYMTYPE
        link.linkname = "real.log"
        archive.addfile(link)
        data = b"real\n"
        real = tarfile.TarInfo("dir/real.log")
        real.size = len(data)
        archive.addfile(real, io.BytesIO(data))
    path = tmp_path / "t.tar"
    path.write_bytes(buffer.getvalue())

    lines, report = _read(path)

    assert lines == [Line(f"{_src(path)}!/dir/real.log", 1, "real")]
    assert report.files_skipped == []


def test_report_is_filled_progressively(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("x\ny\n", encoding="utf-8")
    report = IngestReport()
    lines = iter_lines(path, report=report)
    assert report.files_read == []
    next(lines)
    assert report.files_read == [_src(path)]


def test_max_lines_per_file_stops_each_file_early(tmp_path: Path) -> None:
    first, second = tmp_path / "a.log", tmp_path / "b.log"
    first.write_text("".join(f"a{i}\n" for i in range(200_000)), encoding="utf-8")  # ~1.5 MB
    second.write_text("b0\nb1\n", encoding="utf-8")
    archive = tmp_path / "c.tar"
    archive.write_bytes(_tar({"x.log": b"x0\nx1\nx2\n", "y.log": b"y0\ny1\ny2\n"}))

    lines, report = _read([first, second, archive], max_lines_per_file=2)

    assert _texts(lines) == ["a0", "a1", "b0", "b1", "x0", "x1", "y0", "y1"]
    assert report.bytes_read < first.stat().st_size / 10  # the rest of a.log was never read
    assert report.warnings == []


def test_invalid_options_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_line_chars"):
        IngestOptions(max_line_chars=0)
    with pytest.raises(ValueError, match="max_lines_per_file"):
        IngestOptions(max_lines_per_file=0)
