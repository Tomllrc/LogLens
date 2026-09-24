# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Step 0: `CLAUDE.md` with project goal, architecture, unified event schema, stack, and coding rules.
- Step 1: project scaffold — `pyproject.toml` (uv, hatchling, src layout), core dependencies (Polars, DuckDB, PyArrow, Typer, Rich), dev tooling (ruff, mypy strict on `src/`, pytest), empty `loglens` package with a smoke test, `.gitignore`, `README.md`.
