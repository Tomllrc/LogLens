# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Step 0: `CLAUDE.md` with project goal, architecture, unified event schema, stack, and coding rules.
- Step 1: project scaffold — `pyproject.toml` (uv, hatchling, src layout), core dependencies (Polars, DuckDB, PyArrow, Typer, Rich), dev tooling (ruff, mypy strict on `src/`, pytest), empty `loglens` package with a smoke test, `.gitignore`, `README.md`.
- Step 1b: completed scaffold.
  - `loglens` CLI entry point (Typer) with `--version` / `-V` and a placeholder `parse` command (exits 1, "not implemented yet").
  - Pipeline subpackages: `ingest`, `split`, `detect`, `parsers`, `schema`, `analysis`, `ai`, `api`.
  - Folders `tests/fixtures/`, `benchmarks/`, `docs/`.
  - Dev dependencies `pytest-cov` and `hypothesis`; coverage configured (branch coverage on `loglens`).
  - GitHub Actions CI: ruff, mypy, and pytest with coverage on Python 3.11 and 3.12, for every push and PR.
  - MIT `LICENSE`; README skeleton (What it is, Why, Quick start, Architecture, Benchmarks, Roadmap).
  - `.gitignore` now excludes `data/`, `*.parquet`, `.env`, `coverage.xml`.
  - CLI tests, including a smoke test that runs the installed `loglens --version` script.
- Step 2: ingestion layer (`loglens.ingest`).
  - `iter_lines(inputs, options=, report=)` lazily yields `Line(source_file, line_number, text)` from files, recursive folders, and stdin (`-`).
  - Transparent `.gz`, `.bz2`, `.zip`, `.tar` (and `.tar.gz`/`.tar.bz2`) at any nesting depth, detected by magic bytes rather than extension. Members are named `archive.zip!/inner/file.log`. Zips from non-seekable streams are spooled to a temp file (disk, not RAM); tars are streamed.
  - Binary files skipped (NUL byte or >30% control characters in the first 8 KB).
  - Encoding per file: BOM (UTF-8/16/32), BOM-less UTF-16 heuristic, strict UTF-8, then charset-normalizer, with exact ties broken toward cp1252; fallback UTF-8 with `errors="replace"` plus a warning.
  - `\n`, `\r\n`, `\r` normalized, including across read boundaries. Only those three split lines (not U+2028, `\x0b`, etc.).
  - Constant memory: streaming decode; lines longer than `max_line_chars` (default 1,000,000) are yielded in pieces with the same line number, with a warning.
  - `IngestOptions`: include/exclude globs (matched on full path and basename; filter discovered files, not explicit ones; exclude prunes directories), `max_line_chars`, `max_archive_depth`.
  - `IngestReport`: files read, files skipped with reason, warnings, bytes read, encoding per file.
  - Never raises on bad input: missing, unopenable, corrupt or truncated files and archives, encrypted zip members are all reported.
  - Fixtures in `tests/fixtures/ingest/` (UTF-8, Latin-1, UTF-16 BOM, CRLF, nested zip, gz, binary, empty), generated deterministically by `tests/fixtures/make_ingest_fixtures.py`; `.gitattributes` keeps them byte-exact.
  - New dependency: `charset-normalizer`.
  - 89 new tests, including Hypothesis round-trip properties and tracemalloc memory tests (40 MB file, 24 MB gzip, 24 MB single line: peak < 4 MiB).
- Step 3: record splitting (`loglens.split`).
  - `iter_records(lines, options=, report=)` lazily groups ingested lines into `Record(source_file, line_start, line_end, text)`; records never span files.
  - Per-file record-start signature learned from the first `sample_lines` (default 1000) lines. Candidates, most specific first: `timestamp` (ISO, day/month-first, syslog, Apache, RFC 2822, time-only, glog, epoch; optional leading bracket), `level`, `json` (`{` at column 0), `bracket` (`[...]`, `<PRI>`).
  - Scoring: a candidate must match at least one line and either explain ≥ 80% of the sample (starts plus continuation-looking lines: indented, `at `, `Caused by:`, `... N more`, `Traceback`, exception names, closing braces) or cover ≥ 30% of it (admits unindented multi-line SQL). Best (explained, coverage) wins; ties go to the more specific signature.
  - Fallback without a signature: `unindented` (only unmistakable continuations attach, so uniform files give one record per line), or `every-line` when most of the sample is indented.
  - Records are capped at `max_record_lines` (default 500): overflow starts a new record, counted per file in `SplitReport.warnings` and logged via `logging`.
  - Pieces of over-long lines (same line number from ingestion) are rejoined into one line.
  - `SplitReport`: chosen signature per file, warnings.
  - 85 new tests: Java and Python (chained) tracebacks, every-line logs, no-timestamp files, pretty-printed JSON and JSON lines, multi-line SQL, orphan continuation lines, the giant-record cap, laziness on an endless input, ingest integration, and a Hypothesis property (records partition the input exactly, never exceed the cap). About 1.4 M lines/s.
