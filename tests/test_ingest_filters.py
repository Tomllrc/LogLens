"""Tests for glob filtering of source paths."""

import pytest

from loglens.ingest.filters import matches_any


@pytest.mark.parametrize(
    ("source", "patterns", "expected"),
    [
        ("logs/app.log", ["*.log"], True),
        ("logs/app.txt", ["*.log"], False),
        ("logs/nginx/access.log", ["*/nginx/*"], True),
        ("logs.zip!/inner/server.log", ["*.log"], True),
        ("logs.zip!/inner/server.log", ["*inner/*"], True),
        ("repo/.git", [".git"], True),
        ("C:\\logs\\app.log", ["C:/logs/*"], True),
        ("logs/APP.LOG", ["*.log"], False),  # case-sensitive everywhere
        ("logs/app.log", [], False),
        ("logs/app.log", ["*.txt", "*.log"], True),
        # A trailing "/" makes a pattern match directories only.
        ("logs/old/worker.log", ["old/"], True),
        ("logs/old/deeper/worker.log", ["old/"], True),
        ("logs/old/", ["old/"], True),  # a source ending with "/" is a directory
        ("logs/old", ["old/"], False),  # a file named "old"
        ("logs/older/worker.log", ["old/"], False),
        ("logs/old/worker.log", ["logs/old/"], True),
        ("logs/old/worker.log", ["ol*/"], True),
        ("logs.zip!/old/worker.log", ["old/"], True),
        ("/var/log/app.log", ["/"], False),
        ("logs/old/", ["old"], True),  # patterns without "/" still match directories
    ],
)
def test_matches_any(source: str, patterns: list[str], expected: bool) -> None:
    assert matches_any(source, patterns) is expected
