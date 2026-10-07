"""Test-wide safety: no real home folder, no real API key, no .env loading."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Every test gets its own LogLens home, and no Gemini key: CI never calls the API."""
    home = tmp_path_factory.mktemp("loglens-home")
    monkeypatch.setenv("LOGLENS_HOME", str(home))
    monkeypatch.setenv("LOGLENS_NO_DOTENV", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LOGLENS_AI", raising=False)
    monkeypatch.delenv("LOGLENS_LLM_MODEL", raising=False)
    monkeypatch.delenv("LOGLENS_LLM_FALLBACK_MODELS", raising=False)
    return home
