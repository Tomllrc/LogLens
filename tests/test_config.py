"""Tests for user settings: home folder, AI switch, model, and the .env file."""

import os
from pathlib import Path

import pytest

from loglens.config import (
    DEFAULT_FALLBACK_MODELS,
    DEFAULT_MODEL,
    ConfigError,
    api_key_is_set,
    load_env_file,
    load_settings,
)


def test_defaults(tmp_path: Path) -> None:
    settings = load_settings({"LOGLENS_HOME": str(tmp_path)})
    assert settings.home == tmp_path
    assert settings.ai_enabled
    assert settings.model == DEFAULT_MODEL
    assert settings.formats_dir == tmp_path / "formats"
    assert settings.cache_dir == tmp_path / "cache" / "llm"
    assert not settings.without_ai().ai_enabled


def test_home_defaults_to_the_user_folder() -> None:
    assert load_settings({}).home == Path.home() / ".loglens"


def test_config_file_then_environment(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "ai: false\nmodel: from-file\nmin_delay_seconds: 1\nmax_retries: 2\n", encoding="utf-8"
    )
    from_file = load_settings({"LOGLENS_HOME": str(tmp_path)})
    assert (from_file.ai_enabled, from_file.model) == (False, "from-file")
    assert (from_file.min_delay_seconds, from_file.max_retries) == (1.0, 2)

    from_env = load_settings(
        {
            "LOGLENS_HOME": str(tmp_path),
            "LOGLENS_AI": "1",
            "LOGLENS_LLM_MODEL": "from-env",
            "LOGLENS_LLM_MIN_DELAY": "0.5",
            "LOGLENS_LLM_MAX_RETRIES": "7",
        }
    )
    assert (from_env.ai_enabled, from_env.model) == (True, "from-env")
    assert (from_env.min_delay_seconds, from_env.max_retries) == (0.5, 7)


@pytest.mark.parametrize("value", ["0", "false", "OFF", "no"])
def test_ai_can_be_switched_off(tmp_path: Path, value: str) -> None:
    assert not load_settings({"LOGLENS_HOME": str(tmp_path), "LOGLENS_AI": value}).ai_enabled


@pytest.mark.parametrize(
    ("content", "env"),
    [
        ("- a list\n", {}),
        ("model: [unclosed\n", {}),
        ("", {"LOGLENS_LLM_MIN_DELAY": "soon"}),
        ("", {"LOGLENS_LLM_MAX_RETRIES": "-1"}),
    ],
)
def test_invalid_settings_are_reported(tmp_path: Path, content: str, env: dict[str, str]) -> None:
    (tmp_path / "config.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings({"LOGLENS_HOME": str(tmp_path), **env})


def test_empty_config_file_is_fine(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    assert load_settings({"LOGLENS_HOME": str(tmp_path)}).ai_enabled


def test_api_key_presence_never_exposes_the_value() -> None:
    assert not api_key_is_set({})
    assert not api_key_is_set({"GEMINI_API_KEY": "  "})
    assert api_key_is_set({"GEMINI_API_KEY": "abc"}) is True


def test_env_file_is_skipped_in_tests() -> None:
    assert load_env_file() is None


def test_env_file_is_loaded_without_overriding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("LOGLENS_TEST_A=from-file\nLOGLENS_TEST_B=file\n", "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOGLENS_NO_DOTENV")
    monkeypatch.setenv("LOGLENS_TEST_B", "env")
    monkeypatch.delenv("LOGLENS_TEST_A", raising=False)
    try:
        assert load_env_file() == tmp_path / ".env"
        assert os.environ["LOGLENS_TEST_A"] == "from-file"
        assert os.environ["LOGLENS_TEST_B"] == "env"
    finally:
        os.environ.pop("LOGLENS_TEST_A", None)


def test_fallback_models(tmp_path: Path) -> None:
    home = {"LOGLENS_HOME": str(tmp_path)}
    assert load_settings(home).fallback_models == DEFAULT_FALLBACK_MODELS
    from_env = load_settings({**home, "LOGLENS_LLM_FALLBACK_MODELS": " x , y ,"})
    assert from_env.fallback_models == ("x", "y")
    assert load_settings({**home, "LOGLENS_LLM_FALLBACK_MODELS": ""}).fallback_models == ()
    assert load_settings({**home, "LOGLENS_LLM_FALLBACK_MODELS": "none"}).fallback_models == ()
    (tmp_path / "config.yaml").write_text("fallback_models: [p, q]\n", encoding="utf-8")
    assert load_settings(home).fallback_models == ("p", "q")
    (tmp_path / "config.yaml").write_text("fallback_models: r\n", encoding="utf-8")
    assert load_settings(home).fallback_models == ("r",)
