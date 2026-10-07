"""Regenerate the exotic synthetic logs in tests/fixtures/exotic/.

Run with: uv run python tests/fixtures/make_exotic_fixtures.py
Output is deterministic (seeded randomness, no clock). These are three invented layouts
that no built-in parser knows, used to show what the AI format learner adds
(docs/ai-parser.md) and to test learned formats. All data is synthetic.
"""

import random
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "exotic"
LINES = 2000
START = datetime(2026, 3, 14, 6, 0, 0)


def plc_alarm_log(rng: random.Random) -> list[str]:
    """Industrial PLC alarm journal.

    ``14.03.26 06:00:01,250  S7-1500/CPU1  ALM 0042  Conveyor 3 motor overcurrent: 12.4 A``
    Day-first two-digit-year dates with a decimal comma, a CPU path, a three-letter state
    (ALM alarm, WRN warning, INF info, FLT fault, ACK acknowledged), a 4-digit alarm number.
    Fault records carry indented detail lines.
    """
    cpus = ["S7-1500/CPU1", "S7-1500/CPU2", "S7-1200/PLC_Press", "ET200SP/IM155"]
    messages = {
        "ALM": [
            ("Conveyor {n} motor overcurrent: {v}.{d} A", 42),
            ("Pressure on press {n} above limit: {v}.{d} bar", 57),
            ("Hydraulic oil temperature {v} C on unit {n}", 61),
        ],
        "WRN": [
            ("Cycle time {v}{d}0 ms exceeds target on station {n}", 12),
            ("Filter {n} differential pressure high: {v}.{d} mbar", 18),
        ],
        "INF": [
            ("Batch {v}{d}{n} started on line {n}", 1),
            ("Operator login at HMI {n}", 3),
            ("Recipe R-{v}{d} loaded on station {n}", 5),
        ],
        "ACK": [("Alarm {v}{d} acknowledged by operator {n}", 99)],
        "FLT": [("Safety relay K{n} tripped, line {n} stopped", 900)],
    }
    states = ["INF"] * 6 + ["WRN"] * 3 + ["ALM"] * 3 + ["ACK"] * 2 + ["FLT"]
    lines: list[str] = []
    moment = START
    while len(lines) < LINES:
        moment += timedelta(milliseconds=rng.randint(50, 9000))
        state = rng.choice(states)
        template, number = rng.choice(messages[state])
        text = template.format(n=rng.randint(1, 8), v=rng.randint(1, 99), d=rng.randint(0, 9))
        stamp = moment.strftime("%d.%m.%y %H:%M:%S") + f",{moment.microsecond // 1000:03d}"
        lines.append(
            f"{stamp}  {rng.choice(cpus)}  {state} {number + rng.randint(0, 3):04d}  {text}"
        )
        if state == "FLT":
            lines.append(f"    > input I{rng.randint(0, 9)}.{rng.randint(0, 7)} = FALSE")
            lines.append(f"    > last OK cycle: {rng.randint(100, 999)} ms ago")
    return lines[:LINES]


def game_server_log(rng: random.Random) -> list[str]:
    """Multiplayer game server.

    ``[2026/073-06:00:01.250][I][match#8812] KILL Sn1per_42 -> noob_7 (awp, headshot)``
    Year/day-of-year dates, one-letter severities in brackets, a match or system context,
    then an event verb with its own mini-syntax.
    """
    players = ["Sn1per_42", "noob_7", "xX_Rex_Xx", "Lumen", "kappa_99", "Dr.Frag", "Mira"]
    weapons = ["awp", "ak47", "deagle", "knife", "m4a1"]
    lines: list[str] = []
    moment = START
    match = 8812
    while len(lines) < LINES:
        moment += timedelta(milliseconds=rng.randint(10, 3000))
        stamp = moment.strftime("[%Y/%j-%H:%M:%S") + f".{moment.microsecond // 1000:03d}]"
        roll = rng.random()
        a, b = rng.sample(players, 2)
        if roll < 0.45:
            extra = rng.choice(["", ", headshot", ", wallbang", ", headshot, noscope"])
            body = f"[I][match#{match}] KILL {a} -> {b} ({rng.choice(weapons)}{extra})"
        elif roll < 0.6:
            body = (
                f"[I][match#{match}] CHAT {a}: {rng.choice(['gg', 'nice shot', 'lag?', 'rush B'])}"
            )
        elif roll < 0.75:
            body = f"[D][net] TICK rate={rng.randint(60, 128)}Hz lag={rng.randint(5, 250)}ms"
        elif roll < 0.85:
            body = f"[W][net] {a} packet loss {rng.randint(5, 40)}% (ping {rng.randint(80, 400)}ms)"
        elif roll < 0.93:
            octets = [rng.randint(1, 223), rng.randint(0, 255), rng.randint(0, 255)]
            address = ".".join(map(str, [*octets, rng.randint(1, 254)]))
            body = f"[I][lobby] JOIN {a} from {address}"
        elif roll < 0.98:
            body = f"[E][anticheat] {a} flagged: aim snap {rng.randint(150, 900)} deg/s"
        else:
            match += 1
            body = (
                f"[I][match#{match}] ROUND_START map=de_{rng.choice(['dust2', 'nuke', 'inferno'])}"
            )
        lines.append(stamp + body)
    return lines


def pipe_delimited_log(rng: random.Random) -> list[str]:
    """In-house service log with pipe-separated fields and a variable tail.

    ``20260314T060001.250Z|web-03|checkout|E|ORD-88123|payment declined: card expired|retry=0``
    Basic-ISO timestamps, host, service, one-letter level, a correlation id (or ``-``),
    the message, then zero to three ``key=value`` fields: the column count varies.
    """
    services = ["checkout", "cart", "auth", "search", "inventory"]
    events = [
        ("I", "order created"),
        ("I", "user signed in"),
        ("D", "cache hit for key {k}"),
        ("W", "slow query took {n} ms"),
        ("E", "payment declined: card expired"),
        ("E", "upstream timeout after {n} ms"),
        ("F", "database connection pool exhausted"),
    ]
    lines: list[str] = []
    moment = START
    while len(lines) < LINES:
        moment += timedelta(milliseconds=rng.randint(5, 2000))
        stamp = moment.strftime("%Y%m%dT%H%M%S") + f".{moment.microsecond // 1000:03d}Z"
        level, message = rng.choice(events)
        message = message.format(n=rng.randint(100, 9000), k=f"sku:{rng.randint(1, 999)}")
        correlation = f"ORD-{rng.randint(10000, 99999)}" if rng.random() < 0.7 else "-"
        tail = [
            f"{key}={rng.randint(0, 5)}"
            for key in rng.sample(["retry", "shard", "attempt", "node"], rng.randint(0, 3))
        ]
        fields = [stamp, f"web-{rng.randint(1, 6):02d}", rng.choice(services), level]
        lines.append("|".join([*fields, correlation, message, *tail]))
    return lines


GENERATORS = {
    "plc_alarm.log": plc_alarm_log,
    "game_server.log": game_server_log,
    "pipe_service.log": pipe_delimited_log,
}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, generate in GENERATORS.items():
        rng = random.Random(name)
        (OUT / name).write_bytes(("\n".join(generate(rng)) + "\n").encode("utf-8"))
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
