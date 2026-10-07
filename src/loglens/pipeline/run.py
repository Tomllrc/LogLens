"""Orchestrate a parse run into an output directory.

1. Discover inputs (files, folder contents, archives, stdin).
2. Stage 1, in parallel across inputs (process pool): parse into staging Parquet files.
3. Mining: distinct masked messages, in a deterministic order (input, then record), go
   through Drain once each; state is persisted, templates merged, ids hashed.
4. Stage 2: staging rows joined with their templates, params recovered, and written as
   the final events, sorted by source file and line, one Parquet folder per source.
5. The DuckDB catalog (templates, files, warnings, run info) is (re)built.

Memory stays bounded: workers stream with fixed-size batches, and the joins, sorts and
aggregations run in DuckDB, which spills to disk.
"""

import os
import shutil
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final

import duckdb
import pyarrow as pa

from loglens import __version__
from loglens.ingest import FileIssue, IngestOptions, IngestReport, discover_inputs
from loglens.parsers.mining import (
    MiningConfig,
    Template,
    TemplateMiner,
    extract_params,
    final_templates,
    literal_share,
    template_id,
)
from loglens.pipeline.options import LOW_CONFIDENCE, ParseOptions
from loglens.pipeline.worker import Task, TaskResult, run_task
from loglens.schema.events import EVENT_SCHEMA
from loglens.schema.levels import ERROR_LEVELS
from loglens.store.catalog import DB_FILE, EVENTS_DIR, STATE_DIR, events_view_sql
from loglens.store.writer import PartitionedEventWriter

STAGING_DIR: Final = "_staging"
STATE_FILE: Final = "drain3.bin"
GENERIC_QUALITY_FLOOR: Final = 0.6
"""A generic-parser event's confidence is scaled between this and 1 by template quality."""

ProgressCallback = Callable[[str, int, int], None]
"""Called with (stage, done, total) as the run advances."""


@dataclass(frozen=True)
class FileSummary:
    """One source file of the run."""

    source_file: str
    format_name: str
    detection_confidence: float
    events: int


@dataclass
class ParseSummary:
    """Totals of a run, for the CLI summary (details are in the store)."""

    output_dir: Path
    inputs: int = 0
    files: list[FileSummary] = field(default_factory=list)
    skipped: list[FileIssue] = field(default_factory=list)
    warnings: int = 0
    failed_inputs: list[tuple[str, str]] = field(default_factory=list)
    records: int = 0
    events: int = 0
    malformed: int = 0
    generic: int = 0
    bytes_read: int = 0
    seconds: float = 0.0
    templates: int = 0
    formats: Counter[str] = field(default_factory=Counter)
    levels: Counter[str] = field(default_factory=Counter)
    low_confidence: int = 0
    top_templates: list[tuple[str, int]] = field(default_factory=list)
    top_error_templates: list[tuple[str, int]] = field(default_factory=list)

    @property
    def records_per_second(self) -> float:
        """Throughput of the whole run."""
        return self.records / self.seconds if self.seconds > 0 else 0.0

    @property
    def low_confidence_share(self) -> float:
        """Share of events parsed below the low-confidence threshold."""
        return self.low_confidence / self.events if self.events else 0.0


def run_parse(
    inputs: Sequence[str | os.PathLike[str]],
    output_dir: Path,
    options: ParseOptions | None = None,
    *,
    progress: ProgressCallback | None = None,
    stdin: BinaryIO | None = None,
    reference: datetime | None = None,
) -> ParseSummary:
    """Parse inputs into ``output_dir`` (events Parquet + DuckDB catalog)."""
    options = options or ParseOptions()
    started = time.perf_counter()
    reference = reference or datetime.now(UTC)
    output_dir.mkdir(parents=True, exist_ok=True)
    staging = output_dir / STAGING_DIR
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    summary = ParseSummary(output_dir)

    discovery = IngestReport()
    items = list(
        discover_inputs(
            inputs,
            options=IngestOptions(include=options.include, exclude=options.exclude),
            report=discovery,
        )
    )
    summary.inputs = len(items)
    summary.skipped.extend(discovery.files_skipped)
    tasks = [
        Task(index, item, staging / f"{index:06d}.parquet", options, reference)
        for index, item in enumerate(items)
    ]
    results = _run_tasks(tasks, options.workers, progress, stdin)
    staged = [r.staging_path.as_posix() for r in results if r.staging_path.is_file()]

    state_file = options.state_file or output_dir / STATE_DIR / STATE_FILE
    connection = duckdb.connect()
    try:
        connection.execute(f"SET temp_directory = '{(staging / 'duckdb_tmp').as_posix()}'")
        templates = _mine(connection, staged, state_file, progress)
        events_dir = output_dir / EVENTS_DIR
        shutil.rmtree(events_dir, ignore_errors=True)
        events_dir.mkdir()
        _write_events(connection, staged, templates, events_dir, progress)
    finally:
        connection.close()

    _collect(summary, results)
    summary.seconds = time.perf_counter() - started
    _build_catalog(output_dir, summary, results, options)
    _summarize_from_store(output_dir, summary)
    shutil.rmtree(staging, ignore_errors=True)
    return summary


def _run_tasks(
    tasks: list[Task],
    workers: int,
    progress: ProgressCallback | None,
    stdin: BinaryIO | None,
) -> list[TaskResult]:
    results: list[TaskResult] = []
    disk_tasks = [t for t in tasks if t.item.path is not None]
    stdin_tasks = [t for t in tasks if t.item.path is None]
    total = len(tasks)

    def done(result: TaskResult) -> None:
        results.append(result)
        if progress:
            progress("parse", len(results), total)

    finished: set[int] = set()
    if workers > 1 and len(disk_tasks) > 1:
        try:
            with ProcessPoolExecutor(max_workers=min(workers, len(disk_tasks))) as pool:
                chunk = max(1, len(disk_tasks) // (workers * 8))
                for result in pool.map(run_task, disk_tasks, chunksize=chunk):
                    finished.add(result.index)
                    done(result)
        except BrokenProcessPool:
            pass  # a worker died (or processes are unavailable): finish in this process
    for task in disk_tasks:
        if task.index not in finished:
            done(run_task(task))
    for task in stdin_tasks:
        done(run_task(task, stdin if stdin is not None else sys.stdin.buffer))
    results.sort(key=lambda r: r.index)
    return results


def _staging_relation(staged: list[str]) -> str:
    files = ", ".join("'" + path.replace("'", "''") + "'" for path in staged)
    return f"read_parquet([{files}])"


def _mine(
    connection: duckdb.DuckDBPyConnection,
    staged: list[str],
    state_file: Path,
    progress: ProgressCallback | None,
) -> dict[int, Template]:
    """Cluster every distinct masked message once, in first-seen order.

    The masked -> cluster mapping goes to the DuckDB table ``masked_clusters`` (it can be
    as large as the input); only cluster -> template stays in memory, bounded by Drain's
    cluster cap.
    """
    miner = TemplateMiner(state_file, MiningConfig())
    connection.execute("CREATE TABLE masked_clusters (masked VARCHAR, cluster BIGINT)")
    if staged:
        reader = connection.cursor()
        cursor = reader.execute(
            f"SELECT masked, min(task_index * 4294967296 + ordinal) AS first_seen "
            f"FROM {_staging_relation(staged)} GROUP BY masked ORDER BY first_seen"
        )
        seen = 0
        while batch := cursor.fetchmany(50_000):
            chunk = pa.table(
                {
                    "masked": [masked for masked, _ in batch],
                    "cluster": [miner.add(masked) for masked, _ in batch],
                }
            )
            connection.register("mined_chunk", chunk)
            connection.execute("INSERT INTO masked_clusters SELECT * FROM mined_chunk")
            connection.unregister("mined_chunk")
            seen += len(batch)
            if progress:
                progress("mine", seen, 0)
        reader.close()
    miner.save()
    return final_templates(miner.templates())


def _write_events(
    connection: duckdb.DuckDBPyConnection,
    staged: list[str],
    templates: dict[int, Template],
    events_dir: Path,
    progress: ProgressCallback | None,
) -> None:
    writer = PartitionedEventWriter(events_dir)
    try:
        if not staged:
            return
        reader = connection.execute(
            f"SELECT s.*, m.cluster FROM {_staging_relation(staged)} AS s "
            "JOIN masked_clusters AS m USING (masked) "
            "ORDER BY s.source_file, s.line_start, s.ordinal"
        ).to_arrow_reader(20_000)
        for batch in reader:
            writer.write(_finalize_batch(batch, templates))
            if progress:
                progress("write", writer.rows_written, 0)
    finally:
        writer.close()


def _template_for(masked: str, cluster: int, templates: dict[int, Template]) -> Template:
    template = templates.get(cluster)
    if template is None:  # evicted from Drain's cluster cache: the message is its own template
        template = Template(template_id(masked), masked, literal_share(masked))
    return template


def _finalize_batch(batch: pa.RecordBatch, templates: dict[int, Template]) -> pa.RecordBatch:
    columns = batch.to_pydict()
    template_ids: list[str] = []
    texts: list[str] = []
    params: list[list[str]] = []
    confidences: list[float] = []
    for masked, cluster, values, fields, generic, confidence in zip(
        columns["masked"],
        columns["cluster"],
        columns["mask_values"],
        columns["field_params"],
        columns["generic"],
        columns["parser_confidence"],
        strict=True,
    ):
        template = _template_for(masked, cluster, templates)
        template_ids.append(template.template_id)
        texts.append(template.text)
        params.append([*extract_params(masked, values, template.text), *fields])
        if generic:
            quality = GENERIC_QUALITY_FLOOR + (1 - GENERIC_QUALITY_FLOOR) * template.literal_share
            confidence = round(confidence * quality, 3)
        confidences.append(confidence)
    columns["template_id"], columns["template"], columns["params"] = template_ids, texts, params
    columns["parser_confidence"] = confidences
    return pa.RecordBatch.from_pydict(
        {name: columns[name] for name in EVENT_SCHEMA.names}, schema=EVENT_SCHEMA
    )


def _collect(summary: ParseSummary, results: Iterable[TaskResult]) -> None:
    for result in results:
        summary.records += result.records
        summary.events += result.events
        summary.malformed += result.malformed
        summary.generic += result.generic
        summary.bytes_read += result.bytes_read
        summary.skipped.extend(result.skipped)
        summary.warnings += len(result.warnings)
        if result.error is not None:
            summary.failed_inputs.append((result.source, result.error))
        for source, detection in result.detections.items():
            events = result.events_per_source.get(source, 0)
            summary.files.append(
                FileSummary(source, detection.format_name, detection.confidence, events)
            )
            summary.formats[detection.format_name] += 1


def _build_catalog(
    output_dir: Path, summary: ParseSummary, results: list[TaskResult], options: ParseOptions
) -> None:
    db_path = output_dir / DB_FILE
    db_path.unlink(missing_ok=True)
    connection = duckdb.connect(str(db_path))
    try:
        connection.execute(events_view_sql(output_dir))
        connection.execute(
            """
            CREATE TABLE templates AS
            SELECT template_id, any_value(template) AS template, count(*) AS events,
                   count(*) FILTER (WHERE level IN ('ERROR', 'FATAL')) AS error_events,
                   min(timestamp) AS first_seen, max(timestamp) AS last_seen,
                   count(DISTINCT source_file) AS sources,
                   arg_min(event_id, line_start) AS example_event_id
            FROM events GROUP BY template_id
            """
        )
        connection.execute(
            "CREATE TABLE files (source_file VARCHAR, format_name VARCHAR, "
            "detection_confidence DOUBLE, events BIGINT)"
        )
        files = [
            (f.source_file, f.format_name, f.detection_confidence, f.events) for f in summary.files
        ]
        if files:  # executemany rejects an empty list
            connection.executemany("INSERT INTO files VALUES (?, ?, ?, ?)", files)
        connection.execute(
            "CREATE TABLE warnings (stage VARCHAR, source_file VARCHAR, reason VARCHAR)"
        )
        rows = [
            (stage, issue.source_file, issue.reason)
            for result in results
            for stage, issue in result.warnings
        ]
        rows += [
            ("ingest", issue.source_file, f"skipped: {issue.reason}") for issue in summary.skipped
        ]
        rows += [
            ("pipeline", source, f"failed: {error}") for source, error in summary.failed_inputs
        ]
        if rows:
            connection.executemany("INSERT INTO warnings VALUES (?, ?, ?)", rows)
        connection.execute("CREATE TABLE run_info (key VARCHAR, value VARCHAR)")
        info = {
            "loglens_version": __version__,
            "created_at": datetime.now(UTC).isoformat(),
            "timezone": options.timezone,
            "inputs": str(summary.inputs),
            "records": str(summary.records),
            "events": str(summary.events),
            "seconds": f"{summary.seconds:.3f}",
        }
        connection.executemany("INSERT INTO run_info VALUES (?, ?)", list(info.items()))
    finally:
        connection.close()


def _summarize_from_store(output_dir: Path, summary: ParseSummary) -> None:
    connection = duckdb.connect(str(output_dir / DB_FILE), read_only=True)
    try:
        summary.templates = _scalar(connection, "SELECT count(*) FROM templates")
        summary.levels = Counter(
            dict(connection.execute("SELECT level, count(*) FROM events GROUP BY level").fetchall())
        )
        summary.low_confidence = _scalar(
            connection, f"SELECT count(*) FROM events WHERE parser_confidence < {LOW_CONFIDENCE}"
        )
        summary.top_templates = connection.execute(
            "SELECT template, events FROM templates ORDER BY events DESC, template LIMIT 10"
        ).fetchall()
        levels = ", ".join(f"'{level}'" for level in sorted(ERROR_LEVELS))
        summary.top_error_templates = connection.execute(
            f"SELECT any_value(template), count(*) AS n FROM events WHERE level IN ({levels}) "
            "GROUP BY template_id ORDER BY n DESC, any_value(template) LIMIT 10"
        ).fetchall()
    finally:
        connection.close()


def _scalar(connection: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    return int(row[0]) if row else 0
