"""Evaluate LogLens anomaly detection on labeled BGL logs, against simple baselines.

Run with: uv run python -I benchmarks/anomaly_eval.py [--full] [--log PATH]...
          [--no-record] [--label TEXT]

BGL (Blue Gene/L supercomputer, Loghub) labels every line: its first token is ``-`` for
a normal line, or an alert category (``KERNDTLB``, ``APPSEV``...) for an alert. The
script streams the log, keeps the labels by line number, and parses a copy *without*
the label column, so the labels never reach LogLens. It then runs ``find_anomalies``
with its default settings and scores the flagged lines (``predictions_by_detector``):

- per detector and for all detectors together: precision, recall, F1 with TP/FP/FN,
- next to three baselines: every ERROR/FATAL line, every line of an issue candidate
  (``find_issues``), and random guessing at the base rate (expected values).

Default input: ``data/loghub/BGL/BGL_2k.log`` (from ``benchmarks/download_loghub.py``).
``--full`` also downloads the full BGL log (4.7M lines, Loghub on Zenodo) into
``data/bgl_full/`` and evaluates it. Results go to benchmarks/ANOMALY_RESULTS.md and are
appended to benchmarks/anomaly_history.json. Thresholds are never tuned to these labels.
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from loglens.analysis.anomalies import find_anomalies, predictions_by_detector
from loglens.analysis.issues import find_issues
from loglens.pipeline import ParseOptions, run_parse
from loglens.store import connect

ROOT = Path(__file__).resolve().parents[1]
BGL_2K = ROOT / "data" / "loghub" / "BGL" / "BGL_2k.log"
FULL_DIR = ROOT / "data" / "bgl_full"
FULL_URL = "https://zenodo.org/records/8196385/files/BGL.zip?download=1"
RESULTS_MD = ROOT / "benchmarks" / "ANOMALY_RESULTS.md"
HISTORY_JSON = ROOT / "benchmarks" / "anomaly_history.json"
DETECTORS = ("spike", "new_pattern", "rare_transition")
BASELINES = ("ERROR/FATAL lines", "issue-candidate lines", "random (base rate)")


@dataclass(frozen=True)
class Metrics:
    """Confusion counts (floats: expected values for the random baseline) and scores."""

    tp: float
    fp: float
    fn: float

    @property
    def precision(self) -> float:
        """TP / (TP + FP)."""
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        """TP / (TP + FN)."""
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        """Harmonic mean of precision and recall."""
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def as_dict(self) -> dict[str, float]:
        """Counts and scores, for the history file."""
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


@dataclass
class Evaluation:
    """Results on one labeled log."""

    name: str
    lines: int
    positives: int
    events: int
    seconds: float
    detectors: dict[str, Metrics] = field(default_factory=dict)
    baselines: dict[str, Metrics] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def score(predicted: set[int], labels: Sequence[bool]) -> Metrics:
    """Score predicted line numbers (1-based) against per-line labels."""
    tp = sum(1 for line in predicted if 1 <= line <= len(labels) and labels[line - 1])
    positives = sum(labels)
    return Metrics(tp, len(predicted) - tp, positives - tp)


def random_baseline(labels: Sequence[bool]) -> Metrics:
    """Expected counts when each line is flagged with probability = the base rate."""
    n, positives = len(labels), sum(labels)
    rate = positives / n if n else 0.0
    return Metrics(rate * positives, rate * (n - positives), positives - rate * positives)


def strip_labels(src: Path, dst: Path) -> list[bool]:
    """Copy ``src`` without its first token per line; return True for alert lines."""
    labels: list[bool] = []
    with (
        src.open(encoding="utf-8", errors="replace", newline="") as reader,
        dst.open("w", encoding="utf-8", newline="\n") as writer,
    ):
        for raw in reader:
            text = raw.rstrip("\r\n")
            label, _, rest = text.partition(" ")
            labels.append(label != "-")
            writer.write(rest + "\n")
    return labels


def lines_of(conn: duckdb.DuckDBPyConnection, event_ids: Iterable[str]) -> set[int]:
    """Line numbers covered by the given events."""
    ids = pa.table({"event_id": pa.array(sorted(set(event_ids)), pa.string())})
    conn.register("flagged_ids", ids)
    try:
        rows = conn.execute(
            "SELECT line_start, line_end FROM events JOIN flagged_ids USING (event_id)"
        ).fetchall()
    finally:
        conn.unregister("flagged_ids")
    return {line for start, end in rows for line in range(start, end + 1)}


def _lines_where(conn: duckdb.DuckDBPyConnection, condition: str, params: list[object]) -> set[int]:
    rows = conn.execute(
        f"SELECT line_start, line_end FROM events WHERE {condition}", params
    ).fetchall()
    return {line for start, end in rows for line in range(start, end + 1)}


def evaluate(log: Path, workdir: Path, workers: int = 1) -> Evaluation:
    """Parse a label-free copy of ``log`` and score every detector and baseline."""
    started = time.perf_counter()
    workdir.mkdir(parents=True, exist_ok=True)
    clean = workdir / log.name
    labels = strip_labels(log, clean)
    store = workdir / "store"
    run_parse([clean], store, ParseOptions(workers=workers))
    conn = connect(store)
    try:
        report = find_anomalies(conn)
        by_detector = {
            name: lines_of(conn, ids) for name, ids in predictions_by_detector(conn, report).items()
        }
        issues = find_issues(conn, evidence=False)
        candidates = [t for issue in issues.issues for t in issue.template_ids]
        error_lines = _lines_where(conn, "level IN ('ERROR', 'FATAL')", [])
        candidate_lines = _lines_where(conn, "list_contains(?, template_id)", [candidates])
        events_row = conn.execute("SELECT count(*) FROM events").fetchone()
    finally:
        conn.close()
    result = Evaluation(
        name=log.name,
        lines=len(labels),
        positives=sum(labels),
        events=int(events_row[0]) if events_row else 0,
        seconds=0.0,
        notes=list(report.notes),
    )
    for name in DETECTORS:
        result.detectors[name] = score(by_detector.get(name, set()), labels)
    result.detectors["all detectors"] = score(set().union(*by_detector.values()), labels)
    result.baselines["ERROR/FATAL lines"] = score(error_lines, labels)
    result.baselines["issue-candidate lines"] = score(candidate_lines, labels)
    result.baselines["random (base rate)"] = random_baseline(labels)
    if report.grid is not None:
        result.notes.append(f"{report.grid.size} buckets of {report.grid.bucket}")
    result.seconds = time.perf_counter() - started
    return result


def download_full_bgl(target_dir: Path = FULL_DIR) -> Path:
    """Download and extract the full BGL log (streamed to disk), unless already there."""
    log = target_dir / "BGL.log"
    if log.is_file():
        return log
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = target_dir / "BGL.zip"
    print(f"Downloading {FULL_URL} ...", flush=True)
    with urllib.request.urlopen(FULL_URL, timeout=120) as response, archive.open("wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)
    with zipfile.ZipFile(archive) as bundle:
        member = next(name for name in bundle.namelist() if name.endswith("BGL.log"))
        with bundle.open(member) as source, log.open("wb") as out:
            shutil.copyfileobj(source, out, length=1 << 20)
    archive.unlink()
    return log


def _pct(value: float) -> str:
    return f"{100 * value:.1f}"


def _count(value: float) -> str:
    return f"{value:,.0f}"


def render_markdown(results: Sequence[Evaluation], label: str, commit: str) -> str:
    """The results page."""
    lines = [
        "# Anomaly detection on labeled BGL logs",
        "",
        "Generated by `benchmarks/anomaly_eval.py`. Line-level scores: a line is flagged when",
        "an event covering it is flagged. Labels are removed before parsing. Detector",
        "thresholds are the defaults (docs/analysis.md), never tuned to these labels.",
        "",
        f"Run: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}, commit `{commit}`"
        + (f", {label}" if label else ""),
        "",
    ]
    for result in results:
        rate = result.positives / result.lines if result.lines else 0.0
        lines += [
            f"## {result.name}",
            "",
            f"{result.lines:,} lines, {result.positives:,} labeled alerts "
            f"({_pct(rate)}%), {result.events:,} events, {result.seconds:.1f} s.",
            "",
            "| Method | Precision % | Recall % | F1 % | TP | FP | FN |",
            "|---|---|---|---|---|---|---|",
        ]
        for title, metrics in [*result.detectors.items(), *result.baselines.items()]:
            name = f"baseline: {title}" if title in result.baselines else title
            lines.append(
                f"| {name} | {_pct(metrics.precision)} | {_pct(metrics.recall)} | "
                f"{_pct(metrics.f1)} | {_count(metrics.tp)} | {_count(metrics.fp)} | "
                f"{_count(metrics.fn)} |"
            )
        if result.notes:
            lines += ["", *(f"- {note}" for note in result.notes)]
        lines.append("")
    return "\n".join(lines)


def _git_commit() -> str:
    head = ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            return (ROOT / ".git" / ref[5:]).read_text(encoding="utf-8").strip()[:7]
        return ref[:7]
    except OSError:
        return "unknown"


def main(argv: list[str]) -> int:
    """Evaluate, print a summary, and record the results unless ``--no-record``."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--log", action="append", type=Path, help="Labeled BGL-style log.")
    parser.add_argument("--full", action="store_true", help="Also evaluate the full BGL log.")
    parser.add_argument("--workers", type=int, default=1, help="Parse workers.")
    parser.add_argument("--no-record", action="store_true", help="Do not write results.")
    parser.add_argument("--label", default="", help="Free-text label stored with the run.")
    parser.add_argument("--results", type=Path, default=RESULTS_MD, help=argparse.SUPPRESS)
    parser.add_argument("--history", type=Path, default=HISTORY_JSON, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logs: list[Path] = list(args.log or [BGL_2K])
    if args.full:
        logs.append(download_full_bgl())
    missing = [log for log in logs if not log.is_file()]
    if missing:
        print(f"Missing {missing[0]}: run `uv run python -I benchmarks/download_loghub.py`.")
        return 1
    results = []
    with tempfile.TemporaryDirectory(prefix="loglens-anomaly-eval-") as tmp:
        for index, log in enumerate(logs):
            result = evaluate(log, Path(tmp) / str(index), workers=args.workers)
            results.append(result)
            combined = result.detectors["all detectors"]
            print(
                f"{result.name}: all detectors P {_pct(combined.precision)} "
                f"R {_pct(combined.recall)} F1 {_pct(combined.f1)} ({result.seconds:.1f} s)"
            )
    if args.no_record:
        return 0
    commit = _git_commit()
    args.results.write_text(render_markdown(results, args.label, commit), encoding="utf-8")
    history = json.loads(args.history.read_text(encoding="utf-8")) if args.history.is_file() else []
    history.append(
        {
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "commit": commit,
            "label": args.label,
            "results": [
                {
                    "name": r.name,
                    "lines": r.lines,
                    "positives": r.positives,
                    "seconds": round(r.seconds, 2),
                    "detectors": {k: m.as_dict() for k, m in r.detectors.items()},
                    "baselines": {k: m.as_dict() for k, m in r.baselines.items()},
                }
                for r in results
            ],
        }
    )
    args.history.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.results} and {args.history}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
