"""Glob filtering of source paths."""

from collections.abc import Iterable
from fnmatch import fnmatchcase


def matches_any(source: str, patterns: Iterable[str]) -> bool:
    """Return True if any glob pattern matches the source's full path or its basename.

    Matching is case-sensitive on every platform. ``*`` also matches ``/``, so
    ``*/nginx/*`` matches any file under an ``nginx`` directory. Inside archives the
    basename is the member's own name: ``*.log`` matches ``logs.zip!/app/server.log``.
    """
    path = source.replace("\\", "/")
    basename = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(path, p) or fnmatchcase(basename, p) for p in patterns)
