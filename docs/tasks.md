# Task list

Working list for completing LogLens (phases 1 to 7). Updated as work progresses, so it
survives context resets together with the "Current status" section of `CLAUDE.md`.
Legend: `[x]` done, `[~]` in progress, `[ ]` to do.

## Phase 1: Template mining and the full parse pipeline
- [x] 1.0 Generic parser for free text (timestamp / level / component extraction). Missing from the repo although listed as done; built first because the pipeline falls back to it.
- [x] 1.1 Variable masking with typed placeholders, YAML-configurable, values kept in order
- [x] 1.2 Drain3 template mining: persisted state, stable `template_id`, single-placeholder merge, confidence from match quality
- [x] 1.3 Timestamp normalization (UTC, `--timezone`, year inference) and level normalization
- [x] 1.4 Pipeline: ingest -> split -> detect -> parse (+ generic fallback) -> mask -> mine -> unified events
- [x] 1.5 Parquet output (exact schema, batches, partitioned by source file) + DuckDB file; process pool across files
- [x] 1.6 CLI `loglens parse` (summary) and `loglens query`
- [x] 1.7 Tests: known-template fixtures, end-to-end mixed folder (JSON + syslog + free text + stack traces + zip)
- [x] 1.8 Checks, CHANGELOG, CLAUDE.md status, commit "Phase 1: ..."

## Phase 2: Benchmark and robustness
- [ ] 2.1 Loghub 2k download (data/loghub, gitignored), grouping/parsing accuracy, timestamp/level rates, throughput; RESULTS.md + JSON history; regression check
- [ ] 2.2 Analyze 3 worst datasets, general fixes only, before/after
- [ ] 2.3 tests/torture/ (encodings, invalid UTF-8, NUL, 10 MB line, truncation, time going backwards, DST, missing timestamps, 10k files, 1M identical lines, all-unique lines, ANSI, rotation sets)
- [ ] 2.4 Hypothesis fuzz test through the whole pipeline
- [ ] 2.5 Checks, docs, commit

## Phase 3: Teaching LogLens any format
- [ ] 3.1 User format plugins (~/.loglens/formats, --formats-dir, override, JSON Schema validation)
- [ ] 3.2 `loglens formats list|test|new`
- [ ] 3.3 LLM layer: `ai/llm.py` interface, GeminiClient (google-genai), FakeClient, cache, backoff, throttle, token usage, --no-ai, `loglens doctor`
- [ ] 3.4 STOP: ask the user to confirm GEMINI_API_KEY before the first real call
- [ ] 3.5 AI format learner (`ai/format_learner.py`, `loglens learn`)
- [ ] 3.6 Three exotic synthetic formats + docs/ai-parser.md
- [ ] 3.7 Checks, docs, commit

## Phase 4: Analysis (no AI)
- [ ] 4.1 Recurring issues (+ optional embeddings/HDBSCAN), impact score doc, `loglens issues` with Pareto
- [ ] 4.2 Anomalies (volume MAD + ruptures, new patterns, sequence), `loglens compare`
- [ ] 4.3 Anomaly evaluation on labeled Loghub data, honest precision/recall
- [ ] 4.4 Synthetic planted-issue tests; checks, docs, commit

## Phase 5: AI investigator and reports
- [ ] 5.1 Investigator (function calling, read-only tools, citations validated, tool-call cap)
- [ ] 5.2 Evaluation: 20 questions, scoring script, throttled + cached
- [ ] 5.3 Reports (md/html, Plotly, --no-ai)
- [ ] 5.4 Checks, docs, commit

## Phase 6: Product
- [ ] 6.1 FastAPI backend (projects, uploads, jobs + SSE, endpoints, tests)
- [ ] 6.2 Web app (React + TS + Vite + Tailwind), demo mode
- [ ] 6.3 Packaging extras, Dockerfile, compose, CI image build, security review
- [ ] 6.4 docs/deploy.md; STOP before anything needing the user's hosting account
- [ ] 6.5 Checks, docs, commit

## Phase 7: Showcase documentation
- [ ] 7.1 README for recruiters, docs/architecture.md, CONTRIBUTING.md
- [ ] 7.2 Final summary
