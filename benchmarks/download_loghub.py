"""Download the Loghub 2k samples and their ground truth into data/loghub/ (gitignored).

Run with: uv run python -I benchmarks/download_loghub.py [dataset ...]

Source: https://github.com/logpai/loghub (Zhu et al., "Loghub: A Large Collection of
System Log Datasets for AI-driven Log Analytics", ISSRE 2023). Each dataset gets its own
folder with <Name>_2k.log, <Name>_2k.log_structured.csv and <Name>_2k.log_templates.csv.
Files already present are kept.
"""

import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://raw.githubusercontent.com/logpai/loghub/master"
DATASETS = (
    "Android",
    "Apache",
    "BGL",
    "Hadoop",
    "HDFS",
    "HealthApp",
    "HPC",
    "Linux",
    "Mac",
    "OpenSSH",
    "OpenStack",
    "Proxifier",
    "Spark",
    "Thunderbird",
    "Windows",
    "Zookeeper",
)
SUFFIXES = ("_2k.log", "_2k.log_structured.csv", "_2k.log_templates.csv")
DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "loghub"


def download(dataset: str, target: Path = DATA_DIR) -> list[Path]:
    """Download one dataset's files (skipping existing ones); returns their paths."""
    folder = target / dataset
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for suffix in SUFFIXES:
        path = folder / f"{dataset}{suffix}"
        if not path.is_file() or path.stat().st_size == 0:
            url = f"{BASE_URL}/{dataset}/{dataset}{suffix}"
            with urllib.request.urlopen(url, timeout=60) as response:
                path.write_bytes(response.read())
        paths.append(path)
    return paths


def main(argv: list[str]) -> int:
    names = argv[1:] or list(DATASETS)
    unknown = sorted(set(names) - set(DATASETS))
    if unknown:
        print(f"unknown datasets: {', '.join(unknown)}; choose from {', '.join(DATASETS)}")
        return 2
    for name in names:
        paths = download(name)
        sizes = ", ".join(f"{p.name} {p.stat().st_size:,} B" for p in paths)
        print(f"{name}: {sizes}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
