"""The AI-taught parser: learn a YAML text format for an unknown log layout.

1. Read the file's records (archive members such as ``logs.zip!/app.log`` work).
2. Pick about 50 diverse samples: records are grouped by line *shape* (digits, letters and
   punctuation pattern) and taken round-robin across shapes, so rare types are included.
3. Ask the model once, with structured output matching the format schema, for a regex
   with named groups, timestamp hints and a level mapping.
4. Validate on up to 10,000 records: the definition must load (regex compiles, samples
   match), then match rate, timestamp parse rate and level mapping rate are measured.
5. Below 95% matching, retry (at most 3 times) sending the failing lines back.
6. Save the best definition as YAML, with metadata (generated_by, model, generated_at,
   match_rate), in the user formats folder; it is then used like any other format.

Nothing is sent before ``confirm`` approves the exact sample lines.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import yaml

from loglens.ai.llm import AIError, LLMClient, LLMRequest, Usage, complete_json
from loglens.parsers.textformats import (
    TEXT_FORMATS,
    FormatDefinitionError,
    TextFormat,
    TextFormatParser,
    build_text_format,
)
from loglens.parsers.timestamps import TimestampNormalizer
from loglens.sampling import load_records, select_samples
from loglens.schema.levels import LEVELS
from loglens.split import Record

TARGET_MATCH_RATE: Final = 0.95
MAX_RETRIES: Final = 3
SAMPLE_CHARS: Final = 600
"""Each sample record is cut to this many characters in the prompt."""
FAILING_LINES_SENT: Final = 15
SAMPLE_LINES_KEPT: Final = 8
"""Matched samples stored in the YAML (they become its tests)."""

LEARNED_FORMAT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "snake_case name of the format"},
        "description": {"type": "string", "description": "one sentence: what produces it"},
        "regex": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Python `re` patterns, alternatives tried in order",
        },
        "timestamp_format": {
            "type": "array",
            "items": {"type": "string"},
            "description": "strptime patterns, or iso8601 / epoch / epoch_ms / epoch_us",
        },
        "level_group": {"type": "string", "description": "named group holding the level"},
        "level_map": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw": {"type": "string"},
                    "level": {"type": "string", "enum": list(LEVELS)},
                },
                "required": ["raw", "level"],
            },
        },
        "record_start": {
            "type": "string",
            "description": "only for multi-line records: regex matching their first line",
        },
    },
    "required": ["name", "description", "regex", "timestamp_format"],
}

SYSTEM_PROMPT: Final = """\
You write log parsers for LogLens. Given sample records of one log format, return a \
format definition:
- `regex`: Python `re` patterns (usually one) matched with re.match at the start of each \
record. Use named groups: `timestamp`, `level`, `component`, `message`, and snake_case \
names for any other field (for example `pid`, `host`, `request_id`). The `message` group \
should take the free text, usually `(?P<message>.*)` at the end. Do not anchor with $.
- Be general: values vary between records, so match the shape (\\d+, \\S+, [^\\]]*), \
never copy a specific value from a sample.
- `timestamp_format`: strptime patterns that parse the text of the `timestamp` group \
exactly (or iso8601, epoch, epoch_ms, epoch_us).
- `level_group` and `level_map`: the group holding the severity and how each raw value \
maps to TRACE, DEBUG, INFO, WARN, ERROR or FATAL (omit when the log has no level).
- `record_start`: only if one record spans several lines.
- `name`: a short snake_case name; `description`: one sentence."""


class LearnCancelled(AIError):
    """The user declined to send the samples."""


@dataclass(frozen=True)
class Validation:
    """How well a candidate definition parses the file."""

    match_rate: float = 0.0
    timestamp_rate: float = 0.0
    level_rate: float | None = None
    records: int = 0
    errors: tuple[str, ...] = ()
    failing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """The definition loads and matches enough records."""
        return not self.errors and self.match_rate >= TARGET_MATCH_RATE


@dataclass
class LearnResult:
    """Outcome of :func:`learn_format`."""

    definition: dict[str, Any] | None
    validation: Validation
    attempts: int
    samples: list[str]
    path: Path | None = None
    usage: Usage = field(default_factory=Usage)

    @property
    def text_format(self) -> TextFormat | None:
        """The learned format, if a valid one was produced."""
        if self.definition is None or self.validation.errors:
            return None
        return build_text_format(self.definition, source="learned")


# --- Prompting -------------------------------------------------------------------------------


def build_request(
    samples: Sequence[str],
    *,
    previous: dict[str, Any] | None = None,
    validation: Validation | None = None,
) -> LLMRequest:
    """The structured-output request: samples, and on a retry what went wrong."""
    lines = [f"Sample records ({len(samples)}), one per block:"]
    for index, sample in enumerate(samples, start=1):
        lines.append(f"--- record {index}\n{sample[:SAMPLE_CHARS]}")
    if previous is not None and validation is not None:
        lines.append(
            "\nYour previous definition was:\n" + yaml.safe_dump(previous, sort_keys=False)
        )
        if validation.errors:
            lines.append("It could not be used:\n" + "\n".join(f"- {e}" for e in validation.errors))
        lines.append(f"It matched {validation.match_rate:.0%} of {validation.records} records.")
        if validation.failing:
            lines.append("Records it did not match:")
            lines += [f"--- failing\n{text[:SAMPLE_CHARS]}" for text in validation.failing]
        lines.append("Return a corrected definition that matches all of them.")
    return LLMRequest.from_prompt(
        "\n".join(lines),
        system=SYSTEM_PROMPT,
        response_schema=LEARNED_FORMAT_SCHEMA,
        temperature=0.0,
    )


def to_definition(
    data: dict[str, Any], samples: Sequence[str], *, name: str | None = None
) -> dict[str, Any]:
    """A format YAML mapping from the model's answer (samples that match become tests)."""
    regexes = [r for r in (data.get("regex") or []) if isinstance(r, str) and r]
    definition: dict[str, Any] = {
        "name": safe_name(name or str(data.get("name") or "learned_format")),
        "description": str(data.get("description") or "Format learned from sample logs.").strip(),
        "regex": regexes or ["(?P<message>.*)"],
        "priority": 90,
    }
    hints = [h for h in (data.get("timestamp_format") or []) if isinstance(h, str) and h]
    if hints:
        definition["timestamp_format"] = hints
    level_map = {
        str(item["raw"]): item["level"]
        for item in data.get("level_map") or []
        if isinstance(item, dict) and item.get("raw") and item.get("level") in LEVELS
    }
    if level_map:
        definition["level"] = {"group": data.get("level_group") or "level", "map": level_map}
    if data.get("record_start"):
        definition["record_start"] = str(data["record_start"])
    definition["sample_lines"] = _matching_samples(definition, samples)
    return definition


def safe_name(name: str) -> str:
    """A snake_case format name that does not override a built-in format."""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "learned_format"
    if not cleaned[0].isalpha():
        cleaned = f"format_{cleaned}"
    return f"{cleaned}_learned" if cleaned in TEXT_FORMATS else cleaned


def _matching_samples(definition: dict[str, Any], samples: Sequence[str]) -> list[str]:
    try:
        patterns = [re.compile(r) for r in definition["regex"]]
    except re.error:
        return list(samples[:1])
    matched = [s for s in samples if any(p.match(s) for p in patterns)]
    return matched[:SAMPLE_LINES_KEPT] or list(samples[:1])


# --- Validation --------------------------------------------------------------------------------


def validate(definition: dict[str, Any], records: Sequence[str]) -> Validation:
    """Load the definition and measure it on the records."""
    try:
        text_format = build_text_format(definition, source=f"{definition.get('name')}.yaml")
    except FormatDefinitionError as error:
        raw_matched, raw_failing = _raw_matches(definition, records)
        return Validation(
            match_rate=raw_matched / (len(records) or 1),
            records=len(records),
            errors=(str(error).split(": ", 1)[-1],),
            failing=raw_failing,
        )
    parser = TextFormatParser(text_format)
    normalizer = TimestampNormalizer(
        text_format.timestamp_formats, tz=ZoneInfo("UTC"), reference=datetime.now(UTC)
    )
    matched = timestamped = with_level = levels_mapped = 0
    failing: list[str] = []
    for index, text in enumerate(records, start=1):
        event = parser.parse(Record("validation", index, index, text))
        if event.error is not None:
            if len(failing) < FAILING_LINES_SENT:
                failing.append(text)
            continue
        matched += 1
        timestamped += normalizer.to_utc(event.timestamp_raw) is not None
        if event.level_raw is not None:
            with_level += 1
            levels_mapped += event.level is not None
    total = len(records) or 1
    return Validation(
        match_rate=matched / total,
        timestamp_rate=timestamped / matched if matched else 0.0,
        level_rate=levels_mapped / with_level if with_level else None,
        records=len(records),
        failing=tuple(failing),
    )


def _raw_matches(definition: dict[str, Any], records: Sequence[str]) -> tuple[int, tuple[str, ...]]:
    """Records matched by the bare regexes of an unusable definition, and some that are not.

    The retry prompt can then show the model the lines it missed even when the definition
    as a whole did not load.
    """
    try:
        patterns = [re.compile(r) for r in definition.get("regex") or []]
    except (re.error, TypeError):
        return 0, tuple(records[:FAILING_LINES_SENT])
    matched = 0
    failing: list[str] = []
    for text in records:
        if any(p.match(text) for p in patterns):
            matched += 1
        elif len(failing) < FAILING_LINES_SENT:
            failing.append(text)
    return matched, tuple(failing)


# --- Learning ---------------------------------------------------------------------------------


def learn_format(
    source: str | Path,
    client: LLMClient,
    *,
    formats_dir: Path | None,
    name: str | None = None,
    confirm: Callable[[list[str]], bool] | None = None,
    max_retries: int = MAX_RETRIES,
) -> LearnResult:
    """Learn, validate and (with ``formats_dir``) save a format for ``source``."""
    records = load_records(source)
    if not records:
        raise AIError(f"no records to learn from in {source}")
    samples = select_samples(records)
    if confirm is not None and not confirm(samples):
        raise LearnCancelled("cancelled: nothing was sent")

    best: tuple[dict[str, Any], Validation] | None = None
    previous: dict[str, Any] | None = None
    validation: Validation | None = None
    usage = Usage()
    attempts = 0
    while attempts <= max_retries:
        attempts += 1
        data, response = complete_json(
            client, build_request(samples, previous=previous, validation=validation)
        )
        usage = usage + response.usage
        if not isinstance(data, dict):
            raise AIError("the model's answer was not a format definition")
        previous = to_definition(data, samples, name=name)
        validation = validate(previous, records)
        if best is None or _better(validation, best[1]):
            best = (previous, validation)
        if validation.ok:
            break

    assert best is not None
    definition, best_validation = best
    result = LearnResult(definition, best_validation, attempts, samples, usage=usage)
    if formats_dir is not None and not best_validation.errors and best_validation.match_rate > 0:
        result.path = save_definition(definition, best_validation, client.model, formats_dir)
    return result


def _better(candidate: Validation, current: Validation) -> bool:
    if bool(candidate.errors) != bool(current.errors):
        return not candidate.errors
    return candidate.match_rate > current.match_rate


def save_definition(
    definition: dict[str, Any], validation: Validation, model: str, formats_dir: Path
) -> Path:
    """Write the YAML with provenance metadata; returns its path."""
    document = {
        **definition,
        "metadata": {
            "generated_by": "ai",
            "model": model,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "match_rate": round(validation.match_rate, 4),
        },
    }
    formats_dir.mkdir(parents=True, exist_ok=True)
    path = formats_dir / f"{definition['name']}.yaml"
    path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )
    return path
