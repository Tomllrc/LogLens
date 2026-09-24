"""Declarative text formats: one YAML file per format, in ``loglens/parsers/patterns/``.

Adding a format never requires Python code: drop a YAML file next to the others.
Schema (keys other than these are rejected, to catch typos):

``name`` (required)
    Unique ``snake_case`` name, equal to the file name without ``.yaml``.
``description`` (required)
    What the format is and where it comes from.
``regex`` (required)
    One pattern or a list of alternatives, tried in order and matched at the start of
    a record. Named groups ``timestamp``, ``level``, ``component`` and ``message`` fill
    the schema; any other named group goes to ``extra``. Without a ``message`` group,
    the text after the match is the message. Text after the match on following lines
    (a stack trace) is appended to the message.
``flags``
    Regex flags: ``IGNORECASE``, ``DOTALL``, ``MULTILINE``, ``VERBOSE``.
``priority``
    Lower is more specific. On equal detection scores, the lower priority wins
    (default 100).
``timestamp_format``
    One hint or a list: ``strptime`` patterns, or ``iso8601``, ``epoch``, ``epoch_ms``,
    ``epoch_us``. Every sample line's timestamp must parse with one of them.
``level``
    ``group`` (default ``level``), optional ``transform`` (``syslog_priority``: PRI to
    severity number; ``http_status``: ``404`` to ``4xx``), ``map`` from raw values
    (case-insensitive) to normalized levels, optional ``default``.
``null_values``
    Group values meaning "absent" (such as ``-``); they are dropped.
``unquote``
    Groups whose value may be wrapped in double quotes (``""`` inside meaning ``"``).
``record_start``
    For multi-line records: a regex matching the first line of each record. The
    record splitter then uses it instead of guessing.
``ignore``
    Regexes for records that hold no event, such as a CSV header row. Matching records
    become non-events and do not count against the format when detecting it.
``extra_fields``
    ``regex`` with ``key`` and ``value`` groups, applied repeatedly to the whole record
    to collect fields in any order (``exclude`` lists keys to ignore). Quotes around
    values are removed.
``sample_lines`` (required)
    Real examples. Each is a string, or ``{line: ..., expect: {field: value}}`` where
    fields are ``timestamp``, ``level`` (normalized), ``level_raw``, ``component``,
    ``message`` or an extra field. Samples must match, and they run as tests.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

import yaml

from loglens.parsers.base import malformed_event, structural_event
from loglens.parsers.fields import describe_fields
from loglens.parsers.models import ParsedEvent
from loglens.parsers.timestamps import is_valid_hint
from loglens.schema.levels import LEVELS
from loglens.split.models import Record

DEFAULT_PRIORITY: Final = 100
FLAGS: Final = {
    "IGNORECASE": re.IGNORECASE,
    "DOTALL": re.DOTALL,
    "MULTILINE": re.MULTILINE,
    "VERBOSE": re.VERBOSE,
}


def _syslog_severity(raw: str) -> str | None:
    return str(int(raw) % 8) if raw.isdigit() else None


def _http_status_class(raw: str) -> str | None:
    return f"{raw[0]}xx" if len(raw) == 3 and raw.isdigit() else None


LEVEL_TRANSFORMS: Final[Mapping[str, Callable[[str], str | None]]] = {
    "syslog_priority": _syslog_severity,
    "http_status": _http_status_class,
}

_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")
_KEYS: Final = frozenset(
    {
        "name",
        "description",
        "regex",
        "flags",
        "priority",
        "timestamp_format",
        "level",
        "null_values",
        "unquote",
        "record_start",
        "ignore",
        "extra_fields",
        "sample_lines",
    }
)


class FormatDefinitionError(ValueError):
    """A format definition file is invalid; the message names the file."""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source


@dataclass(frozen=True)
class LevelRule:
    """How to turn a group's raw value into a normalized level."""

    group: str
    mapping: Mapping[str, str]
    transform: str | None = None
    default: str | None = None

    def normalize(self, raw: str | None) -> str | None:
        """The normalized level for ``raw``, or the default when it is not mapped."""
        if raw is None:
            return None
        key: str | None = raw.strip()
        if self.transform is not None and key is not None:
            key = LEVEL_TRANSFORMS[self.transform](key)
        if key is None:
            return self.default
        return self.mapping.get(key.lower(), self.default)


@dataclass(frozen=True)
class ExtraFields:
    """Key/value pairs collected anywhere in a record."""

    pattern: re.Pattern[str]
    exclude: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Sample:
    """A sample line from a format definition and the fields it should yield."""

    line: str
    expect: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TextFormat:
    """A declaratively defined text format (see the module docstring for the schema)."""

    name: str
    description: str
    regexes: tuple[re.Pattern[str], ...]
    samples: tuple[Sample, ...]
    priority: int = DEFAULT_PRIORITY
    timestamp_formats: tuple[str, ...] = ()
    level: LevelRule | None = None
    null_values: frozenset[str] = frozenset()
    unquote: frozenset[str] = frozenset()
    record_start: re.Pattern[str] | None = None
    ignore: tuple[re.Pattern[str], ...] = ()
    extra_fields: ExtraFields | None = None
    source: str = ""

    def match(self, text: str) -> re.Match[str] | None:
        """The first alternative matching the start of ``text``, or None."""
        for regex in self.regexes:
            found = regex.match(text)
            if found is not None:
                return found
        return None

    def is_ignored(self, text: str) -> bool:
        """True for a record that holds no event (see ``ignore``)."""
        return any(regex.match(text) for regex in self.ignore)

    def score(self, sample: Sequence[str]) -> float:
        """Detection score: share of non-blank, non-ignored records matching this format."""
        records = [text for text in sample if text.strip() and not self.is_ignored(text)]
        if not records:
            return 0.0
        return sum(1 for text in records if self.match(text) is not None) / len(records)


class TextFormatParser:
    """Parses records of one :class:`TextFormat` into events."""

    def __init__(self, text_format: TextFormat) -> None:
        self.text_format = text_format
        self.format_name = text_format.name

    def parse(self, record: Record) -> ParsedEvent:
        """Match the record and map its named groups to the schema."""
        fmt = self.text_format
        text = record.text
        if fmt.ignore and fmt.is_ignored(text):
            return structural_event(text)
        found = fmt.match(text)
        if found is None:
            return malformed_event(text, f"does not match the {fmt.name} format")

        nulls = fmt.null_values
        fields = {k: v for k, v in found.groupdict().items() if v is not None and v not in nulls}
        for key in fmt.unquote.intersection(fields):
            fields[key] = unquote(fields[key])

        message = fields.pop("message", None)
        rest = text[found.end() :]
        if rest.strip():
            message = rest.strip("\r\n") if message is None else message + rest
        timestamp = fields.pop("timestamp", None)
        component = fields.pop("component", None)
        rule = fmt.level
        if rule is None or rule.group == "level":
            level_raw = fields.pop("level", None)
        else:
            level_raw = fields.get(rule.group)
        level = rule.normalize(level_raw) if rule is not None else None

        if fmt.extra_fields is not None:
            for pair in fmt.extra_fields.pattern.finditer(text):
                key = pair.group("key")
                if key not in fmt.extra_fields.exclude and key not in fields:
                    fields[key] = unquote(pair.group("value"))

        message = (message or "").strip().lstrip("﻿").strip() or describe_fields(fields)
        return ParsedEvent(
            message=message,
            timestamp_raw=timestamp,
            level_raw=level_raw,
            level=level,
            component=component,
            extra=fields,
        )


def unquote(value: str) -> str:
    """Remove surrounding double quotes, turning ``""`` or ``\\"`` inside into ``"``."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('""', '"').replace('\\"', '"')
    return value


# --- Loading ------------------------------------------------------------------------


def build_text_format(data: object, *, source: str = "<definition>") -> TextFormat:
    """Validate a parsed YAML definition and build the format. Raises FormatDefinitionError."""

    def fail(message: str) -> FormatDefinitionError:
        return FormatDefinitionError(source, message)

    if not isinstance(data, dict):
        raise fail("a format definition must be a mapping")
    unknown = sorted(set(map(str, data)) - _KEYS)
    if unknown:
        raise fail(f"unknown keys: {', '.join(unknown)}")

    name = _text(data, "name", fail)
    if not _NAME.fullmatch(name):
        raise fail(f"name {name!r} must be snake_case")
    priority = data.get("priority", DEFAULT_PRIORITY)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise fail("priority must be an integer")

    flags = 0
    for flag in _texts(data, "flags", fail, required=False):
        if flag not in FLAGS:
            raise fail(f"unknown flag {flag!r}; use {', '.join(FLAGS)}")
        flags |= FLAGS[flag]
    regexes = tuple(_compile(p, flags, "regex", fail) for p in _texts(data, "regex", fail))
    groups = {group for regex in regexes for group in regex.groupindex}
    if not groups:
        raise fail("regex must have named groups")

    hints = tuple(_texts(data, "timestamp_format", fail, required=False))
    for hint in hints:
        if not is_valid_hint(hint):
            raise fail(f"timestamp_format {hint!r} is neither a keyword nor a strptime pattern")
    unquoted = frozenset(_texts(data, "unquote", fail, required=False))
    if unquoted - groups:
        raise fail(f"unquote names unknown groups: {', '.join(sorted(unquoted - groups))}")
    record_start = data.get("record_start")
    if record_start is not None and not isinstance(record_start, str):
        raise fail("record_start must be a regex string")

    text_format = TextFormat(
        name=name,
        description=_text(data, "description", fail),
        regexes=regexes,
        samples=_samples(data.get("sample_lines"), fail),
        priority=priority,
        timestamp_formats=hints,
        level=_level_rule(data.get("level"), groups, fail),
        null_values=frozenset(_texts(data, "null_values", fail, required=False)),
        unquote=unquoted,
        record_start=None
        if record_start is None
        else _compile(record_start, 0, "record_start", fail),
        ignore=tuple(
            _compile(p, 0, "ignore", fail) for p in _texts(data, "ignore", fail, required=False)
        ),
        extra_fields=_extra_fields(data.get("extra_fields"), fail),
        source=source,
    )
    for sample in text_format.samples:
        if text_format.match(sample.line) is None:
            raise fail(f"sample line does not match the regex: {sample.line!r}")
    return text_format


def load_text_format(path: Traversable | Path) -> TextFormat:
    """Load and validate one YAML format file; its name must match the file name."""
    source = str(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise FormatDefinitionError(source, f"invalid YAML: {error}") from error
    text_format = build_text_format(data, source=source)
    stem = path.name.rsplit(".", 1)[0]
    if text_format.name != stem:
        raise FormatDefinitionError(source, f"name {text_format.name!r} must match the file name")
    return text_format


def load_text_formats(directory: Traversable | Path) -> dict[str, TextFormat]:
    """Load every ``*.yaml`` file of a directory, ordered by (priority, name)."""
    formats: dict[str, TextFormat] = {}
    paths = sorted(
        (p for p in directory.iterdir() if p.name.endswith((".yaml", ".yml"))),
        key=lambda p: p.name,
    )
    for path in paths:
        text_format = load_text_format(path)
        if text_format.name in formats:
            raise FormatDefinitionError(str(path), f"duplicate format name {text_format.name!r}")
        formats[text_format.name] = text_format
    return dict(sorted(formats.items(), key=lambda item: (item[1].priority, item[0])))


# --- Validation helpers -------------------------------------------------------------

_Fail = Callable[[str], FormatDefinitionError]


def _text(data: Mapping[str, object], key: str, fail: _Fail) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise fail(f"{key} is required and must be a non-empty string")
    return value.strip()


def _texts(
    data: Mapping[str, object], key: str, fail: _Fail, *, required: bool = True
) -> list[str]:
    value = data.get(key)
    if value is None and not required:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return list(value)
    raise fail(f"{key} must be a string or a non-empty list of strings")


def _compile(pattern: str, flags: int, key: str, fail: _Fail) -> re.Pattern[str]:
    try:
        return re.compile(pattern, flags)
    except re.error as error:
        raise fail(f"{key} does not compile: {error}") from error


def _scalar_text(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    return str(value)


def _level_rule(value: object, groups: set[str], fail: _Fail) -> LevelRule | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise fail("level must be a mapping")
    unknown = sorted(set(map(str, value)) - {"group", "map", "transform", "default"})
    if unknown:
        raise fail(f"unknown level keys: {', '.join(unknown)}")
    group = value.get("group", "level")
    if group not in groups:
        raise fail(f"level group {group!r} is not a group of the regex")
    transform = value.get("transform")
    if transform is not None and transform not in LEVEL_TRANSFORMS:
        raise fail(f"unknown level transform {transform!r}; use {', '.join(LEVEL_TRANSFORMS)}")
    raw_map = value.get("map")
    if not isinstance(raw_map, dict) or not raw_map:
        raise fail("level.map must be a non-empty mapping")
    mapping: dict[str, str] = {}
    for raw, level in raw_map.items():
        key = _scalar_text(raw)
        if key is None or level not in LEVELS:
            raise fail(f"level.map entry {raw!r}: {level!r} must map to one of {', '.join(LEVELS)}")
        mapping[key.lower()] = level
    default = value.get("default")
    if default is not None and default not in LEVELS:
        raise fail(f"level.default must be one of {', '.join(LEVELS)}")
    return LevelRule(group=group, mapping=mapping, transform=transform, default=default)


def _extra_fields(value: object, fail: _Fail) -> ExtraFields | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("regex"), str):
        raise fail("extra_fields must be a mapping with a regex")
    pattern = _compile(value["regex"], re.MULTILINE, "extra_fields.regex", fail)
    if not {"key", "value"} <= set(pattern.groupindex):
        raise fail("extra_fields.regex needs 'key' and 'value' groups")
    exclude = frozenset(_texts(value, "exclude", fail, required=False))
    return ExtraFields(pattern, exclude)


def _samples(value: object, fail: _Fail) -> tuple[Sample, ...]:
    if not isinstance(value, list) or not value:
        raise fail("sample_lines is required and must be a non-empty list")
    samples: list[Sample] = []
    for item in value:
        if isinstance(item, str):
            samples.append(Sample(item))
            continue
        if not isinstance(item, dict) or not isinstance(item.get("line"), str):
            raise fail("each sample line is a string or a mapping with 'line' and 'expect'")
        expect: dict[str, str] = {}
        for key, expected in (item.get("expect") or {}).items():
            text = _scalar_text(expected)
            if text is None:
                raise fail(f"expected value of {key!r} must be a string or a number")
            expect[str(key)] = text
        samples.append(Sample(item["line"], expect))
    return tuple(samples)


# --- Built-in formats (loaded last: the helpers above must exist) -----------------

BUILTIN_FORMATS_DIR: Final = resources.files("loglens.parsers").joinpath("patterns")

TEXT_FORMATS: Final = load_text_formats(BUILTIN_FORMATS_DIR)
"""The built-in text formats, by name, most specific (lowest priority) first."""


__all__ = [
    "BUILTIN_FORMATS_DIR",
    "TEXT_FORMATS",
    "FormatDefinitionError",
    "LevelRule",
    "Sample",
    "TextFormat",
    "TextFormatParser",
    "build_text_format",
    "load_text_format",
    "load_text_formats",
    "unquote",
]
