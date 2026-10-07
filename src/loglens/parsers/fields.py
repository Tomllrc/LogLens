"""Automatic mapping of arbitrary field names onto the unified schema.

Structured logs name the same things differently: ``timestamp``, ``@timestamp``, ``ts``,
``time``; ``level``, ``severity``, ``log.level``; ``msg``, ``message``, ``log``. Each
schema role (timestamp, level, message, component) has candidate names in priority
order. Keys are compared after normalization (lowercase; ``@``, ``_``, ``-`` and spaces
dropped), so ``eventTime``, ``event_time`` and ``EventTime`` are the same name.

Nested keys are flattened with dots (``{"log": {"level": "x"}}`` gives ``log.level``).
A candidate matches a key exactly; a few unambiguous candidates (``level``,
``message``, ``container_name``...) may also match the end of a nested key such as
``kubernetes.container_name``, the shallowest such key winning. A value must also
look right for its role: a component such as ``service=13ms`` is rejected because it
starts with a digit.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from loglens.parsers.generic import GENERIC_TIMESTAMP
from loglens.parsers.values import LEVEL_VALUE, NUMERIC_LEVEL, TIMESTAMP_VALUE

_IGNORED_KEY_CHARS: Final = re.compile(r"[@_\-\s]")


@dataclass(frozen=True)
class MappedFields:
    """Fields assigned to schema roles; ``extra`` holds the rest, rendered as strings."""

    timestamp_raw: str | None
    level_raw: str | None
    component: str | None
    message: str | None
    extra: dict[str, str]


@dataclass(frozen=True)
class FieldRole:
    """How to find one schema role among a record's fields.

    ``names`` are normalized candidate keys in priority order. ``nested`` lists the
    candidates that may also match the end of a nested key. ``accepts`` checks the
    value (already known to be a non-empty string or a number).
    """

    names: tuple[str, ...]
    nested: frozenset[str]
    accepts: Callable[[str], bool]


def _any_value(_value: str) -> bool:
    return True


def _looks_like_timestamp(value: str) -> bool:
    return any(ch.isdigit() for ch in value)


def _looks_like_level(value: str) -> bool:
    stripped = value.strip()
    return bool(LEVEL_VALUE.fullmatch(stripped) or NUMERIC_LEVEL.fullmatch(stripped))


def _looks_like_component(value: str) -> bool:
    stripped = value.strip()
    return 0 < len(stripped) <= 128 and not stripped[0].isdigit()


TIMESTAMP: Final = FieldRole(
    names=(
        "timestamp",
        "time",
        "ts",
        "datetime",
        "date",
        "eventtime",
        "timegenerated",
        "timecreated.systemtime",
        "systemtime",
        "logtime",
        "asctime",
        "createdat",
        "created",
        "timelocal",
    ),
    nested=frozenset({"timestamp", "eventtime", "timecreated.systemtime", "systemtime"}),
    accepts=_looks_like_timestamp,
)
LEVEL: Final = FieldRole(
    names=(
        "level",
        "log.level",
        "loglevel",
        "levelname",
        "severity",
        "severitytext",
        "lvl",
        "sev",
        "priority",
        "at",
    ),
    nested=frozenset({"level", "loglevel", "levelname", "severity", "severitytext"}),
    accepts=_looks_like_level,
)
MESSAGE: Final = FieldRole(
    names=(
        "message",
        "msg",
        "text",
        "event",
        "log",
        "body",
        "shortmessage",
        "fullmessage",
        "description",
        "summary",
        "content",
    ),
    nested=frozenset({"message", "msg"}),
    accepts=_any_value,
)
COMPONENT: Final = FieldRole(
    names=(
        "logger",
        "loggername",
        "logger.name",
        "component",
        "service",
        "service.name",
        "servicename",
        "app",
        "application",
        "module",
        "category",
        "source",
        "program",
        "containername",
        "container",
        "provider.name",
        "tag",
        "class",
        "name",
    ),
    nested=frozenset({"logger", "loggername", "service.name", "containername", "provider.name"}),
    accepts=_looks_like_component,
)

ROLES: Final = (
    ("timestamp", TIMESTAMP),
    ("level", LEVEL),
    ("message", MESSAGE),
    ("component", COMPONENT),
)
"""Roles in resolution order: a key claimed by one role is not offered to later ones."""


def normalize_key(key: str) -> str:
    """Lowercase a key and drop ``@``, ``_``, ``-`` and whitespace (dots are kept)."""
    return _IGNORED_KEY_CHARS.sub("", key).lower()


def flatten(data: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    """Flatten nested mappings into dotted keys, keeping lists and scalars as values.

    On a key collision (``{"a.b": 1, "a": {"b": 2}}``) the first occurrence wins.
    """
    flat: dict[str, object] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping) and value:
            for nested_key, nested_value in flatten(value, path).items():
                flat.setdefault(nested_key, nested_value)
        else:
            flat.setdefault(path, value)
    return flat


def render_value(value: object) -> str:
    """A field value as text: strings unchanged, anything else as JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _candidate_text(value: object) -> str | None:
    """The text of a value usable for a role: a non-empty string or a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, int | float):
        return render_value(value)
    return None


def find_field(
    fields: Mapping[str, object], role: FieldRole, taken: frozenset[str] = frozenset()
) -> str | None:
    """The key that best fills ``role``, or None. Keys in ``taken`` are skipped."""
    usable = [
        (key, normalize_key(key))
        for key, value in fields.items()
        if key not in taken and (text := _candidate_text(value)) is not None and role.accepts(text)
    ]
    for name in role.names:
        for key, normalized in usable:
            if normalized == name:
                return key
        if name in role.nested:
            nested = [key for key, normalized in usable if normalized.endswith("." + name)]
            if nested:
                return min(nested, key=lambda key: key.count("."))
    return None


def map_fields(fields: Mapping[str, object]) -> MappedFields:
    """Assign fields to the timestamp, level, message and component roles by name."""
    taken: set[str] = set()
    found: dict[str, str] = {}
    for role_name, role in ROLES:
        key = find_field(fields, role, frozenset(taken))
        if key is not None:
            taken.add(key)
            found[role_name] = render_value(fields[key]).strip()
    extra = {key: render_value(value) for key, value in fields.items() if key not in taken}
    return MappedFields(
        timestamp_raw=found.get("timestamp"),
        level_raw=found.get("level"),
        component=found.get("component"),
        message=found.get("message"),
        extra=extra,
    )


def map_by_value(fields: Mapping[str, str]) -> MappedFields:
    """Assign roles from what values look like, for columns without meaningful names.

    The first timestamp-looking value (any layout the generic parser knows) is the
    timestamp, the first level word the level, and the remaining value with the most
    words (at least two; the last column wins ties) the message. No component is guessed.
    """
    timestamp = next((k for k, v in fields.items() if _is_timestamp(v)), None)
    level = next(
        (k for k, v in fields.items() if k != timestamp and LEVEL_VALUE.fullmatch(v.strip())),
        None,
    )
    wordy = [k for k, v in fields.items() if k not in (timestamp, level) and len(v.split()) >= 2]
    message = max(reversed(wordy), key=lambda k: len(fields[k].split()), default=None)
    taken = {timestamp, level, message}
    return MappedFields(
        timestamp_raw=fields[timestamp].strip() if timestamp else None,
        level_raw=fields[level].strip() if level else None,
        component=None,
        message=fields[message].strip() if message else None,
        extra={k: v for k, v in fields.items() if k not in taken},
    )


def _is_timestamp(value: str) -> bool:
    """A whole value in any timestamp layout the generic parser knows."""
    text = value.strip()
    return bool(TIMESTAMP_VALUE.fullmatch(text) or GENERIC_TIMESTAMP.fullmatch(text))


def describe_fields(fields: Mapping[str, str]) -> str:
    """A readable one-line message from fields: ``key=value`` pairs, quoted when needed."""
    return " ".join(f"{key}={_quote(value)}" for key, value in fields.items())


def _quote(value: str) -> str:
    if value and not any(ch.isspace() or ch in '"=' for ch in value):
        return value
    return json.dumps(value, ensure_ascii=False)
