# LogLens

LogLens is an open-source, AI-powered log investigation tool. It ingests **any** log (any format, any size, any system) and turns it into a unified table of structured events. It then detects recurring issues and anomalies, and lets users ask questions in plain language. Answers cite the exact log lines they rely on.

## Core principle: universality

Every stage after parsing sees only the **unified event schema**. Analysis code must never depend on a specific log format. If a feature needs format-specific knowledge, that knowledge belongs in a parser, not in analysis.

## Architecture (pipeline)

1. **Ingestion**: files, nested folders, `.gz` / `.zip` / `.tar` archives, and stdin. Detect the encoding and stream in chunks so files larger than RAM still work.
2. **Record splitting**: group multiline records (e.g. stack traces) into single events.
3. **Format detection**: for each file, score candidate formats (JSON lines, CSV, logfmt, XML, known text formats, free text).
4. **Parsing**, with three strategies:
   - Known-format parsers.
   - A generic parser: timestamp and level extraction, variable masking, Drain template mining.
   - An AI-taught parser that generates validated YAML parser plugins.
5. **Unified event table**: stored as Parquet and queried with DuckDB.
6. **Analysis**: recurring-issue clustering, anomaly detection, an AI investigator, and incident reports.
7. **Interface**: CLI (Typer), API (FastAPI), web dashboard.

## Unified event schema

| Field | Description |
|---|---|
| `event_id` | Unique identifier of the event |
| `timestamp` | Parsed timestamp, UTC, nullable |
| `timestamp_raw` | Timestamp text as it appears in the log |
| `source_file` | File the event came from (including its path inside an archive) |
| `line_start` | First line of the record in the source |
| `line_end` | Last line of the record in the source |
| `level` | Normalized: `TRACE` / `DEBUG` / `INFO` / `WARN` / `ERROR` / `FATAL` / `UNKNOWN` |
| `level_raw` | Level text as it appears in the log |
| `component` | Emitting component, service, or logger (nullable) |
| `message` | Message text |
| `template_id` | Identifier of the mined message template |
| `template` | Message template with variables masked |
| `params` | Extracted variable values (list of strings) |
| `format_name` | Detected format that produced this event |
| `parser_confidence` | Parser confidence, from 0 to 1 |
| `raw` | Original raw record text |

## Stack

- Python 3.11+, with **uv** for dependency management
- Polars, DuckDB, PyArrow
- Typer and Rich for the CLI
- pytest, ruff, mypy (strict on `src/`)
- src layout: `src/loglens/`

## Coding rules

- Use type hints everywhere. Prefer small pure functions. Write docstrings on public functions.
- Every new module comes with unit tests. **Never delete or weaken a test to make it pass.**
- Stream data with generators and iterators. **Never load a whole log file into memory.**
- **Never crash on bad input.** A malformed line becomes an event with level `UNKNOWN` and a low `parser_confidence`, and is counted in a warnings report.
- Before declaring a task done, run:
  ```
  uv run ruff check
  uv run mypy src
  uv run pytest
  ```
- Update `CHANGELOG.md` at every step.

## Current status

- **Step 0: project context.** `CLAUDE.md` written.
- **Step 1: scaffold.** `pyproject.toml`, empty `src/loglens/` package, smoke test, ruff/mypy/pytest config, `CHANGELOG.md`. No pipeline code yet.
- **Next:** step 2, ingestion (files, folders, archives, stdin, encoding detection, chunked streaming).
