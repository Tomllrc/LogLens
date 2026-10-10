# Phase 4: Analysis without AI. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `loglens issues`, `loglens anomalies`, `loglens compare` over a LogLens store, plus an honest anomaly evaluation on labeled BGL data.

**Architecture:** `analysis/aggregates.py` is the only SQL (DuckDB over the `events` view, bounded results). Pure Python modules (`signals`, `grouping`, `timeseries`, `issues`, `anomalies`, `compare`) turn aggregates into report dataclasses; `cli/analysis_cmd.py` renders them (Rich or `--json`).

**Tech Stack:** Python 3.11+, DuckDB, ruptures (+ numpy, scipy), PyYAML, Typer/Rich, pytest + hypothesis.

**Spec:** `docs/superpowers/specs/2026-10-10-phase4-analysis-design.md`

## Global Constraints

- Analysis reads only the unified event schema and catalog tables; no format-specific code.
- Never load all events into Python: every query returns bounded aggregates (<= 500 buckets per series, <= 50 template series for spikes, capped evidence).
- Never crash on bad input: empty store, no timestamps, short series give partial reports with `notes`.
- mypy strict on `src/`, type hints and docstrings on public functions, ruff clean.
- Thresholds (fixed before evaluation): grouping Jaccard >= 0.8 with >= 3 constant tokens; weights FATAL 10, ERROR 5, WARN 1, keyword-UNKNOWN 2; buckets <= 500; spike z >= 5 and value >= median + 3; change points ratio >= 2, segments >= 3, series >= 20 points, penalty `3 * log(n) * sigma^2`; new pattern after start + 20% of span; rare transition P(b|a) < 0.001, count(a) >= 100, a's top successor share >= 0.9; compare ratio >= 2 and p < 0.01; Pareto 80%.
- CLI errors print one line and exit 1 (same as `loglens query`).
- Run tools as `uv run ...` (uv lives in `~/AppData/Roaming/Python/Python313/Scripts` on this machine).

## Spec adjustments found while planning

1. **Exit code 1, not 2** for CLI errors: `loglens query` uses `fail()` (exit 1); consistency wins.
2. **Rare transition needs an expected successor.** With randomly interleaved background traffic, any
   low-rate template is a "rare successor" of everything (P(b|a) ~ P(b) < 0.001), so the spec's
   rule would flag every steady-rate error. Added condition: a's most frequent successor accounts
   for >= 90% of a's transitions (a is part of a predictable sequence). Recorded as a decision.
3. **Planted `<IP>`/`<HOST>` split is not a grouping test:** Phase 1's single-placeholder merge
   already turns both into `<*>`. The planted split uses two lengths instead
   ("Connection to <IP> refused by peer" / "... refused by remote peer"), which Drain never
   merges and the lexical rule does (Jaccard 5/6).

## Review Focus

1. Store whose events all lack timestamps: `issues` must still rank, `anomalies` must still run `rare_transition` and note the skipped detectors. Test in Task 6 and Task 7.
2. A window (`--since/--until`) that matches no event: empty reports, no crash, no division by zero. Test in Task 5.
3. Single-event or single-timestamp store (span 0): bucket choice and persistence must not divide by zero. Test in Tasks 3 and 6.
4. Template text containing SQL-ish quotes or `%` (passed as parameters, never interpolated). Test in Task 5.
5. `compare` with an empty side (baseline or target has no events): everything becomes new/gone, no exception. Test in Task 8.

---

### Task 1: Shared model types and JSON conversion

**Files:**
- Create: `src/loglens/analysis/model.py`
- Test: `tests/test_analysis_model.py`

**Interfaces (produces):**
```python
@dataclass(frozen=True)
class TimeWindow:
    since: datetime | None = None
    until: datetime | None = None
    @property
    def active(self) -> bool: ...
    def where(self, column: str = "timestamp") -> tuple[str, list[object]]:
        """SQL condition (always non-empty, 'true' when inactive) and its parameters."""

@dataclass(frozen=True)
class EventRef:
    event_id: str; source_file: str; line_start: int; line_end: int
    timestamp: datetime | None; level: str; message: str
    @property
    def location(self) -> str: ...  # "file:12" or "file:12-14"

def jsonable(value: object) -> object:
    """Dataclasses, datetimes (ISO UTC), timedeltas (seconds), sets/tuples -> JSON-ready."""
```

- [ ] Tests: `TimeWindow().where()` is `("true", [])`; with since/until gives `timestamp >= ? AND timestamp < ?` with params; `location` single vs range; `jsonable` on nested dataclass with datetime/timedelta/frozenset/None.
- [ ] Implement, run `uv run pytest tests/test_analysis_model.py`.

### Task 2: Signals (candidate test, weights)

**Files:**
- Create: `src/loglens/analysis/signals.yaml`, `src/loglens/analysis/signals.py`
- Modify: `pyproject.toml` only if YAML package data is not already included (check `hatch` wheel includes `*.yaml` under src: masking.yaml already ships, so no change expected)
- Test: `tests/test_analysis_signals.py`

**Interfaces (produces):**
```python
class SignalsError(ValueError): ...
@dataclass(frozen=True)
class Signals:
    keywords: tuple[str, ...]            # regex sources
    level_weights: Mapping[str, float]   # FATAL 10, ERROR 5, WARN 1
    unknown_keyword_weight: float        # 2
    def matches_keyword(self, template: str) -> bool: ...
    def is_candidate(self, by_level: Mapping[str, int], template: str) -> bool: ...
    def impact(self, by_level: Mapping[str, int], template: str) -> float: ...
DEFAULT_SIGNALS_FILE: Traversable
def load_signals(path: Path | None = None) -> Signals: ...
```
Rules: keyword match is `re.search(r"(?<![A-Za-z0-9])(?:k1|k2|...)(?![A-Za-z0-9])", template, re.I)` (whole word, works for multi-word `timed out`). Candidate = any WARN/ERROR/FATAL count > 0, or UNKNOWN > 0 and keyword match. Impact = sum(level_weights[l] * n) + (UNKNOWN n * unknown_keyword_weight if keyword match).

YAML:
```yaml
# Signals for recurring-issue detection (loglens.analysis.signals).
failure_keywords: [error, errors, fail, failed, failure, failing, exception, timeout,
  timed out, refused, denied, fatal, panic, abort, aborted, corrupt, corrupted,
  unreachable, crash, crashed, killed, segfault, unavailable]
level_weights: {FATAL: 10, ERROR: 5, WARN: 1}
unknown_keyword_weight: 2
```

- [ ] Tests: default keywords match "Connection timed out", "Exception in thread", not "errorless"/"terror"; INFO-only "error count=0" is not a candidate; UNKNOWN + "failed" is; UNKNOWN without keyword is not; impact numbers (e.g. {ERROR: 3, WARN: 2} -> 17; {UNKNOWN: 4} + keyword -> 8); `load_signals(path)` overrides; invalid YAML, missing keys, non-numeric weight, unknown level name, bad regex all raise `SignalsError` mentioning the path.
- [ ] Implement; run tests.

### Task 3: Time series helpers

**Files:**
- Create: `src/loglens/analysis/timeseries.py`
- Test: `tests/test_analysis_timeseries.py`

**Interfaces (produces):**
```python
BUCKETS: tuple[timedelta, ...]  # 1s, 10s, 1m, 5m, 15m, 1h, 6h, 1d
MAX_BUCKETS = 500; MIN_POINTS = 20
def choose_bucket(span: timedelta) -> timedelta: ...      # span 0 -> 1s; > 500 days -> 1d
def bucket_count(span: timedelta, bucket: timedelta) -> int: ...  # floor(span/bucket) + 1
def parse_bucket(text: str) -> timedelta: ...             # "30s", "5m", "1h", "1d"; ValueError otherwise
def robust_z(values: Sequence[float]) -> list[float]: ...
def spikes(values: Sequence[float], z: float = 5.0, min_excess: float = 3.0) -> list[int]: ...
@dataclass(frozen=True)
class ChangePoint:
    index: int; before: float; after: float  # index = first bucket of the new segment
    @property
    def ratio(self) -> float: ...  # max(b,a)/max(min(b,a), 0.5)
def change_points(values: Sequence[float], min_size: int = 3, min_ratio: float = 2.0) -> list[ChangePoint]: ...
```
`robust_z`: median m, MAD; scale = 1.4826*MAD, else 1.2533*mean(|x-m|), else 0 -> z = 0 if x == m else copysign(inf, x-m). `change_points`: return [] if len < MIN_POINTS or all equal; sigma = 1.4826*MAD(diff(x))/sqrt(2), if 0 use std(x) or return []; `rpt.Pelt(model="l2", min_size=min_size, jump=1).fit(np.asarray(x, float).reshape(-1,1)).predict(pen=3*log(n)*sigma**2)`; for each breakpoint except the last (n), before = mean of previous segment, after = mean of next segment; keep if ratio >= min_ratio and both segments >= min_size.

- [ ] Tests: bucket choice (24h -> 5m, 10min -> 10s, 0 -> 1s, 3 years -> 1d), `parse_bucket` valid/invalid; robust_z known values; MAD=0 fallback; constant series -> all 0 and no spikes; spike in noisy series found at the right index; spike below min_excess not flagged; step 100->50 with noise found near index 144 of 288; spike-of-one-bucket not a change point; series < 20 -> []; Hypothesis: constant series give no spikes/change points; `robust_z(a*x+b) == robust_z(x)` for a > 0 (approx).
- [ ] mypy override for `ruptures` (`[[tool.mypy.overrides]] module = ["ruptures.*", "scipy.*"] ignore_missing_imports = true`).
- [ ] Implement; run tests.

### Task 4: Lexical grouping

**Files:**
- Create: `src/loglens/analysis/grouping.py`
- Test: `tests/test_analysis_grouping.py`

**Interfaces (produces):**
```python
MIN_JACCARD = 0.8; MIN_CONSTANT_TOKENS = 3
def constant_tokens(template: str) -> frozenset[str]: ...
def similar(a: frozenset[str], b: frozenset[str]) -> bool: ...
def group_templates(templates: Sequence[tuple[str, str]]) -> list[list[str]]: ...
def match_templates(a: Sequence[tuple[str, str]], b: Sequence[tuple[str, str]]) -> dict[str, str]: ...
```
`constant_tokens`: split on whitespace, drop tokens containing `<` + `>` placeholder pattern `<[A-Z*][A-Z0-9_*]*>`, strip `.,;:!?()[]{}"'` from both ends, lowercase, drop empty. Grouping: inverted index token -> template indexes; for each pair sharing a token (deduplicated, i < j), union if similar. Groups sorted by smallest id; members sorted. `match_templates`: maps each id of `b` to an id of `a`: exact id if present, else best Jaccard among similar (ties -> smallest id); unmatched ids absent.

- [ ] Tests: "Connection to <IP> refused by peer" + "Connection to <IP> refused by remote peer" merge; "<*> failed" + "<*> failed again" do not (too short); transitivity (A~B, B~C, A!~C -> one group); determinism; Hypothesis: shuffled input gives same groups; `match_templates` exact, lexical, unmatched.
- [ ] Implement; run tests.

### Task 5: Aggregates (the only SQL)

**Files:**
- Create: `src/loglens/analysis/aggregates.py`
- Create: `tests/analysis_helpers.py` (build stores from text in `tmp_path` via `run_parse(..., ParseOptions(workers=1))`)
- Test: `tests/test_analysis_aggregates.py`

**Interfaces (produces):**
```python
Connection = duckdb.DuckDBPyConnection
@dataclass(frozen=True)
class TemplateStats:
    template_id: str; template: str; events: int; by_level: Mapping[str, int]
    first_seen: datetime | None; last_seen: datetime | None; sources: int
    components: tuple[tuple[str, int], ...]; timestamped: int
@dataclass(frozen=True)
class Span:
    start: datetime | None; end: datetime | None; timestamped: int; untimestamped: int
    @property
    def length(self) -> timedelta | None: ...
@dataclass(frozen=True)
class Grid:   # bucket grid shared by every series of one analysis
    start: datetime; bucket: timedelta; size: int
    def bucket_start(self, index: int) -> datetime: ...
@dataclass(frozen=True)
class Transition:
    prev: str; next: str; count: int

def template_stats(conn, window: TimeWindow) -> list[TemplateStats]
def time_span(conn, window: TimeWindow) -> Span
def make_grid(span: Span, bucket: timedelta | None = None) -> Grid | None   # None without timestamps
def total_series(conn, grid: Grid, window, levels: Sequence[str] | None = None) -> list[int]
def level_series(conn, grid: Grid, window) -> dict[str, list[int]]
def template_series(conn, grid: Grid, window, template_ids: Sequence[str]) -> dict[str, list[int]]
def transition_counts(conn, window) -> list[Transition]
def transition_event_ids(conn, pairs: Sequence[tuple[str, str]], window) -> dict[tuple[str, str], list[str]]
def event_refs(conn, event_ids: Sequence[str]) -> list[EventRef]          # ordered by source, line
def sample_events(conn, template_ids: Sequence[str], window, k: int = 3) -> list[EventRef]
def first_event_ids(conn, template_ids: Sequence[str], window) -> dict[str, str]
def event_ids_in_cells(conn, grid: Grid, cells: Sequence[tuple[int, str | None]], window,
                       levels: Sequence[str] | None = None) -> set[str]
```
Bucket index SQL: `CAST(floor((epoch(timestamp) - ?) / ?) AS BIGINT)` with params (grid start epoch seconds, bucket seconds). Lists of ids passed as a parameter list (`template_id = ANY(?)` / `list_contains(?, template_id)`), never interpolated. Window applied as `WHERE {window.where()}` with params. Transitions: `SELECT prev, template_id, count(*) FROM (SELECT template_id, lag(template_id) OVER (PARTITION BY source_file ORDER BY line_start) AS prev FROM events WHERE ...) WHERE prev IS NOT NULL GROUP BY ALL`. `sample_events`: first by (timestamp NULLS LAST, source_file, line_start), last, plus k spread via `ntile(k)` row picks; dedupe by event_id; order by time.

- [ ] Tests on stores built from small text logs: template_stats counts/levels/first-last/sources; time_span with mixed timestamped/untimestamped; empty store (all functions return empty/None without error); window filters; `make_grid` None without timestamps; series lengths equal grid size and sum to event count; template text with a quote `it's` and `100%` survives; transitions per file in line order (no cross-file pairs); event_ids_in_cells with level filter; sample_events returns first and last.
- [ ] Implement; run tests.

### Task 6: Issues

**Files:**
- Create: `src/loglens/analysis/issues.py`
- Test: `tests/test_analysis_issues.py`

**Interfaces (produces):**
```python
PARETO_SHARE = 0.8
@dataclass(frozen=True)
class Issue:
    issue_id: str; title: str; template_ids: tuple[str, ...]; events: int
    by_level: Mapping[str, int]; first_seen: datetime | None; last_seen: datetime | None
    sources: int; top_components: tuple[tuple[str, int], ...]; persistence: float | None
    impact: float; share: float; cumulative_share: float; vital_few: bool
    evidence: tuple[EventRef, ...]
@dataclass(frozen=True)
class IssuesReport:
    issues: tuple[Issue, ...]; total_events: int; candidate_events: int
    vital_few_count: int; window: TimeWindow; notes: tuple[str, ...]
def issue_id_for(template_ids: Sequence[str]) -> str     # "ISS-" + blake2b(min id, 4 bytes) hex
def group_candidates(stats: Sequence[TemplateStats], signals: Signals) -> list[list[TemplateStats]]
def rank_issues(...)  # pure: impact, Pareto flags (tested without a store)
def find_issues(conn, *, window: TimeWindow = TimeWindow(), signals: Signals | None = None,
                min_events: int = 1, bucket: timedelta | None = None,
                evidence: bool = True) -> IssuesReport
def template_issue_map(report: IssuesReport) -> dict[str, str]
```
Pareto: sort by (-impact, -events, issue_id); vital few = shortest prefix with cumulative >= 0.8 (none if total impact 0). `min_events` filters after grouping. Persistence = active buckets of the union of member template series / grid.size (None without grid). Notes: "no events", "N events have no timestamp".

- [ ] Tests: pure ranking (one issue -> share 1 and vital; ties ordered by events then id; zero impact -> no vital few; 80% cut exact boundary); store test: two refused templates merge into one issue with both template ids; INFO-only templates absent; evidence locations point to real lines; no-timestamp store: persistence None and note; empty window: empty report.
- [ ] Implement; run tests.

### Task 7: Anomalies

**Files:**
- Create: `src/loglens/analysis/anomalies.py`
- Test: `tests/test_analysis_anomalies.py`

**Interfaces (produces):**
```python
KINDS = ("spike", "level_shift", "new_pattern", "rare_transition")
@dataclass(frozen=True)
class Anomaly:
    kind: str; score: float; raw_score: float; start: datetime | None; end: datetime | None
    series: str          # "total", "errors", "level:WARN", "template", "transition", "pattern"
    template_ids: tuple[str, ...]; issue_id: str | None; summary: str
    buckets: tuple[int, ...]; evidence: tuple[EventRef, ...]
@dataclass(frozen=True)
class AnomalyReport:
    anomalies: tuple[Anomaly, ...]; grid: Grid | None; span: Span; window: TimeWindow
    baseline_until: datetime | None; notes: tuple[str, ...]
    def by_kind(self, kind: str) -> list[Anomaly]: ...
def find_anomalies(conn, *, window=TimeWindow(), signals=None, bucket=None,
                   baseline_until=None, kinds: Sequence[str] = KINDS) -> AnomalyReport
def predicted_event_ids(conn, report: AnomalyReport) -> set[str]
```
Rules from the spec plus adjustment 2. Rare transition: outgoing totals per a; for a with total >= 100 and top successor share >= 0.9, flag b with count/total < 0.001; evidence: up to 5 b-events (`transition_event_ids` then `event_refs`). Spike anomalies: consecutive flagged buckets of the same series merge into one anomaly (start = first bucket start, end = last bucket end, raw score = max z capped at 50). Level shift summary: "total volume x2.0 down at 12:00". Scores normalized per kind. Sorted by kind order then -score.

- [ ] Tests on small built stores and with monkeypatched aggregates where simpler: spike found; level shift found; new pattern found (and template present in baseline is not new); rare transition in a strict cycle with one deviation; random interleaving gives no rare transition; skip notes for < 20 buckets and no timestamps (rare_transition still runs); `kinds` filter; `predicted_event_ids` contains the burst events and the deviating event, not background.
- [ ] Implement; run tests.

### Task 8: Compare

**Files:**
- Create: `src/loglens/analysis/compare.py`
- Test: `tests/test_analysis_compare.py`

**Interfaces (produces):**
```python
MIN_RATIO = 2.0; MAX_P = 0.01
@dataclass(frozen=True)
class IssueChange:
    title: str; baseline: Issue | None; target: Issue | None
    baseline_rate: float; target_rate: float; ratio: float; p_value: float | None
@dataclass(frozen=True)
class SideSummary:
    events: int; error_events: int; hours: float | None
    @property
    def error_rate(self) -> float: ...
@dataclass(frozen=True)
class CompareReport:
    new: tuple[IssueChange, ...]; gone: tuple[IssueChange, ...]; changed: tuple[IssueChange, ...]
    baseline: SideSummary; target: SideSummary; notes: tuple[str, ...]
def rate_test(c1: int, t1: float, c2: int, t2: float) -> float   # two-sided binomtest p
def compare(baseline_conn, target_conn, *, baseline_window=TimeWindow(),
            target_window=TimeWindow(), signals=None) -> CompareReport
```
Matching: `match_templates(baseline (id, text), target (id, text))`, then target issue -> baseline issue holding the matched template. Durations in hours from each span (min 1 second); without timestamps on either side use 1.0 each and add a note. Changed: ratio = max(r1, r2)/max(min(r1, r2), tiny) >= 2 and p < 0.01; sorted by p then title. New sorted by impact.

- [ ] Tests: `rate_test` (equal rates -> p ~ 1; 10 vs 100 over equal time -> p < 0.01); split mode on one store: new pattern after split appears in `new`; issue only before split in `gone`; burst after split in `changed`; empty target side: all baseline issues gone; no-timestamp note.
- [ ] Implement; run tests.

### Task 9: CLI commands

**Files:**
- Create: `src/loglens/cli/analysis_cmd.py`
- Modify: `src/loglens/cli/__init__.py` (import `analysis_cmd`)
- Test: `tests/test_cli_analysis.py`

Commands (Typer, registered on `app`):
- `issues STORE [--top 20] [--since] [--until] [--min-events 1] [--signals FILE] [--json]`
- `anomalies STORE [--bucket 5m] [--since] [--until] [--baseline-until] [--kind K]... [--signals FILE] [--json]`
- `compare BASELINE [TARGET] [--split T] [--signals FILE] [--json]`

Helpers: `parse_time(text) -> datetime` (ISO 8601, naive = UTC, `Z` accepted; bad -> fail), `open_store(path)` (StoreError -> fail), `--json` prints `json.dumps(jsonable(report), indent=2)` through `typer.echo`. Rich: issues table + summary line "N of M issues account for X% of impact"; anomalies one table per kind + notes; compare sections.

- [ ] Tests (CliRunner on a built store): each command exits 0 with expected text; `--json` parses and has the expected keys; missing store -> exit 1 one-line message; compare with neither/both TARGET and `--split` -> exit 1; bad `--since` -> exit 1; bad `--kind` -> exit 1; bad `--bucket` -> exit 1.
- [ ] Implement; run tests; `uv run ruff check`, `uv run mypy src`.
- [ ] Milestone commit: "Phase 4 (part 1): recurring issues, anomalies and compare".

### Task 10: Planted-issue fixtures and scenario tests

**Files:**
- Create: `tests/fixtures/make_analysis_fixtures.py`, generated `tests/fixtures/analysis/{scenario,background}/{app,worker}.{log,jsonl}`
- Test: `tests/test_analysis_scenario.py`

Generator (seed 4, start 2026-03-01T00:00:00Z, 24 h): `app` stream: per minute `randint(15, 25)` INFO events (halved from 12:00 in scenario) chosen among 4 INFO templates; WARN "Cache miss rate high for region <name>" every 10 min; ERROR "Payment gateway timeout after <n> ms" every hour plus 200 events 06:30:00-06:34:59 (scenario); ERROR "Connection to <ip> refused by peer" and "... refused by remote peer" 30 each spread evenly (both twins); ERROR "Disk quota exceeded on volume /data/<n>" every 2 min from 18:00 (scenario). `worker` stream: 2,000 jobs evenly over 24 h, each "Job <n> started", "Job <n> step load done", "Job <n> finished"; in scenario job 1500 has "Job <n> step load skipped from cache" instead of "load done". Rendered as free text `2026-03-01T06:30:12.345Z ERROR [payments] message` and as JSON lines `{"time":..., "level":..., "service":..., "message":...}`. Background twin = no burst, no halving, no disk quota, no deviation.

- [ ] Tests (stores built once per module via fixtures): top issue is the payment timeout and vital few holds it; refused templates merged into one issue; spike at 06:30 in template and errors series; level shift on total at 12:00 (+/- 1 bucket), down; disk quota is a new pattern starting 18:00; rare transition targets "skipped from cache"; background twin: zero spikes, zero level shifts, zero rare transitions; universality: free text vs JSON give same issue titles (in order) and same anomaly (kind, start) sets.
- [ ] Run; fix detector bugs only with general reasoning (never threshold tuning).

### Task 11: Anomaly evaluation on BGL

**Files:**
- Create: `benchmarks/anomaly_eval.py`; outputs `benchmarks/ANOMALY_RESULTS.md`, `benchmarks/anomaly_history.json`
- Test: `tests/test_anomaly_eval.py` (smoke, tiny labeled sample written in tmp_path, no network)

Functions: `strip_labels(src: Path, dst: Path) -> list[bool]` (streamed; index = line number - 1; True = anomalous), `predictions_by_detector(conn, report) -> dict[str, set[str]]`, `line_flags(conn, event_ids) -> set[int]` (lines covered by those events), `score(pred_lines, labels) -> Metrics(tp, fp, fn, precision, recall, f1)`, baselines (ERROR/FATAL events; issue-candidate events; random expected: precision = base rate, recall = base rate... i.e. predicting each line with probability p = base rate), `main(argv)` with `--full`, `--check`. Full mode downloads `https://zenodo.org/records/8196385/files/BGL.zip?download=1` to `data/bgl_full/` streaming, extracts `BGL.log`.

- [ ] Smoke test on a 60-line synthetic labeled file: script runs end to end in tmp dir, writes results with all detector rows and baselines.
- [ ] Run on BGL_2k; record numbers; write the honest reading in `docs/analysis.md`. Optionally run `--full` if download succeeds in reasonable time.

### Task 12: Docs, checks, commit

- [ ] `docs/analysis.md` (issue model, impact, Pareto, detectors, compare, evaluation), `docs/decisions.md` D21-D26, `CHANGELOG.md`, `CLAUDE.md` status, `docs/tasks.md` checkboxes, README roadmap line if present.
- [ ] `uv run ruff check`, `uv run ruff format --check` (if used in CI), `uv run mypy src`, `uv run pytest`.
- [ ] Commit "Phase 4: Analysis without AI".
