"""The DuckDB catalog of an output directory, and read-only querying."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import duckdb

from loglens.schema.events import EVENT_SCHEMA

EVENTS_DIR: Final = "events"
DB_FILE: Final = "loglens.duckdb"
STATE_DIR: Final = "state"
CATALOG_TABLES: Final = ("templates", "files", "warnings", "run_info")


class StoreError(RuntimeError):
    """An output directory is missing or not a LogLens store."""


@dataclass(frozen=True)
class QueryResult:
    """Rows of a query, with column names."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool


def events_glob(directory: Path) -> str:
    """The glob matching every event Parquet file of a store, with forward slashes."""
    return (directory.resolve() / EVENTS_DIR / "*" / "*.parquet").as_posix()


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def events_view_sql(directory: Path, name: str = "events") -> str:
    """SQL creating the events view over a store's Parquet files (empty-safe)."""
    if any((directory / EVENTS_DIR).glob("*/*.parquet")):
        return (
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM "
            f"read_parquet({_sql_string(events_glob(directory))}, hive_partitioning = false)"
        )
    columns = ", ".join(
        f"CAST(NULL AS {_duck_type(field.type)}) AS {field.name}" for field in EVENT_SCHEMA
    )
    return f"CREATE OR REPLACE VIEW {name} AS SELECT {columns} WHERE false"


def _duck_type(arrow_type: object) -> str:
    text = str(arrow_type)
    if text.startswith("timestamp"):
        return "TIMESTAMPTZ"
    if text.startswith("list"):
        return "VARCHAR[]"
    return {"string": "VARCHAR", "int64": "BIGINT", "double": "DOUBLE"}[text]


def connect(directory: Path) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB connection exposing a store read-only.

    The catalog file is attached read-only; its tables and an ``events`` view over the
    Parquet files are exposed unqualified. Writes to the store are impossible through it,
    and it works even after the directory has been moved.
    """
    db_path = directory / DB_FILE
    if not db_path.is_file():
        raise StoreError(f"{directory} is not a LogLens output directory (no {DB_FILE})")
    connection = duckdb.connect()
    connection.execute("SET TimeZone = 'UTC'")  # timestamps are stored in UTC: show them so
    connection.execute(f"ATTACH {_sql_string(db_path.resolve().as_posix())} AS store (READ_ONLY)")
    for table in CATALOG_TABLES:
        connection.execute(f"CREATE VIEW {table} AS SELECT * FROM store.main.{table}")
    connection.execute(events_view_sql(directory))
    return connection


def query(directory: Path, sql: str, *, limit: int = 1000) -> QueryResult:
    """Run SQL against a store and return at most ``limit`` rows."""
    connection = connect(directory)
    try:
        cursor = connection.execute(sql)
        columns = [description[0] for description in cursor.description or []]
        rows = cursor.fetchmany(limit + 1) if columns else []
    finally:
        connection.close()
    return QueryResult(columns, rows[:limit], truncated=len(rows) > limit)
