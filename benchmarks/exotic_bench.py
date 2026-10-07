"""Before/after measurement of the AI format learner on the exotic synthetic logs.

Usage::

    uv run python benchmarks/exotic_bench.py                       # before: built-ins only
    uv run python benchmarks/exotic_bench.py --formats-dir DIR     # after: with learned YAMLs

Each log in ``tests/fixtures/exotic/`` (see ``make_exotic_fixtures.py``) is parsed by the
full pipeline; the table reports how much structure LogLens recovered. It measures only
what the unified schema holds; no ground truth is used to tune anything.
"""

import argparse
import tempfile
from pathlib import Path

from loglens.formats import cached_registry
from loglens.pipeline import ParseOptions, run_parse
from loglens.store import query

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "exotic"
METRICS_SQL = """
SELECT
    any_value(format_name) AS format,
    count(*) AS events,
    avg(parser_confidence) AS confidence,
    avg(CASE WHEN timestamp IS NOT NULL THEN 1 ELSE 0 END) AS timestamps,
    avg(CASE WHEN level <> 'UNKNOWN' THEN 1 ELSE 0 END) AS levels,
    avg(CASE WHEN component IS NOT NULL THEN 1 ELSE 0 END) AS components,
    count(DISTINCT template_id) AS templates
FROM events WHERE source_file LIKE ?
"""


def measure(formats_dirs: tuple[Path, ...]) -> list[dict[str, object]]:
    """Parse every exotic log and return one row of metrics per file."""
    cached_registry.cache_clear()
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        options = ParseOptions(workers=1, formats_dirs=formats_dirs)
        run_parse([str(path) for path in sorted(FIXTURES.glob("*.log"))], out, options)
        for path in sorted(FIXTURES.glob("*.log")):
            result = query(out, METRICS_SQL.replace("?", f"'%{path.name}'"))
            columns = result.columns
            rows.append({"file": path.name, **dict(zip(columns, result.rows[0], strict=True))})
    return rows


def render(rows: list[dict[str, object]]) -> str:
    """A Markdown table of the metrics."""
    lines = [
        "| File | Format | Events | Confidence | Timestamps | Levels | Components | Templates |",
        "|---|---|--:|--:|--:|--:|--:|--:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['file']} | `{row['format']}` | {row['events']:,} | "
            f"{float(row['confidence']):.2f} | {float(row['timestamps']):.0%} | "  # type: ignore[arg-type]
            f"{float(row['levels']):.0%} | {float(row['components']):.0%} | "  # type: ignore[arg-type]
            f"{row['templates']} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--formats-dir", type=Path, action="append", default=[])
    args = parser.parse_args()
    print(render(measure(tuple(args.formats_dir))))


if __name__ == "__main__":
    main()
