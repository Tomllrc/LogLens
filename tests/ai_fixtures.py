"""Synthetic logs and scripted model answers shared by the format-learning tests."""

import random
from pathlib import Path
from typing import Any

PLC_CODES = {
    "ALM": ["Conveyor {n} motor overcurrent ({v} A)", "Pressure {v} bar above limit on line {n}"],
    "WRN": ["Temperature {v} C on press {n} near limit", "Cycle {n} took {v} s"],
    "INF": ["Batch {n} started", "Valve {n} opened to {v} %"],
    "FLT": ["Safety relay {n} tripped, line stopped"],
}


def plc_lines(count: int = 200, seed: int = 7) -> list[str]:
    """An industrial PLC log: ``[PLC07] 14/03/2026 09:00:01.250 ALM #0042 message``."""
    rng = random.Random(seed)
    lines = []
    for i in range(count):
        code = rng.choice(list(PLC_CODES))
        message = rng.choice(PLC_CODES[code]).format(n=rng.randint(1, 9), v=rng.randint(1, 99))
        lines.append(
            f"[PLC{rng.randint(1, 12):02d}] 14/03/2026 09:{i // 60 % 60:02d}:{i % 60:02d}."
            f"{rng.randint(0, 999):03d} {code} #{rng.randint(1, 9999):04d} {message}"
        )
    return lines


def write_plc_log(path: Path, count: int = 200) -> Path:
    path.write_text("\n".join(plc_lines(count)) + "\n", encoding="utf-8")
    return path


PLC_LEVELS = [
    {"raw": "ALM", "level": "ERROR"},
    {"raw": "WRN", "level": "WARN"},
    {"raw": "INF", "level": "INFO"},
    {"raw": "FLT", "level": "FATAL"},
]

CORRECT_PLC: dict[str, Any] = {
    "name": "plc_alarm_log",
    "description": "Alarm log of an industrial PLC.",
    "regex": [
        r"\[(?P<component>PLC\d+)\] (?P<timestamp>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}\.\d{3}) "
        r"(?P<level>[A-Z]{3}) #(?P<code>\d{4}) (?P<message>.*)"
    ],
    "timestamp_format": ["%d/%m/%Y %H:%M:%S.%f"],
    "level_group": "level",
    "level_map": PLC_LEVELS,
}

WRONG_PLC: dict[str, Any] = {
    "name": "plc_alarm_log",
    "description": "Alarm log of an industrial PLC.",
    "regex": [r"(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<message>.*)"],
    "timestamp_format": ["%Y-%m-%d %H:%M:%S"],
}

PARTIAL_PLC: dict[str, Any] = {
    **CORRECT_PLC,
    "regex": [
        r"\[(?P<component>PLC\d+)\] (?P<timestamp>\S+ \S+) (?P<level>ALM|WRN) "
        r"#(?P<code>\d{4}) (?P<message>.*)"
    ],
}
