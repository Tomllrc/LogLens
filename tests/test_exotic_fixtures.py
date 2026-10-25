"""The exotic synthetic logs: unknown to every built-in parser, parsed without crashing."""

from pathlib import Path

import pytest

from loglens.formats import cached_registry, load_registry
from loglens.pipeline import ParseOptions, low_confidence_sources, run_parse
from loglens.sampling import load_records, select_samples
from loglens.store import query

EXOTIC = Path(__file__).parent / "fixtures" / "exotic"
FILES = ["game_server.log", "pipe_service.log", "plc_alarm.log"]


def test_fixtures_exist() -> None:
    assert sorted(p.name for p in EXOTIC.glob("*.log")) == FILES


def test_no_built_in_format_claims_them(tmp_path: Path) -> None:
    summary = run_parse([str(EXOTIC)], tmp_path / "out", ParseOptions(workers=1))
    assert dict(summary.formats) == {"free_text": 3}
    assert summary.events == sum(len(load_records(EXOTIC / name)) for name in FILES)
    flagged = {Path(source).name for source, _, _ in low_confidence_sources(tmp_path / "out")}
    assert flagged == set(FILES)
    # The summary agrees with the learning hint: these events are low confidence too.
    assert summary.low_confidence_share == 1.0


@pytest.mark.parametrize("name", FILES)
def test_samples_cover_every_line_shape(name: str) -> None:
    records = load_records(EXOTIC / name)
    samples = select_samples(records)
    assert len(samples) == 50
    assert len(set(samples)) == 50


LEARNED = Path(__file__).parent.parent / "examples" / "formats"
"""Formats Gemini learned from these logs (docs/ai-parser.md), kept as regression tests."""


def test_learned_example_formats_parse_their_logs(tmp_path: Path) -> None:
    registry = load_registry([LEARNED])
    assert registry.errors == ()
    learned = {name for name, origin in registry.origins.items() if "examples" in origin}
    assert learned == {"game_server_log", "pipe_delimited_service", "plc_system_log"}
    assert all(registry.text_formats[n].metadata["generated_by"] == "ai" for n in learned)

    cached_registry.cache_clear()
    out = tmp_path / "out"
    run_parse([str(EXOTIC)], out, ParseOptions(workers=1, formats_dirs=(LEARNED,)))
    cached_registry.cache_clear()
    rows = query(
        out,
        "SELECT format_name, min(parser_confidence), avg((timestamp IS NOT NULL)::INT), "
        "avg((level <> 'UNKNOWN')::INT), avg((component IS NOT NULL)::INT) "
        "FROM events GROUP BY 1 ORDER BY 1",
    ).rows
    assert [row[0] for row in rows] == sorted(learned)
    for _, confidence, timestamps, levels, components in rows:
        assert confidence >= 0.99
        assert (timestamps, levels, components) == (1.0, 1.0, 1.0)
    assert low_confidence_sources(out) == []
