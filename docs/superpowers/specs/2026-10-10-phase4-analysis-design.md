# Phase 4: Analysis without AI (design)

Date: 2026-10-10. Status: approved in conversation, pending written-spec review.
Covers tasks 4.1 to 4.4 of `docs/tasks.md` in one spec and one plan.

## Goal

Turn a LogLens store (the unified event table) into three answers an engineer
investigating an incident needs, deterministically and without AI:

1. **Which recurring problems matter most?** (`loglens issues`, with a Pareto view)
2. **What is unusual, and when?** (`loglens anomalies`)
3. **What changed between two runs or two periods?** (`loglens compare`)

Every claim cites real events (`event_id` and `source_file:line_start-line_end`).
The anomaly detectors are evaluated honestly on labeled data (BGL), against trivial
baselines.

## Decisions taken while brainstorming

| Topic | Decision |
|---|---|
| Scope | One spec for all of Phase 4 |
| Issue candidates | Level (WARN/ERROR/FATAL) **or** UNKNOWN + a failure keyword from `signals.yaml` |
| Grouping | Lexical merge of templates; no embeddings/HDBSCAN (possible future extra) |
| Change points | `ruptures` as a **base** dependency (brings numpy + scipy) |
| Evaluation data | BGL_2k by default, opt-in full BGL (`--full`) |
| Architecture | DuckDB aggregates, then pure Python over the small results, computed on demand (nothing stored at parse time) |

## Constraints (from CLAUDE.md)

- Analysis sees only the unified event schema and catalog tables, never a format.
- Never load a whole log into memory: aggregation runs in DuckDB, Python only gets
  bounded aggregates.
- Never crash on bad input; empty or timestamp-less stores give empty or partial
  reports with notes.
- Type hints, mypy strict, docstrings on public functions, tests for every module,
  CHANGELOG at every step.

## Architecture

```
store.connect(dir)  ->  analysis.aggregates  ->  pure analysis functions  ->  report dataclass  ->  CLI (Rich / --json)
   (read-only)          (the only SQL)           (issues, anomalies, compare)
```

New modules in `src/loglens/analysis/`:

| Module | Responsibility |
|---|---|
| `aggregates.py` | The only module that runs SQL. Returns small frozen dataclasses. |
| `signals.py` + `signals.yaml` | Issue-candidate test and severity weights. |
| `grouping.py` | Lexical merge of templates into groups (union-find). |
| `issues.py` | `find_issues(...) -> IssuesReport`: grouping, impact, Pareto, evidence. |
| `timeseries.py` | Bucket choice, robust z-score, change points (`ruptures`). |
| `anomalies.py` | `find_anomalies(...) -> AnomalyReport`, event-level predictions. |
| `compare.py` | `compare(...) -> CompareReport`. |

CLI: `src/loglens/cli/analysis_cmd.py` registers `issues`, `anomalies`, `compare`.
Evaluation: `benchmarks/anomaly_eval.py`. User docs: `docs/analysis.md`.

### Shared types

- `TimeWindow(since: datetime | None, until: datetime | None)`: applied in SQL
  (`timestamp >= since AND timestamp < until`). When a window is set, events without a
  timestamp are excluded and the report counts them.
- `EventRef(event_id, source_file, line_start, line_end, timestamp, message)`: evidence.
- Every report has `notes: list[str]` (skipped detectors, untimestamped events, ...),
  and `to_dict()` for `--json` (datetimes as ISO 8601 UTC strings).

## Module design

### `aggregates.py`

All functions take a DuckDB connection from `store.connect()` (or one connected to
another store for `compare`) and a `TimeWindow`.

- `template_stats(conn, window) -> list[TemplateStats]`: per `template_id`: template
  text, total events, counts per normalized level, first/last timestamp, distinct
  `source_file` count, top 3 components with counts, timestamped event count.
- `bucket_counts(conn, bucket, window, template_ids=None, levels=None) -> Series`:
  counts per time bucket (`time_bucket` in DuckDB), dense (missing buckets = 0),
  either total, per level, or per template id.
- `transition_counts(conn, window) -> list[Transition]`: per source file in
  `line_start` order, consecutive `(prev_template_id, template_id)` pairs counted with
  `LAG ... OVER (PARTITION BY source_file ORDER BY line_start)` and `GROUP BY` pair.
- `transition_events(conn, pairs, window, limit) -> list[EventRef]`: the events at the
  b-side of the given pairs, capped at `limit` per pair.
- `sample_events(conn, template_ids, window, k) -> list[EventRef]`: first, last, and up
  to `k` events spread over time (by `ntile` over timestamp, else over line order).
- `event_ids_in_cells(conn, bucket, cells, window, levels=None) -> list[str]`: event ids
  in a bounded list of `(bucket_start, template_id | None)` cells (`None` = any
  template), optionally restricted to levels. Used for event-level predictions.
- `first_event_ids(conn, template_ids, window) -> dict[str, str]`: first event id per
  template (new patterns).
- `time_span(conn, window) -> (min_ts, max_ts, timestamped, untimestamped)`.

Memory is bounded by the number of templates, buckets (<= 500 per series by design),
and transition pairs, never by the number of events.

### `signals.py` and `signals.yaml`

- `signals.yaml` (packaged next to `masking.yaml`) holds `failure_keywords` (regexes,
  matched whole-word and case-insensitive on the template text) and `level_weights`.
- Default keywords: `error`, `errors`, `fail`, `failed`, `failure`, `failing`,
  `exception`, `timeout`, `timed out`, `refused`, `denied`, `fatal`, `panic`, `abort`,
  `aborted`, `corrupt`, `corrupted`, `unreachable`, `crash`, `crashed`, `killed`,
  `segfault`, `unavailable`.
- Default weights: FATAL 10, ERROR 5, WARN 1, keyword-UNKNOWN 2. Other levels 0.
- A template is a candidate when it has any WARN/ERROR/FATAL event, or any UNKNOWN
  event and its text matches a keyword. INFO/DEBUG/TRACE-only templates are never
  candidates (levels beat words when they exist).
- `load_signals(path: Path | None) -> Signals`. Override via `--signals FILE` on
  `issues` and `anomalies`. An invalid file raises `SignalsError` naming the file; the
  CLI turns it into exit code 2.

### `grouping.py`

- `constant_tokens(template) -> frozenset[str]`: whitespace tokens with placeholders
  (`<...>` typed placeholders and `<*>`) removed, lowercased, trailing punctuation
  stripped.
- `group_templates(templates: Sequence[tuple[str, str]]) -> list[list[str]]`: input is
  `(template_id, template)`; two templates join when the Jaccard similarity of their
  constant tokens is >= **0.8** and both have >= **3** constant tokens; union-find makes
  it transitive. Output is deterministic (groups sorted by smallest id, members
  sorted). Only candidates are grouped. Candidate pairs come from an inverted index of
  constant tokens (pairs sharing at least one token), which gives exactly the same
  result as a full pairwise pass (Jaccard >= 0.8 implies a shared token) without the
  quadratic cost on stores with many templates.
- `match_templates(a, b) -> dict[str, str]`: maps template ids of store A to store B,
  exact id first, then the same Jaccard rule (best match, ties by id). Used by
  `compare`.

### `issues.py`

`find_issues(conn, *, window, signals, min_events=1) -> IssuesReport`

- `Issue` fields: `issue_id` (`ISS-` + first 8 hex chars of blake2b of the group's
  smallest `template_id`), `title` (the member template with the most events),
  `template_ids`, `events`, `events_by_level`, `first_seen`, `last_seen`, `sources`,
  `top_components`, `persistence` (share of time buckets with >= 1 event, `None` when
  there are no timestamps), `impact`, `share`, `cumulative_share`, `vital_few: bool`,
  `evidence: list[EventRef]` (first, last, up to 3 spread samples).
- Impact: `sum(weight[level] * events[level])`, with UNKNOWN events weighted only when
  the template matched a keyword.
- Pareto: issues sorted by impact (ties: events, then `issue_id`); `share = impact /
  total_impact`; `vital_few` marks the shortest prefix whose cumulative share reaches
  0.8. Zero total impact gives no vital few.
- `IssuesReport`: `issues`, `total_events`, `candidate_events`, `vital_few_count`,
  `notes`.
- Persistence uses the same automatic bucket size as the anomaly detectors.

### `timeseries.py`

- `choose_bucket(span) -> timedelta`: smallest of 1s, 10s, 1m, 5m, 15m, 1h, 6h, 1d
  giving <= 500 buckets.
- `robust_z(values) -> list[float]`: `(x - median) / (1.4826 * MAD)`; if MAD is 0, uses
  `1.2533 * mean absolute deviation`; if that is also 0, returns 0 for values equal to
  the median and `inf` otherwise (the absolute-count floor below then decides).
- `spikes(values, z=5.0, min_excess=3) -> list[int]`: indexes with z >= threshold and
  `value >= median + min_excess`.
- `change_points(values, min_size=3, min_ratio=2.0) -> list[ChangePoint]`: `ruptures`
  PELT, model `l2`, penalty `3 * log(n) * sigma^2` where sigma is the robust standard
  deviation of first differences divided by sqrt(2); keeps a change only if
  `max(before, after) / max(min(before, after), 0.5) >= min_ratio` and both segments
  have >= `min_size` buckets. Series shorter than 20 points return nothing.

### `anomalies.py`

`find_anomalies(conn, *, window, signals, bucket=None, baseline_until=None, kinds=ALL) -> AnomalyReport`

`Anomaly(kind, score, start, end, template_ids, issue_id, summary, evidence)`.
Kinds and rules:

1. `spike`: `spikes()` on the total series, the ERROR+FATAL series, and the series of
   the 50 templates with the most timestamped events. Raw score = z (capped at 50).
2. `level_shift`: `change_points()` on the total series and each level's series. Raw
   score = log2 of the ratio.
3. `new_pattern`: templates whose first event comes after the baseline cutoff
   (`baseline_until`, default: start + 20% of the time span). Candidates first. Raw
   score = signal weight of the template's dominant level + 1 (so non-candidates still
   appear, ranked last). Evidence: the first occurrence.
4. `rare_transition`: pairs a->b with `P(b|a) < 0.001` and `count(a) >= 100`. Raw
   score = `-log10(P(b|a))`. Evidence: up to 5 b-events.

Scores are normalized to 0-1 within each kind (divide by the kind's max raw score).
The report lists anomalies grouped by kind, sorted by score, and links each anomaly to
an `issue_id` when its template belongs to an issue.

**Event-level prediction** (`predicted_event_ids(report) -> set[str]`, computed with
bounded queries): an event is predicted anomalous when it is in a flagged
(bucket, template) cell of a template spike, in a flagged bucket of the ERROR+FATAL
spike series and has level ERROR/FATAL, is the first occurrence of a new pattern, or
is a b-event of a rare transition. Total-series spikes and level shifts are reported
but do not mark individual events (they say "when", not "which line").

Time-based detectors are skipped, with a note, when there are fewer than 20 buckets
or no timestamps. `rare_transition` needs no timestamps.

### `compare.py`

`compare(baseline_conn, target_conn, *, baseline_window, target_window, signals) -> CompareReport`

- Two modes from the CLI: two stores (`compare <baseline> <target>`), or one store
  split in time (`compare <store> --split TIMESTAMP`, baseline = before, target = at
  or after).
- Issues are computed on both sides with `find_issues`, then matched through
  `match_templates` (exact template id, then the lexical rule).
- `new`: issues only in target. `gone`: issues only in baseline. `changed`: both
  sides, rate per hour differs by >= 2x and a two-sided exact Poisson rate test
  (conditional binomial, `scipy.stats.binomtest`) gives p < 0.01. Durations come from
  each side's time span; without timestamps, raw counts are compared and a note says so.
- Also reports overall events, error rate (ERROR+FATAL share) on each side, and its
  change.

## CLI

All three in `cli/analysis_cmd.py`; each prints a Rich table and supports `--json`.

- `loglens issues STORE [--top 20] [--since T] [--until T] [--min-events 1] [--signals FILE] [--json]`.
  Columns: rank, impact, share, cumulative %, title, events, first/last seen, files.
  Summary line: "N of M issues account for X% of impact".
- `loglens anomalies STORE [--bucket 5m] [--since T] [--until T] [--baseline-until T] [--kind K]... [--signals FILE] [--json]`.
  One table per kind: score, time range, summary, issue, evidence location.
- `loglens compare BASELINE [TARGET] [--split T] [--signals FILE] [--json]`. Exactly one
  of TARGET or `--split` is required (otherwise exit 2 with a clear message).
  Sections: new, gone, changed, overall.

Timestamps on the command line accept ISO 8601; naive values are UTC. Errors
(`StoreError`, `SignalsError`, bad timestamps) print one line and exit 2, like
`loglens query`.

## Evaluation (`benchmarks/anomaly_eval.py`)

- Input: `data/loghub/BGL/BGL_2k.log` (default) or the full BGL log with `--full`
  (downloaded to `data/bgl_full/`, gitignored, from the Loghub Zenodo record
  https://zenodo.org/records/8196385, file `BGL.zip`. The download streams to disk;
  if it fails, the script exits with a clear message and changes nothing).
- Labels: the first whitespace token of each line (`-` = normal, otherwise an alert
  category). The script streams the file, stores labels by line number, and writes a
  label-free copy (first token and one space removed) to a temp dir. The pipeline
  parses that copy; the labels never reach the parser.
- Prediction: `predicted_event_ids`, mapped to lines through `line_start..line_end`.
  A record is positive if any of its lines is labeled.
- Metrics: precision, recall, F1, with TP/FP/FN, per detector and for the union.
- Baselines in the same table: every ERROR/FATAL event; every event of an issue
  candidate; random at the base rate (expected values, computed analytically).
- Outputs: `benchmarks/ANOMALY_RESULTS.md` (regenerated), `benchmarks/anomaly_history.json`
  (appended), discussion in `docs/analysis.md`.
- All thresholds in this spec are fixed before the first evaluation run and recorded
  in `docs/decisions.md`. Changing one later needs a general justification, never a
  better BGL score (same rule as D11).

## Testing

- **Unit tests** per module on hand-built inputs: `signals` (keywords, weights,
  overrides, invalid YAML), `grouping` (merge, no-merge on short templates,
  transitivity, determinism, `match_templates`), `timeseries` (bucket choice, robust z
  edge cases, spikes, change points on synthetic step series), `issues` (impact,
  Pareto edge cases: one issue, ties, zero impact), `anomalies` (each kind, scoring,
  skip rules), `compare` (new/gone/changed, Poisson test, no-timestamp mode).
- **Store tests** for `aggregates`: small stores built by running the pipeline into
  `tmp_path`, including an empty store and a store without timestamps.
- **Planted-issue fixtures** from `tests/fixtures/make_analysis_fixtures.py` (fixed
  seed; edit the script, never the files) into `tests/fixtures/analysis/`:
  24 h of background traffic plus an ERROR burst at a known minute, a new error
  pattern from hour 18, total traffic halving at hour 12, one rare transition, one
  issue split across `<IP>` / `<HOST>` templates that must merge into one issue, and
  one dominant issue for Pareto. Tests assert each item is found at the right time.
  A background-only twin must give zero spikes and zero level shifts.
- **Universality**: the same scenario rendered as JSON lines and as free text gives
  the same issues (by title text) and the same anomaly kinds and times.
- **Hypothesis**: grouping is independent of input order; Pareto shares sum to 1;
  constant series give no spikes or change points; robust z is invariant under
  positive scaling and shifting.
- **CLI** tests with `CliRunner`: all three commands, `--json` output parses, error
  exits (missing store, bad `--split`, both TARGET and `--split`).
- The evaluation script gets a smoke test on a tiny labeled sample (no network).

## Error handling

| Situation | Behavior |
|---|---|
| Missing directory / not a store | One-line message, exit 2 |
| Empty store | Empty report, note "no events" |
| No timestamps | Issues work (persistence `n/a`); time detectors skipped with a note |
| Fewer than 20 buckets | Time detectors skipped with a note |
| Invalid `signals.yaml` override | `SignalsError` naming the file, exit 2 |
| Invalid timestamp option | One-line message, exit 2 |
| Huge store | Bounded: 50 template series, 500 buckets, transition evidence capped |

## Dependencies

`uv add ruptures` (pulls numpy and scipy). scipy is also used for the Poisson rate
test. mypy: add overrides for untyped `ruptures` if needed.

## Documentation and bookkeeping

- `docs/analysis.md`: issue model, impact formula, Pareto, each detector with its
  rule and thresholds, compare semantics, evaluation results and their honest reading.
- `docs/decisions.md`: D21 onwards (grouping threshold, impact weights, detector
  thresholds, evaluation protocol, ruptures as base dependency).
- `CHANGELOG.md`, `CLAUDE.md` current status, `docs/tasks.md` checkboxes.
- Final commit: "Phase 4: Analysis without AI".

## Out of scope

- Embeddings / HDBSCAN grouping (documented as a possible `loglens[ml]` extra).
- Session-based sequence models (HDFS block ids); `rare_transition` is the
  format-agnostic sequence signal for now.
- Storing analysis results; everything is computed on demand.
- AI summaries of issues (Phase 5).
