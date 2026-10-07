"""Evaluate LogLens on the Loghub 2k datasets.

Run with: uv run python -I benchmarks/loghub_bench.py [--datasets A,B] [--scale 50]
          [--no-record] [--check] [--label TEXT]

Each dataset's 2k log goes through the full pipeline (`run_parse`, one worker), and its
events are compared with the ground truth (``<Name>_2k.log_structured.csv``):

- Grouping accuracy (GA, Zhu et al. 2019): a line counts as correct when the set of
  lines sharing its predicted template is exactly the set sharing its true event.
- Parsing accuracy (PA): the predicted template equals the true one, after mapping our
  typed placeholders (<IP>, <NUM>...) to <*> and normalizing whitespace.
- Timestamp rate: lines with a timestamp extracted; timestamp accuracy: the digits of
  the true Time column appear in the extracted timestamp.
- Level rate / accuracy: among lines whose true level is a level, extracted at all /
  normalized equal.
- Throughput: lines per second of the full pipeline on the 2k log repeated ``--scale``
  times (2k lines alone mostly measure start-up costs).

The pipeline splits multi-line records: a ground-truth line swallowed into a previous
record gets no template of its own and counts as wrongly grouped and parsed.

Results go to benchmarks/RESULTS.md and are appended to benchmarks/history.json.
``--check`` fails (exit 1) if average GA dropped by more than 2 points since the last
recorded run. The code under test is never tuned to this ground truth.
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from loglens.pipeline import ParseOptions, run_parse
from loglens.schema import UNKNOWN, normalize_level
from loglens.store import query

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "loghub"
RESULTS_MD = ROOT / "benchmarks" / "RESULTS.md"
HISTORY_JSON = ROOT / "benchmarks" / "history.json"
GA_REGRESSION_POINTS = 2.0
_PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*>|<\*>")
_SINGLE_LETTER_LEVELS = {
    "V": "DEBUG",
    "D": "DEBUG",
    "I": "INFO",
    "W": "WARN",
    "E": "ERROR",
    "F": "FATAL",
}


@dataclass
class DatasetResult:
    """Metrics of one dataset (rates in [0, 1])."""

    dataset: str
    lines: int
    grouping_accuracy: float
    parsing_accuracy: float
    timestamp_rate: float
    timestamp_accuracy: float
    level_rate: float | None
    level_accuracy: float | None
    lines_per_second: float
    templates_true: int
    templates_found: int


def normalize_template(text: str) -> str:
    """A template with every placeholder as <*> and single spaces."""
    return " ".join(_PLACEHOLDER.sub("<*>", text).split())


def grouping_accuracy(predicted: list[str], truth: list[str]) -> float:
    """Share of lines whose predicted group equals their true group exactly."""
    predicted_groups: dict[str, list[int]] = defaultdict(list)
    true_groups: dict[str, set[int]] = defaultdict(set)
    for index, (p, t) in enumerate(zip(predicted, truth, strict=True)):
        predicted_groups[p].append(index)
        true_groups[t].add(index)
    correct = 0
    for members in predicted_groups.values():
        labels = {truth[i] for i in members}
        if len(labels) == 1 and true_groups[labels.pop()] == set(members):
            correct += len(members)
    return correct / len(truth) if truth else 0.0


def _truth_level(raw: str) -> str:
    raw = raw.strip()
    return _SINGLE_LETTER_LEVELS.get(raw, normalize_level(raw))


def _digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def _read_truth(dataset: str) -> list[dict[str, str]]:
    path = DATA_DIR / dataset / f"{dataset}_2k.log_structured.csv"
    previous_limit = csv.field_size_limit(10_000_000)  # process-wide: restored below
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as handle:
            return list(csv.DictReader(handle))
    finally:
        csv.field_size_limit(previous_limit)


def evaluate(dataset: str, workdir: Path, scale: int) -> DatasetResult:
    """Parse one dataset and score it against its ground truth."""
    log = DATA_DIR / dataset / f"{dataset}_2k.log"
    truth = _read_truth(dataset)
    output = workdir / dataset
    run_parse([log], output, ParseOptions(workers=1))
    rows = query(
        output,
        "SELECT line_start, line_end, template_id, template, timestamp_raw, timestamp, level "
        "FROM events",
        limit=10_000_000,
    ).rows
    by_line = {row[0]: row for row in rows}
    covered = {line for start, end, *_ in rows for line in range(start + 1, end + 1)}

    predicted, true_ids = [], []
    parsed_ok = ts_found = ts_ok = ts_total = 0
    level_found = level_ok = level_total = 0
    for index, line in enumerate(truth, start=1):
        true_ids.append(line["EventId"])
        row = by_line.get(index)
        if row is None:
            reason = "merged" if index in covered else "missing"
            predicted.append(f"{reason}:{index}")
            continue
        _, _, template_id, template, timestamp_raw, timestamp, level = row
        predicted.append(template_id)
        parsed_ok += normalize_template(template) == normalize_template(line["EventTemplate"])
        if line.get("Time", "").strip():
            ts_total += 1
            ts_found += timestamp is not None
            found = _digits(timestamp_raw)
            ts_ok += timestamp is not None and (
                _digits(line["Time"]) in found  # the time of day as written
                or (bool(line.get("Timestamp")) and _digits(line["Timestamp"]) == found)  # epoch
            )
        expected_level = _truth_level(line.get("Level", ""))
        if expected_level != UNKNOWN:
            level_total += 1
            level_found += level != UNKNOWN
            level_ok += level == expected_level

    seconds = _throughput_seconds(log, workdir / f"{dataset}-scaled", scale)
    return DatasetResult(
        dataset=dataset,
        lines=len(truth),
        grouping_accuracy=grouping_accuracy(predicted, true_ids),
        parsing_accuracy=parsed_ok / len(truth),
        timestamp_rate=ts_found / ts_total if ts_total else 0.0,
        timestamp_accuracy=ts_ok / ts_total if ts_total else 0.0,
        level_rate=level_found / level_total if level_total else None,
        level_accuracy=level_ok / level_total if level_total else None,
        lines_per_second=len(truth) * scale / seconds if seconds else 0.0,
        templates_true=len(set(true_ids)),
        templates_found=len({p for p in predicted if not p.startswith(("merged:", "missing:"))}),
    )


def _throughput_seconds(log: Path, folder: Path, scale: int) -> float:
    if scale < 1:
        return 0.0
    folder.mkdir(parents=True, exist_ok=True)
    scaled = folder / log.name
    data = log.read_bytes()
    if not data.endswith(b"\n"):
        data += b"\n"
    scaled.write_bytes(data * scale)
    started = time.perf_counter()
    run_parse([scaled], folder / "out", ParseOptions(workers=1))
    return time.perf_counter() - started


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}"


def averages(results: list[DatasetResult]) -> dict[str, float]:
    """Mean of each metric over the datasets where it applies."""

    def avg(values: list[float | None]) -> float:
        present = [v for v in values if v is not None]
        return mean(present) if present else 0.0

    return {
        "grouping_accuracy": avg([r.grouping_accuracy for r in results]),
        "parsing_accuracy": avg([r.parsing_accuracy for r in results]),
        "timestamp_rate": avg([r.timestamp_rate for r in results]),
        "timestamp_accuracy": avg([r.timestamp_accuracy for r in results]),
        "level_rate": avg([r.level_rate for r in results]),
        "level_accuracy": avg([r.level_accuracy for r in results]),
        "lines_per_second": avg([r.lines_per_second for r in results]),
    }


def render_markdown(
    results: list[DatasetResult], label: str, history: list[dict[str, object]]
) -> str:
    """The RESULTS.md table for a run, plus the GA history."""
    lines = [
        "# Benchmark results",
        "",
        "LogLens on the [Loghub](https://github.com/logpai/loghub) 2k samples (16 datasets, "
        "2,000 lines each, with ground-truth templates). Generated by "
        "`benchmarks/loghub_bench.py`; methodology in its docstring. GA = grouping accuracy, "
        "PA = parsing accuracy, TS = timestamps extracted / correct, Level = levels extracted "
        "/ correct (only where the ground truth has a level). Throughput: full pipeline, "
        "one worker, on each log repeated 50 times.",
        "",
        f"Run: {label}",
        "",
        "| Dataset | GA | PA | TS found | TS correct | Level found | Level correct "
        "| Templates (true / found) | Lines/s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r.dataset} | {_pct(r.grouping_accuracy)} | {_pct(r.parsing_accuracy)} "
            f"| {_pct(r.timestamp_rate)} | {_pct(r.timestamp_accuracy)} | {_pct(r.level_rate)} "
            f"| {_pct(r.level_accuracy)} | {r.templates_true} / {r.templates_found} "
            f"| {r.lines_per_second:,.0f} |"
        )
    avg = averages(results)
    lines.append(
        f"| **Average** | **{_pct(avg['grouping_accuracy'])}** "
        f"| **{_pct(avg['parsing_accuracy'])}** "
        f"| {_pct(avg['timestamp_rate'])} | {_pct(avg['timestamp_accuracy'])} "
        f"| {_pct(avg['level_rate'])} | {_pct(avg['level_accuracy'])} | | "
        f"{avg['lines_per_second']:,.0f} |"
    )
    baseline = history[0] if history else None
    if baseline is not None and baseline is not history[-1]:
        before = {d["dataset"]: d for d in baseline["datasets"]}  # type: ignore[index, union-attr]
        lines += [
            "",
            f"## Change since the baseline ({baseline['label']}, commit {baseline['commit']})",
            "",
            "| Dataset | GA before | GA now | PA before | PA now |",
            "|---|---:|---:|---:|---:|",
        ]
        for r in results:
            old = before.get(r.dataset)
            if old is not None:
                lines.append(
                    f"| {r.dataset} | {_pct(old['grouping_accuracy'])} "
                    f"| {_pct(r.grouping_accuracy)} "
                    f"| {_pct(old['parsing_accuracy'])} | {_pct(r.parsing_accuracy)} |"
                )
        lines += [
            "",
            "What changed and why, with real failing examples: [ANALYSIS.md](ANALYSIS.md).",
        ]
    if history:
        lines += [
            "",
            "## History (average GA / PA)",
            "",
            "| Date | Commit | Label | GA | PA |",
            "|---|---|---|---:|---:|",
        ]
        for entry in history[-15:]:
            entry_avg = entry["averages"]
            assert isinstance(entry_avg, dict)
            lines.append(
                f"| {str(entry['date'])[:16]} | {entry['commit']} | {entry['label']} "
                f"| {_pct(entry_avg['grouping_accuracy'])} "
                f"| {_pct(entry_avg['parsing_accuracy'])} |"
            )
    return "\n".join(lines) + "\n"


def ga_change_points(previous: dict[str, float], current: dict[str, float]) -> float:
    """Change of average grouping accuracy, in percentage points."""
    # Rounded so that an exact 2-point drop is not flagged through float error.
    return round((current["grouping_accuracy"] - previous["grouping_accuracy"]) * 100, 6)


def is_regression(previous: dict[str, float], current: dict[str, float]) -> bool:
    """True if average GA dropped by more than the allowed 2 points."""
    return ga_change_points(previous, current) < -GA_REGRESSION_POINTS


def _load_history() -> list[dict[str, object]]:
    if HISTORY_JSON.is_file():
        data = json.loads(HISTORY_JSON.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    return []


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--datasets", default="", help="Comma-separated subset (default: all).")
    parser.add_argument("--scale", type=int, default=50, help="Repetitions for throughput.")
    parser.add_argument("--no-record", action="store_true", help="Do not write RESULTS/history.")
    parser.add_argument("--check", action="store_true", help="Fail on a GA regression > 2 points.")
    parser.add_argument("--label", default="", help="Free-text label stored with the run.")
    args = parser.parse_args(argv[1:])

    available = (
        sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir()) if DATA_DIR.is_dir() else []
    )
    names = [n for n in args.datasets.split(",") if n] or available
    missing = [n for n in names if n not in available]
    if not names or missing:
        print(f"datasets not downloaded: {missing or 'all'}; run benchmarks/download_loghub.py")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="loglens-bench-"))
    results: list[DatasetResult] = []
    try:
        for name in names:
            result = evaluate(name, workdir, args.scale)
            results.append(result)
            print(
                f"{name:<12} GA {_pct(result.grouping_accuracy):>5}"
                f"  PA {_pct(result.parsing_accuracy):>5}"
                f"  TS {_pct(result.timestamp_rate):>5}/{_pct(result.timestamp_accuracy):>5}"
                f"  LV {_pct(result.level_rate):>5}/{_pct(result.level_accuracy):>5}"
                f"  {result.lines_per_second:>9,.0f} lines/s"
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    avg = averages(results)
    print(
        f"{'AVERAGE':<12} GA {_pct(avg['grouping_accuracy']):>5}"
        f"  PA {_pct(avg['parsing_accuracy']):>5}"
    )
    history = _load_history()
    status = 0
    full_run = set(names) == set(available)
    if args.check and history and full_run:
        previous = history[-1]["averages"]
        assert isinstance(previous, dict)
        change = ga_change_points(previous, avg)
        if is_regression(previous, avg):
            print(
                f"REGRESSION: average GA dropped by {-change:.1f} points (> {GA_REGRESSION_POINTS})"
            )
            status = 1
        else:
            print(f"Regression check passed (GA change {change:+.1f} points)")
    if not args.no_record and full_run:
        entry = {
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "commit": _git_commit(),
            "label": args.label,
            "averages": avg,
            "datasets": [asdict(r) for r in results],
        }
        history.append(entry)
        HISTORY_JSON.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
        label = (
            f"{entry['date']} (commit {entry['commit']}){' - ' + args.label if args.label else ''}"
        )
        RESULTS_MD.write_text(render_markdown(results, label, history), encoding="utf-8")
        print(f"Recorded in {RESULTS_MD.relative_to(ROOT)} and {HISTORY_JSON.relative_to(ROOT)}")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
