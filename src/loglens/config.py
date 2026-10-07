"""User settings: where LogLens keeps its files, and how (and whether) it uses AI.

Sources, later ones winning: defaults, ``<home>/config.yaml``, environment variables.
The home folder is ``~/.loglens`` unless ``LOGLENS_HOME`` is set. A ``.env`` file in the
current folder (or a parent) is loaded into the environment first, without overriding
variables already set.

The Gemini API key is read **only** from the ``GEMINI_API_KEY`` environment variable
(usually set through ``.env``); it is never written to a file, printed or logged.

``config.yaml`` keys: ``ai`` (true/false), ``model``, ``min_delay_seconds``,
``max_retries``. Environment variables: ``LOGLENS_AI`` (``0``/``false``/``off``
disables AI), ``LOGLENS_LLM_MODEL``, ``LOGLENS_LLM_MIN_DELAY``, ``LOGLENS_LLM_MAX_RETRIES``.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import yaml
from dotenv import find_dotenv, load_dotenv

DEFAULT_MODEL: Final = "gemini-3.8-flash"
"""Newest stable Gemini Flash model; free on the free tier (October 2026 docs)."""
DEFAULT_MIN_DELAY_SECONDS: Final = 4.0
"""Minimum spacing between real API calls, to stay under free-tier per-minute limits."""
DEFAULT_MAX_RETRIES: Final = 5
API_KEY_VARIABLE: Final = "GEMINI_API_KEY"
_FALSE: Final = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """The configuration file or a variable is invalid."""


@dataclass(frozen=True)
class Settings:
    """Resolved settings (never holds the API key)."""

    home: Path
    ai_enabled: bool = True
    model: str = DEFAULT_MODEL
    min_delay_seconds: float = DEFAULT_MIN_DELAY_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES

    @property
    def formats_dir(self) -> Path:
        """User format plugins (YAML), loaded on top of the built-in ones."""
        return self.home / "formats"

    @property
    def cache_dir(self) -> Path:
        """Cached LLM responses, so that re-running a command costs no quota."""
        return self.home / "cache" / "llm"

    @property
    def config_file(self) -> Path:
        return self.home / "config.yaml"

    def without_ai(self) -> "Settings":
        """The same settings with AI disabled (``--no-ai``)."""
        return replace(self, ai_enabled=False)


def load_env_file() -> Path | None:
    """Load the nearest ``.env`` (searching up from the current folder); returns its path.

    Skipped when ``LOGLENS_NO_DOTENV`` is set (the test suite sets it, so a developer's
    real key can never reach a test).
    """
    if os.environ.get("LOGLENS_NO_DOTENV"):
        return None
    path = find_dotenv(usecwd=True)
    if not path:
        return None
    load_dotenv(path, override=False)
    return Path(path)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Settings from defaults, ``config.yaml`` and the environment."""
    env = os.environ if environ is None else environ
    home = Path(env["LOGLENS_HOME"]) if env.get("LOGLENS_HOME") else Path.home() / ".loglens"
    settings = Settings(home=home)
    file_values = _read_config_file(settings.config_file)
    try:
        ai = _bool(env.get("LOGLENS_AI"), _bool(file_values.get("ai"), True))
        model = env.get("LOGLENS_LLM_MODEL") or str(file_values.get("model") or DEFAULT_MODEL)
        delay = float(
            str(
                env.get("LOGLENS_LLM_MIN_DELAY")
                or file_values.get("min_delay_seconds", DEFAULT_MIN_DELAY_SECONDS)
            )
        )
        retries = int(
            str(
                env.get("LOGLENS_LLM_MAX_RETRIES")
                or file_values.get("max_retries", DEFAULT_MAX_RETRIES)
            )
        )
    except (TypeError, ValueError) as error:
        raise ConfigError(f"invalid LogLens setting: {error}") from error
    if delay < 0 or retries < 0:
        raise ConfigError("min_delay_seconds and max_retries must not be negative")
    return replace(
        settings, ai_enabled=ai, model=model, min_delay_seconds=delay, max_retries=retries
    )


def api_key_is_set(environ: Mapping[str, str] | None = None) -> bool:
    """Whether a Gemini API key is available (its value is never exposed)."""
    env = os.environ if environ is None else environ
    return bool(env.get(API_KEY_VARIABLE, "").strip())


def _read_config_file(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"{path}: {error}") from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping of settings")
    return {str(key): value for key, value in data.items()}


def _bool(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSE
