"""Glob filtering of source paths."""

from collections.abc import Iterable
from fnmatch import fnmatchcase


def matches_any(source: str, patterns: Iterable[str]) -> bool:
    """Return True if any glob pattern matches the source's full path or its basename.

    Matching is case-sensitive on every platform. ``*`` also matches ``/``, so
    ``*/nginx/*`` matches any file under an ``nginx`` directory. Inside archives the
    basename is the member's own name: ``*.log`` matches ``logs.zip!/app/server.log``.

    A pattern ending with ``/`` matches directories only: ``old/`` matches every source
    under a directory named ``old`` (``logs/old/a.log``, ``bundle.zip!/old/a.log``), but
    not a file named ``old``. A source ending with ``/`` is itself a directory.
    """
    path = source.replace("\\", "/")
    for pattern in patterns:
        if pattern.endswith("/"):
            if _under_matching_directory(path, pattern.rstrip("/")):
                return True
        elif _matches(path.rstrip("/"), pattern):
            return True
    return False


def _matches(path: str, pattern: str) -> bool:
    return fnmatchcase(path, pattern) or fnmatchcase(path.rsplit("/", 1)[-1], pattern)


def _under_matching_directory(path: str, pattern: str) -> bool:
    if not pattern:
        return False
    parts = path.split("/")
    return any(_matches("/".join(parts[:end]), pattern) for end in range(1, len(parts)))
