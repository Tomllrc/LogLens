# Analysis without AI: issues, anomalies, compare

Once logs are parsed (`loglens parse <logs> -o out/`), three commands answer the first
questions of an investigation. All three read only the unified event table, so they work
the same on JSON lines, syslog, Apache, a Java stack-trace log or a format learned by AI.
Every finding cites events (`file:line`), and every command takes `--json`.

```bash
loglens issues out/                      # recurring problems, ranked by impact
loglens anomalies out/                   # spikes, level shifts, new patterns, rare transitions
loglens compare out/ --split 2026-03-01T12:00:00Z   # before vs after, in one store
loglens compare yesterday/ today/        # two parse runs
```

The thresholds below are fixed defaults (docs/decisions.md D21-D27). They were set before
the evaluation and are not tuned to any dataset.

## Recurring issues (`loglens issues`)

**Candidates.** A template is a candidate issue when at least one of its events is
WARN, ERROR or FATAL. Logs without levels (UNKNOWN) still get issues: a template with
UNKNOWN events is a candidate when its text contains a failure word (`error`, `failed`,
`exception`, `timed out`, `refused`, `denied`, `panic`, `corrupt`, ...). The list and
weights live in `src/loglens/analysis/signals.yaml`; `--signals my.yaml` replaces them.
A template with only INFO/DEBUG/TRACE events is never an issue, even if it says
"error count=0": when a log has levels, they are more trustworthy than words.

**Grouping.** Drain sometimes splits one problem into several templates ("Connection
to <IP> refused by peer" and "... refused by remote peer"). Candidates whose constant
words (placeholders removed) overlap with a Jaccard similarity of at least 0.8 are one
issue; each must have at least 3 constant words, so short templates such as
"<*> failed" never swallow others. The issue's title is its most frequent template, and
the table says "(+N similar)".

**Impact score.**

```
impact = 10 x FATAL events + 5 x ERROR events + 1 x WARN events
         + 2 x UNKNOWN events (only for keyword candidates)
```

One formula, easy to explain: a fatal error weighs ten warnings. Persistence (share of
time buckets in which the issue appears), time span, files and components are shown next
to it but do not change the score.

**Pareto.** Issues are sorted by impact; `share` is each issue's part of the total and
`cumulative_share` the running sum. The *vital few* are the shortest list of top issues
reaching 80% of all impact, shown in bold with a summary such as
"3 of 37 issues account for 81% of impact".

Options: `--top N` (20), `--since/--until` (ISO 8601, naive = UTC), `--min-events`,
`--signals`, `--json`.

## Anomalies (`loglens anomalies`)

Timestamps are counted in buckets: the smallest of 1s, 10s, 1m, 5m, 15m, 1h, 6h, 1d, 1w,
30d giving at most 500 buckets (`--bucket 5m` to choose). Events without a timestamp are
left out of the time-based detectors, and the report says so; with fewer than 20 buckets
those detectors are skipped rather than guessing.

| Detector | Looks at | Flags |
|---|---|---|
| `spike` | total volume, ERROR+FATAL volume, the 50 busiest templates | a bucket with robust z >= 5 (median and 1.4826 x MAD; mean absolute deviation if the MAD is 0) **and** at least median + 3 events. Adjacent buckets merge into one spike. Scored by excess events over the median. |
| `level_shift` | total volume and each level's volume | a change point (ruptures PELT, l2 cost, penalty 3 log(n) sigma^2) where the median of the segment after is >= 2x or <= 1/2 the median before, both segments >= 3 buckets. Catches "the service went quiet" as well as "errors doubled for good". |
| `new_pattern` | every template | first seen after the baseline period: the first 20% of the time span, or `--baseline-until`. Candidate issues rank first. |
| `rare_transition` | consecutive templates within each file | a -> b where a is followed by the same template >= 90% of the time (at least 100 transitions), and b follows a less than 0.1% of the time: a broken sequence. Needs no timestamps. |

Scores are normalized to 0-1 within each kind (1 = the strongest of that kind). Each
anomaly lists its time, the related issue when there is one, and evidence events.

**Which lines are anomalous?** For the evaluation, a report is turned into flagged
events: the events of a template spike's buckets, the ERROR/FATAL events of an error
spike's buckets, the first event of each new pattern, every occurrence of a rare
transition's second template. Total-volume spikes and level shifts say *when*, not
*which line*, so they flag none.

Options: `--bucket`, `--since/--until`, `--baseline-until`, `--kind K` (repeatable),
`--signals`, `--json`.

## Compare (`loglens compare`)

Two modes, one comparison: two stores (`compare BASELINE TARGET`, e.g. yesterday's run
and today's), or one store cut in time (`compare STORE --split T`: before T vs from T).

Issues are computed on both sides and matched through their templates: the same template
id (ids are hashes of the template text, so they match across stores), else the lexical
rule above, since two runs can generalize a message differently. Then:

- **new**: issues only in the target; **gone**: issues only in the baseline;
- **changed**: on both sides, with a rate per hour at least 2x higher or lower **and** an
  exact Poisson rate test (conditional binomial) at p < 0.01, so a change from 1 to 3
  events is not news but 5 to 60 is. Rates use each side's own time span, so a 1-hour
  window and a 24-hour window compare fairly.

The overall line gives events and the ERROR+FATAL share on each side.

## How it is tested

- Unit tests for every rule (`tests/test_analysis_*.py`), Hypothesis properties (grouping
  ignores input order, constant series raise nothing, robust z ignores scale and shift).
- A planted scenario (`tests/fixtures/make_analysis_fixtures.py`): 24 hours of a web app
  and a job worker with an error burst at 06:30, traffic down to a quarter at 12:00, a new
  error from 18:00, one job skipping a step, and an error split across two templates. All
  five are found at the right time; its clean twin raises no spike, level shift or rare
  transition; the same scenario as free text and as JSON lines gives the same analysis.
  Weakening a rule (Jaccard 0.9, no successor-dominance rule, no minimum excess) makes
  these tests fail.

## Evaluation on labeled data (BGL)

See `benchmarks/ANOMALY_RESULTS.md` for the latest numbers, produced by
`uv run python -I benchmarks/anomaly_eval.py [--full]`.

EVALUATION_PLACEHOLDER
