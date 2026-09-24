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
