"""The unified event table: Parquet files plus a DuckDB catalog, in one output directory.

Layout of an output directory::

    events/<source-key>/part-00000.parquet   events (exact schema), one folder per source
    loglens.duckdb                           templates, files, warnings, run_info; events view
    state/drain3.bin                         template miner state (stable templates)
"""

from loglens.store.catalog import (
    DB_FILE,
    EVENTS_DIR,
    STATE_DIR,
    QueryResult,
    StoreError,
    connect,
    query,
)
from loglens.store.writer import PartitionedEventWriter, source_key

__all__ = [
    "DB_FILE",
    "EVENTS_DIR",
    "STATE_DIR",
    "PartitionedEventWriter",
    "QueryResult",
    "StoreError",
    "connect",
    "query",
    "source_key",
]
