"""Compare two stores, or two periods of one store: new, gone and changed issues.

Issues are matched across sides through their templates: same template id first (ids
are content hashes), else the lexical rule of ``grouping`` (two runs may generalize a
message differently). An issue has *changed* when its rate per hour differs by at least
``MIN_RATIO`` and an exact Poisson rate test says the difference is not chance.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from scipy.stats import binomtest

from loglens.analysis.aggregates import Connection, Span, template_stats, time_span
from loglens.analysis.grouping import match_templates
from loglens.analysis.issues import Issue, IssuesReport, find_issues, template_issue_map
from loglens.analysis.model import TimeWindow
from loglens.analysis.signals import Signals, load_signals
from loglens.schema.levels import ERROR_LEVELS

MIN_RATIO: Final = 2.0
MAX_P: Final = 0.01


@dataclass(frozen=True)
class IssueChange:
    """One issue on both sides (or only one, for new and gone issues)."""

    title: str
    baseline: Issue | None
    target: Issue | None
    baseline_rate: float
    target_rate: float
    """Events per hour (per side when neither side has timestamps)."""
    ratio: float
    p_value: float | None


@dataclass(frozen=True)
class SideSummary:
    """Size of one side of a comparison."""

    events: int
    error_events: int
    hours: float | None

    @property
    def error_rate(self) -> float:
        """Share of ERROR and FATAL events."""
        return self.error_events / self.events if self.events else 0.0


@dataclass(frozen=True)
class CompareReport:
    """What appeared, disappeared and changed between a baseline and a target."""

    new: tuple[IssueChange, ...]
    gone: tuple[IssueChange, ...]
    changed: tuple[IssueChange, ...]
    baseline: SideSummary
    target: SideSummary
    notes: tuple[str, ...]


def rate_test(c1: int, t1: float, c2: int, t2: float) -> float:
    """Two-sided p-value that counts ``c1`` over ``t1`` and ``c2`` over ``t2`` share a rate.

    Conditional on the total, ``c2`` is binomial with probability ``t2 / (t1 + t2)``.
    """
    if c1 + c2 == 0:
        return 1.0
    return float(binomtest(c2, c1 + c2, t2 / (t1 + t2)).pvalue)


def compare(
    baseline_conn: Connection,
    target_conn: Connection,
    *,
    baseline_window: TimeWindow = TimeWindow(),  # noqa: B008 - frozen, never mutated
    target_window: TimeWindow = TimeWindow(),  # noqa: B008
    signals: Signals | None = None,
) -> CompareReport:
    """Compare the issues of two stores, or of two windows of one store."""
    signals = signals or load_signals()
    base = find_issues(baseline_conn, window=baseline_window, signals=signals, evidence=False)
    target = find_issues(target_conn, window=target_window, signals=signals, evidence=False)
    base_side = _side(baseline_conn, baseline_window)
    target_side = _side(target_conn, target_window)
    notes: list[str] = []
    base_hours, target_hours = base_side.hours, target_side.hours
    if base_hours is None or target_hours is None:
        notes.append("a side has no timestamp: raw counts are compared, not rates")
        base_hours = target_hours = 1.0

    matches = _match_issues(
        base, target, _texts(baseline_conn, baseline_window), _texts(target_conn, target_window)
    )
    matched_base = set(matches.values())
    new: list[IssueChange] = []
    changed: list[IssueChange] = []
    by_id = {issue.issue_id: issue for issue in base.issues}
    for issue in target.issues:
        rate = issue.events / target_hours
        if issue.issue_id not in matches:
            new.append(IssueChange(issue.title, None, issue, 0.0, rate, math.inf, None))
            continue
        before = by_id[matches[issue.issue_id]]
        before_rate = before.events / base_hours
        ratio = max(rate, before_rate) / max(min(rate, before_rate), 1e-12)
        p_value = rate_test(before.events, base_hours, issue.events, target_hours)
        if ratio >= MIN_RATIO and p_value < MAX_P:
            changed.append(
                IssueChange(issue.title, before, issue, before_rate, rate, ratio, p_value)
            )
    gone = [
        IssueChange(issue.title, issue, None, issue.events / base_hours, 0.0, math.inf, None)
        for issue in base.issues
        if issue.issue_id not in matched_base
    ]
    return CompareReport(
        new=tuple(sorted(new, key=lambda c: (-(c.target.impact if c.target else 0), c.title))),
        gone=tuple(
            sorted(gone, key=lambda c: (-(c.baseline.impact if c.baseline else 0), c.title))
        ),
        changed=tuple(sorted(changed, key=lambda c: (c.p_value or 0.0, c.title))),
        baseline=base_side,
        target=target_side,
        notes=tuple(notes),
    )


def _impact(issue: Issue | None) -> float:
    return issue.impact if issue else 0.0


def _side(conn: Connection, window: TimeWindow) -> SideSummary:
    stats = template_stats(conn, window)
    span = time_span(conn, window)
    return SideSummary(
        events=sum(s.events for s in stats),
        error_events=sum(n for s in stats for lvl, n in s.by_level.items() if lvl in ERROR_LEVELS),
        hours=_hours(span),
    )


def _hours(span: Span) -> float | None:
    length = span.length
    if length is None:
        return None
    return max(length, timedelta(seconds=1)) / timedelta(hours=1)


def _texts(conn: Connection, window: TimeWindow) -> dict[str, str]:
    return {s.template_id: s.template for s in template_stats(conn, window)}


def _match_issues(
    base: IssuesReport,
    target: IssuesReport,
    base_texts: dict[str, str],
    target_texts: dict[str, str],
) -> dict[str, str]:
    """Target issue id -> baseline issue id, through matched templates."""
    base_issue_of = template_issue_map(base)
    target_templates = [t for issue in target.issues for t in issue.template_ids]
    template_matches = match_templates(
        [(t, base_texts[t]) for t in sorted(base_issue_of)],
        [(t, target_texts[t]) for t in target_templates],
    )
    base_events = {issue.issue_id: issue.events for issue in base.issues}
    result: dict[str, str] = {}
    for issue in target.issues:
        candidates = _matched(issue.template_ids, template_matches, base_issue_of)
        if candidates:
            result[issue.issue_id] = max(candidates, key=lambda i: (base_events[i], i))
    return result


def _matched(
    template_ids: Sequence[str], matches: dict[str, str], issue_of: dict[str, str]
) -> set[str]:
    return {issue_of[matches[t]] for t in template_ids if t in matches}
