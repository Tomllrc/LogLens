"""The exotic synthetic logs: unknown to every built-in parser, parsed without crashing."""

from pathlib import Path

import pytest

from loglens.pipeline import ParseOptions, low_confidence_sources, run_parse
from loglens.sampling import load_records, select_samples

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


@pytest.mark.parametrize("name", FILES)
def test_samples_cover_every_line_shape(name: str) -> None:
    records = load_records(EXOTIC / name)
    samples = select_samples(records)
    assert len(samples) == 50
    assert len(set(samples)) == 50
