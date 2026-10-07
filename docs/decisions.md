# Decisions

Judgment calls made while building LogLens, with the reason for each. Newest last.

## D1. The generic parser is built in Phase 1

The brief listed "the generic parser's timestamp/level/component extraction" as done, but
the repository stopped at step 5b (YAML text formats): nothing extracted fields from free
text. Phase 1 needs it (the pipeline falls back to it on low confidence), so it is built
first, in `loglens/parsers/generic.py`.

## D2. New packages: `pipeline`, `store`, and `cli` becomes a package

The pipeline stages of CLAUDE.md are unchanged. Orchestration (`loglens/pipeline/`) and
the unified event table (`loglens/store/`: Parquet writer, DuckDB catalog, read-only
queries) needed homes; the CLI became a package with one module per command group so it
stays a thin layer as commands are added. Entry point unchanged (`loglens.cli:app`).

## D3. Templates are mined from the first line of the message

Stack traces and other continuation lines vary too much to template; the headline of
the event (first line, up to 1,000 characters) is what identifies it. The full message
and raw record are kept in the event.

## D4. `params` = template variables, then named fields

The schema has no column for parser fields (JSON keys, `thread`, `pid`...). They are
appended to `params` as `key=value` after the template's variables, so nothing a parser
extracted is lost and `params[:n]` still aligns with the `n` placeholders of `template`.

## D5. Typed-placeholder-aware Drain similarity

Plain Drain treats `peer` vs `<IP>` as a neutral mismatch, so short messages such as
`Connection reset by peer` / `Connection refused by 10.0.0.9` merged. Masking already
proved those positions hold different kinds of values, so such a pair counts against the
similarity (`TypedDrain`). Templates differing at a single position are still unified by
the merge step. This is a general rule, decided before any benchmark run.

## D6. Mining runs once per distinct masked message, in a deterministic order

Workers parse files in parallel; mining needs one global miner. Distinct masked messages
(usually far fewer than lines) are mined in first-seen order (input index, then record
index), so results do not depend on worker scheduling. The masked -> cluster mapping is
a DuckDB table (spills to disk); only cluster -> template stays in memory.

## D7. Confidence of generic-parser events depends on template quality

Structured and known-format parsers report structural confidence. Events from the
generic parser get `confidence * (0.6 + 0.4 * share of literal tokens in the template)`:
a line whose template is mostly placeholders was understood less well.

## D8. Timestamps: UTC, `--timezone` for naive ones, year inference, no date -> null

Naive timestamps are read in `--timezone` (default UTC; DST handled by zoneinfo, with
`tzdata` shipped for Windows). Year-less timestamps (syslog) take the run's year, or
the year before if that would be more than a day in the future. A time without a date
cannot be placed and is stored as null (`timestamp_raw` keeps it).

## D9. A `<HOST>` placeholder, beyond the briefed list

Host names (`www.example.com:443`, `proxy.cse.cuhk.edu.hk`) are variables just like IPs;
without masking, every host made its own template. Accepted: labels ending in a common
generic TLD, or 3+ labels ending in a two-letter country code (so `main.go` is a file).

## D10. Equal typed placeholders are neutral in Drain's similarity

Refines D5 after the Loghub analysis: `<NUM> normal` and `<NUM> running` shared a
placeholder, which plain Drain counts as similarity, so 2-token events merged. Equal
placeholders now say "same kind of value", not "same event"; only literals are similar.

## D11. Benchmark fixes are general only; Drain's threshold is never tuned

Phase 2 fixes came from real failing examples but each is a rule for any log (headers,
masking, similarity). Per-dataset thresholds or rules keyed to a dataset were ruled out.
Remaining errors are documented in `benchmarks/ANALYSIS.md`.

## D12. drain3 persistence is attached only to load and save

drain3 saves its whole state after every new cluster when a persistence handler is set:
quadratic on logs with many templates (20k unique lines took minutes). The handler is
attached only to load at start and to save once at the end.

## D13. Drain compares a new message with at most 256 clusters of its leaf

Bounded worst case for free text whose first words are random (one leaf fills with
thousands of clusters). Normal logs never reach the cap: benchmark results were
identical with and without it.

## D14. Small files are bundled into tasks; rotation sets are read oldest first

Tasks bundle consecutive small files (up to 8 MB or 256 files), cutting per-task costs
for folders of thousands of tiny files; order is kept, so mining stays deterministic.
Folder discovery orders `app.log.N[.gz]` before `app.log`, highest N first.

## D15. Slow torture tests are a separate marker

Tests over a million lines or ten thousand files take over a minute; they are marked
`slow`, excluded from the default `pytest` run, and run with `pytest -m slow` (CI runs
both).
