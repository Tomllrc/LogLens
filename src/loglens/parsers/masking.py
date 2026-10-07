"""Variable masking: replace variable values in a message with typed placeholders.

``"Connected to 10.0.0.5 in 12ms"`` becomes ``"Connected to <IP> in <DURATION>"`` with
params ``["10.0.0.5", "12ms"]``. Masking runs before template mining, so messages that
differ only by their variables share a template. The rules live in ``masking.yaml``
(or a user file with the same layout) and are combined into one regex: at each position
the first rule that matches wins. No value may start right after a word character.
"""

import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

import yaml

PLACEHOLDER: Final = re.compile(r"<[A-Z][A-Z0-9_]*>|<\*>")
"""A typed placeholder such as ``<IP>``, or Drain's generic ``<*>``."""


class MaskingConfigError(ValueError):
    """A masking configuration file is invalid."""


@dataclass(frozen=True)
class MaskRule:
    """One masking rule: values matching ``pattern`` become ``placeholder``."""

    name: str
    pattern: str
    placeholder: str


@dataclass(frozen=True)
class Masked:
    """A masked message and the values its placeholders replaced, in order."""

    text: str
    params: tuple[str, ...]


class Masker:
    """Applies an ordered list of rules in a single pass."""

    def __init__(self, rules: tuple[MaskRule, ...]) -> None:
        if not rules:
            raise MaskingConfigError("at least one masking rule is required")
        self.rules = rules
        groups = "|".join(f"(?P<r{index}>{rule.pattern})" for index, rule in enumerate(rules))
        try:
            # No value starts in the middle of a word: this shared gate lets the engine skip
            # word-internal positions without trying every rule (about 2x faster).
            self._regex = re.compile(rf"(?<!\w)(?:{groups})")
        except re.error as error:
            raise MaskingConfigError(f"masking rules do not compile: {error}") from error
        self._placeholders = {f"r{index}": rule.placeholder for index, rule in enumerate(rules)}

    def mask(self, text: str) -> Masked:
        """Mask ``text``; params hold the replaced values in order of appearance."""
        params: list[str] = []
        placeholders = self._placeholders

        def replace(match: re.Match[str]) -> str:
            params.append(match.group())
            return placeholders[match.lastgroup or ""]

        return Masked(self._regex.sub(replace, text), tuple(params))


def load_masking_rules(path: Traversable | Path) -> tuple[MaskRule, ...]:
    """Read and validate a masking YAML file (a ``rules`` list of ``name``/``regex``)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise MaskingConfigError(f"{path}: cannot read masking rules: {error}") from error
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list) or not rules:
        raise MaskingConfigError(f"{path}: expected a non-empty 'rules' list")
    parsed: list[MaskRule] = []
    for item in rules:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise MaskingConfigError(f"{path}: each rule needs a 'name' and a 'regex'")
        name, pattern = item["name"], item.get("regex")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or not isinstance(pattern, str):
            raise MaskingConfigError(f"{path}: rule {name!r} needs an UPPER_CASE name and a regex")
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise MaskingConfigError(f"{path}: rule {name} does not compile: {error}") from error
        if compiled.groups:
            pattern = re.sub(r"\((?!\?)", "(?:", pattern)  # capture groups would shift names
        placeholder = item.get("placeholder", f"<{name}>")
        if not isinstance(placeholder, str) or not PLACEHOLDER.fullmatch(placeholder):
            raise MaskingConfigError(f"{path}: rule {name}: placeholder must look like <NAME>")
        parsed.append(MaskRule(name, pattern, placeholder))
    return tuple(parsed)


DEFAULT_MASKING_FILE: Final = resources.files("loglens.parsers").joinpath("masking.yaml")
DEFAULT_RULES: Final = load_masking_rules(DEFAULT_MASKING_FILE)


def default_masker() -> Masker:
    """A masker with the built-in rules."""
    return Masker(DEFAULT_RULES)
