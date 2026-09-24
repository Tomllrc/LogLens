"""Regenerate the ingestion fixtures in tests/fixtures/ingest/.

Run with: uv run python tests/fixtures/make_ingest_fixtures.py
Output is deterministic (fixed timestamps), so re-running produces identical bytes.
"""

import gzip
import io
import zipfile
from pathlib import Path

OUT = Path(__file__).parent / "ingest"
ZIP_DATE = (2026, 1, 1, 0, 0, 0)

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
GZ_LINES = ["gz line 1", "gz line 2"]
ZIP_APP_LINES = ["zip line 1", "zip line 2"]
ZIP_DEEP_LINES = ["deep line 1"]


def _text(lines: list[str], newline: str = "\n") -> str:
    return "".join(line + newline for line in lines)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED if name.endswith("/") else zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "utf8.log").write_bytes(_text(UTF8_LINES).encode("utf-8"))
    (OUT / "latin1.log").write_bytes(_text(LATIN1_LINES).encode("latin-1"))
    (OUT / "utf16_bom.log").write_bytes(_text(UTF8_LINES, "\r\n").encode("utf-16"))
    (OUT / "crlf.log").write_bytes(_text(CRLF_LINES, "\r\n").encode("ascii"))
    (OUT / "empty.log").write_bytes(b"")
    (OUT / "binary.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 8)

    with (
        (OUT / "app.log.gz").open("wb") as raw,
        gzip.GzipFile(filename="app.log", mode="wb", fileobj=raw, mtime=0) as gz,
    ):
        gz.write(_text(GZ_LINES).encode("utf-8"))

    deeper = _zip_bytes({"deep.log": _text(ZIP_DEEP_LINES).encode("utf-8")})
    nested = _zip_bytes(
        {
            "inner/": b"",
            "inner/app.log": _text(ZIP_APP_LINES).encode("utf-8"),
            "inner/deeper.zip": deeper,
        }
    )
    (OUT / "nested.zip").write_bytes(nested)


if __name__ == "__main__":
    main()
