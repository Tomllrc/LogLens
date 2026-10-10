"""Recurring issues: candidate templates grouped, scored by impact, ranked with Pareto.

An issue is a group of similar candidate templates (``grouping``); a template is a
candidate when its levels or words say something went wrong (``signals``). Its impact is
the weighted count of its events; the "vital few" are the top issues that together
account for ``PARETO_SHARE`` of all impact.
"""

import dataclasses
import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from loglens.analysis.aggregates import (
    Connection,
    TemplateStats,
    group_source_counts,
    make_grid,
    sample_events,
    template_series,
    template_stats,
    time_span,
)
from loglens.analysis.grouping import group_templates
from loglens.analysis.model import EventRef, TimeWindow
from loglens.analysis.signals import Signals, load_signals

PARETO_SHARE: Final = 0.8
EVIDENCE_SAMPLES: Final = 3


@dataclass(frozen=True)
class Issue:
    """A recurring problem: one or more similar templates and their events."""

    issue_id: str
    title: str
    template_ids: tuple[str, ...]
    events: int
    by_level: Mapping[str, int]
    first_seen: datetime | None
    last_seen: datetime | None
    sources: int
    top_components: tuple[tuple[str, int], ...]
    persistence: float | None
    """Share of time buckets with at least one event (None without timestamps)."""
    impact: float
    share: float = 0.0
    cumulative_share: float = 0.0
    vital_few: bool = False
    evidence: tuple[EventRef, ...] = field(default=())


@dataclass(frozen=True)
class IssuesReport:
    """Issues ranked by impact, with totals and notes."""

    issues: tuple[Issue, ...]
    total_events: int
    candidate_events: int
    vital_few_count: int
    window: TimeWindow
    bucket: timedelta | None
    notes: tuple[str, ...]


def issue_id_for(template_ids: Sequence[str]) -> str:
    """A stable id: derived from the smallest member template id (stable across runs)."""
    digest = hashlib.blake2b(min(template_ids).encode(), digest_size=4).hexdigest()
    return f"ISS-{digest}"


def group_candidates(stats: Sequence[TemplateStats], signals: Signals) -> list[list[TemplateStats]]:
    """Candidate templates grouped into issues (members by events, then id)."""
    candidates = {s.template_id: s for s in stats if signals.is_candidate(s.by_level, s.template)}
    groups = group_templates([(s.template_id, s.template) for s in candidates.values()])
    return [
        sorted((candidates[i] for i in group), key=lambda s: (-s.events, s.template_id))
        for group in groups
    ]


def rank_issues(issues: Sequence[Issue]) -> tuple[Issue, ...]:
    """Sort by impact (then events, then id) and fill shares and the vital-few flags."""
    ordered = sorted(issues, key=lambda i: (-i.impact, -i.events, i.issue_id))
    total = sum(i.impact for i in ordered)
    ranked: list[Issue] = []
    cumulative = 0.0
    reached = total <= 0
    for issue in ordered:
        share = issue.impact / total if total > 0 else 0.0
        cumulative += share
        vital = not reached
        reached = reached or cumulative >= PARETO_SHARE - 1e-9
        ranked.append(
            dataclasses.replace(
                issue, share=share, cumulative_share=min(cumulative, 1.0), vital_few=vital
            )
        )
    return tuple(ranked)


def find_issues(
    conn: Connection,
    *,
    window: TimeWindow = TimeWindow(),  # noqa: B008 - frozen, never mutated
    signals: Signals | None = None,
    min_events: int = 1,
    bucket: timedelta | None = None,
    evidence: bool = True,
) -> IssuesReport:
    """Recurring issues of a store (``conn`` from ``loglens.store.connect``)."""
    signals = signals or load_signals()
    stats = template_stats(conn, window)
    span = time_span(conn, window)
    grid = make_grid(span, bucket)
    groups = [g for g in group_candidates(stats, signals) if sum(s.events for s in g) >= min_events]
    member_ids = [[s.template_id for s in group] for group in groups]
    sources = group_source_counts(conn, member_ids, window)
    series = (
        template_series(conn, grid, window, [i for ids in member_ids for i in ids])
        if grid is not None
        else {}
    )
    issues = [
        _build_issue(group, ids, signals, count, series, grid.size if grid else None)
        for group, ids, count in zip(groups, member_ids, sources, strict=True)
    ]
    ranked = rank_issues(issues)
    if evidence:
        ranked = tuple(
            dataclasses.replace(
                issue,
                evidence=tuple(sample_events(conn, issue.template_ids, window, k=EVIDENCE_SAMPLES)),
            )
            for issue in ranked
        )
    total = span.timestamped + span.untimestamped
    notes: list[str] = []
    if total == 0:
        notes.append("no events" + (" in this time window" if window.active else ""))
    elif span.untimestamped:
        notes.append(f"{span.untimestamped:,} events have no timestamp (persistence n/a)")
    return IssuesReport(
        issues=ranked,
        total_events=total,
        candidate_events=sum(i.events for i in ranked),
        vital_few_count=sum(i.vital_few for i in ranked),
        window=window,
        bucket=grid.bucket if grid else None,
        notes=tuple(notes),
    )


def _build_issue(
    members: Sequence[TemplateStats],
    template_ids: Sequence[str],
    signals: Signals,
    sources: int,
    series: Mapping[str, Sequence[int]],
    buckets: int | None,
) -> Issue:
    by_level: Counter[str] = Counter()
    components: Counter[str] = Counter()
    for member in members:
        by_level.update(member.by_level)
        components.update(dict(member.components))
    firsts = [m.first_seen for m in members if m.first_seen is not None]
    lasts = [m.last_seen for m in members if m.last_seen is not None]
    persistence: float | None = None
    if buckets and any(m.timestamped for m in members):
        active: set[int] = set()
        for template_id in template_ids:
            active.update(i for i, n in enumerate(series.get(template_id, ())) if n)
        persistence = len(active) / buckets
    return Issue(
        issue_id=issue_id_for(template_ids),
        title=members[0].template,
        template_ids=tuple(template_ids),
        events=sum(m.events for m in members),
        by_level=dict(sorted(by_level.items())),
        first_seen=min(firsts) if firsts else None,
        last_seen=max(lasts) if lasts else None,
        sources=sources,
        top_components=tuple(sorted(components.items(), key=lambda item: (-item[1], item[0]))[:3]),
        persistence=persistence,
        impact=sum(signals.impact(m.by_level, m.template) for m in members),
    )


def template_issue_map(report: IssuesReport) -> dict[str, str]:
    """Template id -> issue id, for every template in an issue."""
    return {
        template_id: issue.issue_id for issue in report.issues for template_id in issue.template_ids
    }
