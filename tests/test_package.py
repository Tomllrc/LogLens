"""Smoke tests for the package itself."""

from importlib.metadata import version

import loglens


def test_version_matches_installed_metadata() -> None:
    assert loglens.__version__ == version("loglens")
