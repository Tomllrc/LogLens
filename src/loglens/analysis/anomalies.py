"""Anomaly detection: spikes, level shifts, new patterns and rare transitions.

Four detectors, each looking only at the unified events:

* ``spike``: a bucket far above the series' median (robust z-score), on the total
  volume, the ERROR+FATAL volume, and the busiest templates.
* ``level_shift``: the volume settles at a new level (PELT change points), on the total
  and on each level's series.
* ``new_pattern``: a template first seen after the baseline period.
* ``rare_transition``: in a file, a template that almost always has the same successor is
  followed by one it almost never has.

Scores are normalized to 0-1 within each kind. ``predicted_event_ids`` turns a report
into the set of events flagged as anomalous (used by the evaluation).
"""

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from loglens.analysis.aggregates import (
    Connection,
    Grid,
    Span,
    TemplateStats,
    event_ids_in_cells,
    event_refs,
    first_event_ids,
    level_series,
    make_grid,
    sample_events,
    template_series,
    template_stats,
    time_span,
    total_series,
    transition_counts,
    transition_event_ids,
)
from loglens.analysis.issues import group_candidates, issue_id_for
from loglens.analysis.model import EventRef, TimeWindow
from loglens.analysis.signals import Signals, load_signals
from loglens.analysis.timeseries import MIN_POINTS, change_points, robust_z, spikes
from loglens.schema.levels import ERROR_LEVELS

KINDS: Final = ("spike", "level_shift", "new_pattern", "rare_transition")

TOP_TEMPLATE_SERIES: Final = 50
MAX_SPIKE_Z: Final = 50.0
BASELINE_SHARE: Final = 0.2
RARE_PROBABILITY: Final = 0.001
RARE_MIN_SUPPORT: Final = 100
RARE_MIN_DOMINANCE: Final = 0.9
TRANSITION_EVIDENCE: Final = 5
SUMMARY_CHARS: Final = 80

_ERRORS: Final = sorted(ERROR_LEVELS)


@dataclass(frozen=True)
class Anomaly:
    """One finding: what, when, which templates, and the events that show it."""

    kind: str
    score: float
    raw_score: float
    start: datetime | None
    end: datetime | None
    series: str
    """``total``, ``errors``, ``level:<LEVEL>``, ``template``, ``pattern`` or ``transition``."""
    template_ids: tuple[str, ...]
    issue_id: str | None
    summary: str
    buckets: tuple[int, ...]
    evidence: tuple[EventRef, ...]


@dataclass(frozen=True)
class AnomalyReport:
    """Anomalies grouped by kind (in ``KINDS`` order), each kind sorted by score."""

    anomalies: tuple[Anomaly, ...]
    grid: Grid | None
    span: Span
    window: TimeWindow
    baseline_until: datetime | None
    notes: tuple[str, ...]

    def by_kind(self, kind: str) -> list[Anomaly]:
        """The anomalies of one kind, best first."""
        return [a for a in self.anomalies if a.kind == kind]


@dataclass(frozen=True)
class _Context:
    conn: Connection
    window: TimeWindow
    signals: Signals
    stats: Mapping[str, TemplateStats]
    issues: Mapping[str, str]


def find_anomalies(
    conn: Connection,
    *,
    window: TimeWindow = TimeWindow(),  # noqa: B008 - frozen, never mutated
    signals: Signals | None = None,
    bucket: timedelta | None = None,
    baseline_until: datetime | None = None,
    kinds: Sequence[str] = KINDS,
) -> AnomalyReport:
    """Run the selected detectors over a store (``conn`` from ``loglens.store.connect``)."""
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        raise ValueError(f"unknown anomaly kind {', '.join(unknown)}; use {', '.join(KINDS)}")
    signals = signals or load_signals()
    stats = template_stats(conn, window)
    issues = {
        s.template_id: issue_id_for([m.template_id for m in group])
        for group in group_candidates(stats, signals)
        for s in group
    }
    context = _Context(conn, window, signals, {s.template_id: s for s in stats}, issues)
    span = time_span(conn, window)
    grid = make_grid(span, bucket)
    notes: list[str] = []
    found: list[Anomaly] = []

    timed = [k for k in ("spike", "level_shift") if k in kinds]
    if timed and grid is None:
        if span.untimestamped:
            notes.append(f"{', '.join(timed)} skipped: events have no timestamp")
    elif timed and grid is not None and grid.size < MIN_POINTS:
        notes.append(f"{', '.join(timed)} skipped: only {grid.size} buckets (< {MIN_POINTS})")
    elif grid is not None:
        if "spike" in kinds:
            found += _spikes(context, grid)
        if "level_shift" in kinds:
            found += _level_shifts(context, grid)

    if "new_pattern" in kinds:
        if span.start is None or span.end is None or span.start == span.end:
            if span.untimestamped:
                notes.append("new_pattern skipped: events have no timestamp")
        else:
            cutoff = baseline_until or span.start + BASELINE_SHARE * (span.end - span.start)
            baseline_until = cutoff
            found += _new_patterns(context, cutoff)

    if "rare_transition" in kinds:
        found += _rare_transitions(context)

    ordered = sorted(_normalized(found), key=lambda a: (KINDS.index(a.kind), -a.score, a.summary))
    return AnomalyReport(tuple(ordered), grid, span, window, baseline_until, tuple(notes))


def _normalized(anomalies: Iterable[Anomaly]) -> list[Anomaly]:
    items = list(anomalies)
    best: dict[str, float] = {}
    for a in items:
        best[a.kind] = max(best.get(a.kind, 0.0), a.raw_score)
    return [replace(a, score=a.raw_score / best[a.kind] if best[a.kind] else 0.0) for a in items]


def _label(context: _Context, template_id: str) -> str:
    stats = context.stats.get(template_id)
    text = stats.template if stats else template_id
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 3] + "..."


def _when(grid: Grid, index: int) -> str:
    return grid.bucket_start(index).strftime("%Y-%m-%d %H:%M:%S")


def _runs(indexes: Sequence[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for index in indexes:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def _spikes(context: _Context, grid: Grid) -> list[Anomaly]:
    conn, window = context.conn, context.window
    timestamped = sorted(
        (s for s in context.stats.values() if s.timestamped),
        key=lambda s: (-s.timestamped, s.template_id),
    )[:TOP_TEMPLATE_SERIES]
    series: list[tuple[str, str | None, list[int]]] = [
        ("total", None, total_series(conn, grid, window)),
        ("errors", None, total_series(conn, grid, window, levels=_ERRORS)),
    ]
    by_template = template_series(conn, grid, window, [s.template_id for s in timestamped])
    series += [("template", tid, values) for tid, values in by_template.items()]
    found: list[Anomaly] = []
    for name, template_id, values in series:
        flagged = spikes(values)
        if not flagged:
            continue
        z = robust_z(values)
        median = statistics.median(values)
        for run in _runs(flagged):
            peak = max(run, key=lambda i: values[i])
            sample_window = TimeWindow(grid.bucket_start(run[0]), grid.bucket_end(run[-1]))
            evidence = sample_events(
                conn,
                [template_id] if template_id else None,
                _intersect(window, sample_window),
                k=1,
                levels=_ERRORS if name == "errors" else None,
            )
            label = {"total": "all events", "errors": "ERROR/FATAL events"}.get(
                name, _label(context, template_id or "")
            )
            found.append(
                Anomaly(
                    kind="spike",
                    score=0.0,
                    raw_score=min(max(z[i] for i in run), MAX_SPIKE_Z),
                    start=grid.bucket_start(run[0]),
                    end=grid.bucket_end(run[-1]),
                    series=name,
                    template_ids=(template_id,) if template_id else (),
                    issue_id=context.issues.get(template_id or ""),
                    summary=(
                        f"{label}: {values[peak]:,} in one {_duration(grid.bucket)} bucket at "
                        f"{_when(grid, peak)} (median {median:g})"
                    ),
                    buckets=tuple(run),
                    evidence=tuple(evidence),
                )
            )
    return found


def _level_shifts(context: _Context, grid: Grid) -> list[Anomaly]:
    conn, window = context.conn, context.window
    series = {"total": total_series(conn, grid, window)}
    series.update(
        {f"level:{level}": values for level, values in level_series(conn, grid, window).items()}
    )
    found: list[Anomaly] = []
    for name, values in series.items():
        for point in change_points(values):
            direction = "up" if point.after > point.before else "down"
            label = "total" if name == "total" else name.split(":", 1)[1]
            levels = None if name == "total" else [name.split(":", 1)[1]]
            start = grid.bucket_start(point.index)
            evidence = sample_events(
                conn,
                None,
                _intersect(window, TimeWindow(start, grid.bucket_end(point.index))),
                k=0,
                levels=levels,
            )
            found.append(
                Anomaly(
                    kind="level_shift",
                    score=0.0,
                    raw_score=math.log2(point.ratio),
                    start=start,
                    end=None,
                    series=name,
                    template_ids=(),
                    issue_id=None,
                    summary=(
                        f"{label} volume x{point.ratio:.1f} {direction} from "
                        f"{_when(grid, point.index)} ({point.before:g} -> {point.after:g} "
                        f"per {_duration(grid.bucket)})"
                    ),
                    buckets=(point.index,),
                    evidence=tuple(evidence),
                )
            )
    return found


def _new_patterns(context: _Context, cutoff: datetime) -> list[Anomaly]:
    new = sorted(
        (s for s in context.stats.values() if s.first_seen is not None and s.first_seen > cutoff),
        key=lambda s: s.template_id,
    )
    if not new:
        return []
    firsts = first_event_ids(context.conn, [s.template_id for s in new], context.window)
    refs = {ref.event_id: ref for ref in event_refs(context.conn, list(firsts.values()))}
    found: list[Anomaly] = []
    for stats in new:
        evidence = refs.get(firsts.get(stats.template_id, ""))
        found.append(
            Anomaly(
                kind="new_pattern",
                score=0.0,
                raw_score=_pattern_weight(context.signals, stats) + 1.0,
                start=stats.first_seen,
                end=stats.last_seen,
                series="pattern",
                template_ids=(stats.template_id,),
                issue_id=context.issues.get(stats.template_id),
                summary=f"new: {_label(context, stats.template_id)} ({stats.events:,} events)",
                buckets=(),
                evidence=(evidence,) if evidence else (),
            )
        )
    return found


def _pattern_weight(signals: Signals, stats: TemplateStats) -> float:
    """Weight of the template's most frequent level (the keyword weight for UNKNOWN)."""
    level = max(stats.by_level.items(), key=lambda item: (item[1], item[0]))[0]
    if level == "UNKNOWN":
        return signals.unknown_keyword_weight if signals.matches_keyword(stats.template) else 0.0
    return signals.level_weights.get(level, 0.0)


def _rare_transitions(context: _Context) -> list[Anomaly]:
    transitions = transition_counts(context.conn, context.window)
    outgoing: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for t in transitions:
        outgoing[t.prev][t.next] += t.count
    flagged: list[tuple[str, str, int, int, str]] = []
    for prev, successors in sorted(outgoing.items()):
        total = sum(successors.values())
        top, top_count = max(successors.items(), key=lambda item: (item[1], item[0]))
        if total < RARE_MIN_SUPPORT or top_count / total < RARE_MIN_DOMINANCE:
            continue
        for nxt, count in sorted(successors.items()):
            if count / total < RARE_PROBABILITY:
                flagged.append((prev, nxt, count, total, top))
    if not flagged:
        return []
    events = transition_event_ids(context.conn, [(p, n) for p, n, *_ in flagged], context.window)
    found: list[Anomaly] = []
    for prev, nxt, count, total, top in flagged:
        refs = event_refs(context.conn, events.get((prev, nxt), [])[:TRANSITION_EVIDENCE])
        times = [r.timestamp for r in refs if r.timestamp is not None]
        found.append(
            Anomaly(
                kind="rare_transition",
                score=0.0,
                raw_score=-math.log10(count / total),
                start=min(times) if times else None,
                end=max(times) if times else None,
                series="transition",
                template_ids=(prev, nxt),
                issue_id=context.issues.get(nxt),
                summary=(
                    f"'{_label(context, nxt)}' after '{_label(context, prev)}' "
                    f"({count} of {total:,}; usually '{_label(context, top)}')"
                ),
                buckets=(),
                evidence=tuple(refs),
            )
        )
    return found


def predicted_event_ids(conn: Connection, report: AnomalyReport) -> set[str]:
    """Events flagged by a report: the "which lines" view of its anomalies.

    Template spikes flag their cells, ERROR/FATAL spikes the error events of their
    buckets, new patterns their first event, rare transitions every occurrence of the
    rare successor. Total-volume spikes and level shifts say "when", not "which line",
    so they flag nothing.
    """
    return set().union(*(ids for _, ids in predictions_by_detector(conn, report).items()))


def predictions_by_detector(conn: Connection, report: AnomalyReport) -> dict[str, set[str]]:
    """Flagged events per detector (``spike``, ``new_pattern``, ``rare_transition``)."""
    window = report.window
    result: dict[str, set[str]] = {"spike": set(), "new_pattern": set(), "rare_transition": set()}
    if report.grid is not None:
        grid = report.grid
        template_cells = [
            (index, a.template_ids[0])
            for a in report.by_kind("spike")
            if a.series == "template"
            for index in a.buckets
        ]
        error_cells: list[tuple[int, str | None]] = [
            (index, None)
            for a in report.by_kind("spike")
            if a.series == "errors"
            for index in a.buckets
        ]
        result["spike"] = event_ids_in_cells(conn, grid, list(template_cells), window) | (
            event_ids_in_cells(conn, grid, error_cells, window, levels=_ERRORS)
        )
    patterns = [a.template_ids[0] for a in report.by_kind("new_pattern")]
    result["new_pattern"] = set(first_event_ids(conn, patterns, window).values())
    pairs = [(a.template_ids[0], a.template_ids[1]) for a in report.by_kind("rare_transition")]
    result["rare_transition"] = {
        event_id for ids in transition_event_ids(conn, pairs, window).values() for event_id in ids
    }
    return result


def _intersect(a: TimeWindow, b: TimeWindow) -> TimeWindow:
    def pick(
        x: datetime | None, y: datetime | None, choose: Callable[..., datetime]
    ) -> datetime | None:
        if x is None or y is None:
            return x or y
        return choose(x, y)

    return TimeWindow(pick(a.since, b.since, max), pick(a.until, b.until, min))


def _duration(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
