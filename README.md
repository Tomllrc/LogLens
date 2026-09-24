# LogLens

[![CI](https://github.com/Tomllrc/LogLens/actions/workflows/ci.yml/badge.svg)](https://github.com/Tomllrc/LogLens/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> Status: early development. Nothing below the scaffold works yet. See [CHANGELOG.md](CHANGELOG.md).

## What it is

LogLens is an open-source, AI-powered log investigation tool. It ingests any log (any format, any size, any system) and turns it into a unified table of structured events. It then detects recurring issues and anomalies, and answers plain-language questions with citations to the exact log lines.

## Why

_TODO: the problem with investigating heterogeneous logs today, and what LogLens does differently._

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/Tomllrc/LogLens.git
cd LogLens
uv sync
uv run loglens --version
```

Development checks:

```
uv run ruff check
uv run mypy src
uv run pytest --cov
```

## Architecture

Every stage after parsing sees only the unified event schema, so analysis never depends on a specific log format.

```
ingest -> split -> detect -> parsers -> schema (Parquet + DuckDB) -> analysis / ai -> CLI / api / dashboard
```

| Package | Role |
|---|---|
| `loglens.ingest` | Files, folders, archives, stdin, encoding detection, chunked streaming |
| `loglens.split` | Group multiline records (stack traces) into single events |
| `loglens.detect` | Score candidate formats for each file |
| `loglens.parsers` | Known-format, generic (Drain), and AI-taught YAML parsers |
| `loglens.schema` | Unified event schema |
| `loglens.analysis` | Recurring-issue clustering, anomaly detection, incident reports |
| `loglens.ai` | Plain-language investigator with cited answers |
| `loglens.api` | HTTP API (FastAPI) |

_TODO: architecture diagram and schema reference (see `docs/`)._

## Benchmarks

_TODO: parsing throughput, memory use on larger-than-RAM files, and template-mining accuracy (see `benchmarks/`)._

## Roadmap

- [x] Project scaffold, CLI entry point, CI
- [x] Ingestion (files, folders, archives, stdin, streaming)
- [x] Record splitting
- [x] Format detection
- [ ] Parsers: known formats, generic + Drain, AI-taught plugins
- [ ] Unified event table (Parquet + DuckDB)
- [ ] Analysis: clustering, anomalies, incident reports
- [ ] AI investigator
- [ ] API and web dashboard

## License

[MIT](LICENSE)
