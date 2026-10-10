"""Aggregates: the only SQL of the analysis layer.

Every function runs one or two DuckDB queries over the ``events`` view of a store
(``loglens.store.connect``) and returns small Python values: per-template statistics,
bucketed counts, template transitions, or a bounded list of events. Results are bounded
by the number of templates, buckets and transitions, never by the number of events.
Values from callers are always passed as parameters, never interpolated into SQL.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import duckdb

from loglens.analysis.model import EventRef, TimeWindow
from loglens.analysis.timeseries import bucket_count, choose_bucket

Connection = duckdb.DuckDBPyConnection

MESSAGE_CHARS: Final = 500
"""Evidence messages are cut to this length."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class TemplateStats:
    """Counts and times of one template."""

    template_id: str
    template: str
    events: int
    by_level: Mapping[str, int]
    first_seen: datetime | None
    last_seen: datetime | None
    sources: int
    components: tuple[tuple[str, int], ...]
    timestamped: int


@dataclass(frozen=True)
class Span:
    """First and last timestamps, and how many events have (no) timestamp."""

    start: datetime | None
    end: datetime | None
    timestamped: int
    untimestamped: int

    @property
    def length(self) -> timedelta | None:
        """``end - start`` (None without timestamps)."""
        if self.start is None or self.end is None:
            return None
        return self.end - self.start


@dataclass(frozen=True)
class Grid:
    """Time buckets shared by every series of one analysis."""

    start: datetime
    bucket: timedelta
    size: int

    def bucket_start(self, index: int) -> datetime:
        """Start time of bucket ``index``."""
        return self.start + index * self.bucket

    def bucket_end(self, index: int) -> datetime:
        """End time (exclusive) of bucket ``index``."""
        return self.start + (index + 1) * self.bucket


@dataclass(frozen=True)
class Transition:
    """``count`` times, an event of template ``prev`` was followed by one of ``next``."""

    prev: str
    next: str
    count: int


def _ms(value: datetime) -> int:
    return (value - _EPOCH) // timedelta(milliseconds=1)


def _where(window: TimeWindow) -> tuple[str, list[object]]:
    return window.where("timestamp")


def _bucket_sql(grid: Grid) -> tuple[str, list[object]]:
    """SQL for the bucket index of an event, and its parameters."""
    return "((epoch_ms(timestamp) - ?) // ?)", [
        _ms(grid.start),
        grid.bucket // timedelta(milliseconds=1),
    ]


def template_stats(conn: Connection, window: TimeWindow) -> list[TemplateStats]:
    """Statistics of every template with events in the window, by template id."""
    where, params = _where(window)
    rows = conn.execute(
        f"""
        SELECT template_id, any_value(template), count(*), min(timestamp), max(timestamp),
               count(DISTINCT source_file), count(timestamp)
        FROM events WHERE {where} GROUP BY template_id ORDER BY template_id
        """,
        params,
    ).fetchall()
    levels: dict[str, dict[str, int]] = {}
    for template_id, level, count in conn.execute(
        f"SELECT template_id, level, count(*) FROM events WHERE {where} GROUP BY ALL", params
    ).fetchall():
        levels.setdefault(template_id, {})[level] = count
    components: dict[str, list[tuple[str, int]]] = {}
    for template_id, component, count in conn.execute(
        f"""
        SELECT template_id, component, count(*) AS n FROM events
        WHERE {where} AND component IS NOT NULL AND component <> ''
        GROUP BY template_id, component
        QUALIFY row_number() OVER (PARTITION BY template_id ORDER BY n DESC, component) <= 3
        ORDER BY template_id, n DESC, component
        """,
        params,
    ).fetchall():
        components.setdefault(template_id, []).append((component, count))
    return [
        TemplateStats(
            template_id=template_id,
            template=template,
            events=events,
            by_level=levels.get(template_id, {}),
            first_seen=first,
            last_seen=last,
            sources=sources,
            components=tuple(components.get(template_id, ())),
            timestamped=timestamped,
        )
        for template_id, template, events, first, last, sources, timestamped in rows
    ]


def time_span(conn: Connection, window: TimeWindow) -> Span:
    """First and last timestamps in the window, and timestamped/untimestamped counts."""
    where, params = _where(window)
    row = conn.execute(
        f"SELECT min(timestamp), max(timestamp), count(timestamp), count(*) - count(timestamp) "
        f"FROM events WHERE {where}",
        params,
    ).fetchone()
    if row is None:  # pragma: no cover - an aggregate always returns a row
        return Span(None, None, 0, 0)
    return Span(row[0], row[1], row[2], row[3])


def make_grid(span: Span, bucket: timedelta | None = None) -> Grid | None:
    """Buckets covering the span, aligned to multiples of the bucket (None: no timestamps)."""
    if span.start is None or span.end is None:
        return None
    bucket = bucket or choose_bucket(span.end - span.start)
    bucket_ms = bucket // timedelta(milliseconds=1)
    start = _EPOCH + timedelta(milliseconds=_ms(span.start) // bucket_ms * bucket_ms)
    return Grid(start, bucket, bucket_count(span.end - start, bucket))


def _dense(grid: Grid, pairs: Sequence[tuple[int, int]]) -> list[int]:
    values = [0] * grid.size
    for index, count in pairs:
        values[index] = count
    return values


def _series_query(
    conn: Connection,
    grid: Grid,
    window: TimeWindow,
    key: str,
    condition: str = "true",
    condition_params: Sequence[object] = (),
) -> list[tuple[str, int, int]]:
    where, params = _where(window)
    bucket, bucket_params = _bucket_sql(grid)
    return conn.execute(
        f"""
        SELECT {key} AS key, b, count(*) FROM (
            SELECT {key}, {bucket} AS b FROM events
            WHERE {where} AND timestamp IS NOT NULL AND {condition}
        ) WHERE b >= 0 AND b < ? GROUP BY ALL
        """,
        [*bucket_params, *params, *condition_params, grid.size],
    ).fetchall()


def total_series(
    conn: Connection, grid: Grid, window: TimeWindow, levels: Sequence[str] | None = None
) -> list[int]:
    """Events per bucket (only the given levels, if any)."""
    if levels is None:
        rows = _series_query(conn, grid, window, "'*'")
    else:
        rows = _series_query(conn, grid, window, "'*'", "list_contains(?, level)", [list(levels)])
    return _dense(grid, [(index, count) for _, index, count in rows])


def level_series(conn: Connection, grid: Grid, window: TimeWindow) -> dict[str, list[int]]:
    """Events per bucket for each level present."""
    return _grouped(grid, _series_query(conn, grid, window, "level"))


def template_series(
    conn: Connection, grid: Grid, window: TimeWindow, template_ids: Sequence[str]
) -> dict[str, list[int]]:
    """Events per bucket for each of the given templates (those with events)."""
    if not template_ids:
        return {}
    rows = _series_query(
        conn, grid, window, "template_id", "list_contains(?, template_id)", [list(template_ids)]
    )
    return _grouped(grid, rows)


def _grouped(grid: Grid, rows: Sequence[tuple[str, int, int]]) -> dict[str, list[int]]:
    pairs: dict[str, list[tuple[int, int]]] = {}
    for key, index, count in rows:
        pairs.setdefault(key, []).append((index, count))
    return {key: _dense(grid, items) for key, items in sorted(pairs.items())}


_TRANSITIONS: Final = """
    SELECT event_id, template_id,
           lag(template_id) OVER (PARTITION BY source_file ORDER BY line_start) AS prev
    FROM events WHERE {where}
"""


def transition_counts(conn: Connection, window: TimeWindow) -> list[Transition]:
    """Consecutive template pairs within each source file (in line order), counted."""
    where, params = _where(window)
    rows = conn.execute(
        f"SELECT prev, template_id, count(*) FROM ({_TRANSITIONS.format(where=where)}) "
        "WHERE prev IS NOT NULL GROUP BY ALL ORDER BY 1, 2",
        params,
    ).fetchall()
    return [Transition(prev, nxt, count) for prev, nxt, count in rows]


def transition_event_ids(
    conn: Connection, pairs: Sequence[tuple[str, str]], window: TimeWindow
) -> dict[tuple[str, str], list[str]]:
    """Ids of the second events of the given transitions, in line order."""
    if not pairs:
        return {}
    where, params = _where(window)
    keys = [f"{prev}>{nxt}" for prev, nxt in pairs]
    rows = conn.execute(
        f"""
        SELECT prev, template_id, event_id FROM ({_TRANSITIONS.format(where=where)})
        WHERE prev IS NOT NULL AND list_contains(?, prev || '>' || template_id)
        ORDER BY event_id
        """,
        [*params, keys],
    ).fetchall()
    result: dict[tuple[str, str], list[str]] = {}
    for prev, nxt, event_id in rows:
        result.setdefault((prev, nxt), []).append(event_id)
    return result


_REF_COLUMNS: Final = (
    f"event_id, source_file, line_start, line_end, timestamp, level, left(message, {MESSAGE_CHARS})"
)


def _refs(rows: Sequence[tuple[object, ...]]) -> list[EventRef]:
    return [EventRef(*row) for row in rows]  # type: ignore[arg-type]


def event_refs(conn: Connection, event_ids: Sequence[str]) -> list[EventRef]:
    """Evidence for the given events, ordered by source file and line."""
    if not event_ids:
        return []
    rows = conn.execute(
        f"SELECT {_REF_COLUMNS} FROM events WHERE list_contains(?, event_id) "
        "ORDER BY source_file, line_start",
        [list(event_ids)],
    ).fetchall()
    return _refs(rows)


_ORDER: Final = "timestamp NULLS LAST, source_file, line_start"


def sample_events(
    conn: Connection,
    template_ids: Sequence[str] | None,
    window: TimeWindow,
    k: int = 3,
    levels: Sequence[str] | None = None,
) -> list[EventRef]:
    """The first and last matching events, plus up to ``k`` spread between them.

    ``template_ids`` None matches every template; ``levels`` restricts the levels.
    """
    if template_ids is not None and not template_ids:
        return []
    where, params = _where(window)
    conditions = [where]
    filter_params: list[object] = [*params]
    if template_ids is not None:
        conditions.append("list_contains(?, template_id)")
        filter_params.append(list(template_ids))
    if levels is not None:
        conditions.append("list_contains(?, level)")
        filter_params.append(list(levels))
    filtered = f"FROM events WHERE {' AND '.join(conditions)}"
    total_row = conn.execute(f"SELECT count(*) {filtered}", filter_params).fetchone()
    total = int(total_row[0]) if total_row else 0
    if total == 0:
        return []
    positions = sorted({1, total, *(1 + (total - 1) * i // (k + 1) for i in range(1, k + 1))})
    rows = conn.execute(
        f"""
        SELECT {_REF_COLUMNS} FROM (
            SELECT *, row_number() OVER (ORDER BY {_ORDER}) AS rn {filtered}
        ) WHERE list_contains(?, rn) ORDER BY rn
        """,
        [*filter_params, positions],
    ).fetchall()
    return _refs(rows)


def first_event_ids(
    conn: Connection, template_ids: Sequence[str], window: TimeWindow
) -> dict[str, str]:
    """The first event (by time, then position) of each template."""
    if not template_ids:
        return {}
    where, params = _where(window)
    rows = conn.execute(
        f"""
        SELECT template_id, event_id FROM events
        WHERE {where} AND list_contains(?, template_id)
        QUALIFY row_number() OVER (PARTITION BY template_id ORDER BY {_ORDER}) = 1
        """,
        [*params, list(template_ids)],
    ).fetchall()
    return dict(rows)


def event_ids_in_cells(
    conn: Connection,
    grid: Grid,
    cells: Sequence[tuple[int, str | None]],
    window: TimeWindow,
    levels: Sequence[str] | None = None,
) -> set[str]:
    """Ids of the events in the given (bucket, template) cells; template None means any."""
    if not cells:
        return set()
    where, params = _where(window)
    bucket, bucket_params = _bucket_sql(grid)
    keyed = [f"{index}|{template_id}" for index, template_id in cells if template_id is not None]
    whole = [index for index, template_id in cells if template_id is None]
    level_filter = "list_contains(?, level)" if levels is not None else "true"
    level_params: list[object] = [list(levels)] if levels is not None else []
    rows = conn.execute(
        f"""
        SELECT event_id FROM (
            SELECT event_id, template_id, level, {bucket} AS b FROM events
            WHERE {where} AND timestamp IS NOT NULL
        ) WHERE (list_contains(?, b::VARCHAR || '|' || template_id) OR list_contains(?, b))
          AND {level_filter}
        """,
        [*bucket_params, *params, keyed, whole, *level_params],
    ).fetchall()
    return {event_id for (event_id,) in rows}


def group_source_counts(
    conn: Connection, groups: Sequence[Sequence[str]], window: TimeWindow
) -> list[int]:
    """For each group of template ids, the number of distinct source files with its events."""
    if not groups:
        return []
    template_ids = [template_id for group in groups for template_id in group]
    indexes = [index for index, group in enumerate(groups) for _ in group]
    where, params = _where(window)
    rows = conn.execute(
        f"""
        SELECT g, count(DISTINCT source_file) FROM events
        JOIN (SELECT unnest(?) AS tid, unnest(?) AS g) AS m ON events.template_id = m.tid
        WHERE {where} GROUP BY g
        """,
        [template_ids, indexes, *params],
    ).fetchall()
    counts = [0] * len(groups)
    for index, count in rows:
        counts[index] = count
    return counts
