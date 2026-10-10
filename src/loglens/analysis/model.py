"""Types shared by every analysis: time windows, event references, JSON conversion."""

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TimeWindow:
    """A half-open time range ``[since, until)``; an unset bound is unbounded."""

    since: datetime | None = None
    until: datetime | None = None

    @property
    def active(self) -> bool:
        """Whether the window restricts anything (and so drops untimestamped events)."""
        return self.since is not None or self.until is not None

    def where(self, column: str = "timestamp") -> tuple[str, list[object]]:
        """An SQL condition for the window (``true`` when inactive) and its parameters."""
        parts: list[str] = []
        params: list[object] = []
        if self.since is not None:
            parts.append(f"{column} >= ?")
            params.append(self.since)
        if self.until is not None:
            parts.append(f"{column} < ?")
            params.append(self.until)
        return (" AND ".join(parts) or "true"), params


@dataclass(frozen=True)
class EventRef:
    """A pointer to one event, as evidence: where it is and what it says."""

    event_id: str
    source_file: str
    line_start: int
    line_end: int
    timestamp: datetime | None
    level: str
    message: str

    @property
    def location(self) -> str:
        """``file:line`` or ``file:first-last``."""
        lines = (
            str(self.line_start)
            if self.line_end == self.line_start
            else f"{self.line_start}-{self.line_end}"
        )
        return f"{self.source_file}:{lines}"


def jsonable(value: object) -> object:
    """Convert reports to JSON-ready values.

    Dataclasses become dicts (with ``location`` added for event references), datetimes
    ISO 8601 strings, timedeltas seconds, sets sorted lists, infinite floats strings.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result = {
            field.name: jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
        if isinstance(value, EventRef):
            result["location"] = value.location
        return result
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(jsonable(item) for item in value)  # type: ignore[type-var]
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    return value
