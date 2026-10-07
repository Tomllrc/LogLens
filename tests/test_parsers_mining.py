"""Tests for template mining: Drain, merge step, stable ids, params, persistence."""

from pathlib import Path

import pytest

from loglens.parsers.masking import default_masker
from loglens.parsers.mining import (
    MAX_MINING_CHARS,
    TemplateMiner,
    extract_params,
    final_templates,
    literal_share,
    merge_templates,
    mining_text,
    template_id,
)


def _mine(messages: list[str], miner: TemplateMiner | None = None) -> list[str]:
    """Mine raw messages; return each one's final template."""
    miner = miner or TemplateMiner()
    masker = default_masker()
    clusters = [miner.add(masker.mask(message).text) for message in messages]
    finals = final_templates(miner.templates())
    return [finals[cluster].text for cluster in clusters]


def test_same_template_for_different_variables() -> None:
    templates = _mine(
        [
            "User alice logged in from 10.0.0.1",
            "User bob logged in from 10.0.0.2",
            "User carol logged in from 192.168.1.9",
        ]
    )
    assert set(templates) == {"User <*> logged in from <IP>"}


def test_different_templates_for_different_messages() -> None:
    templates = _mine(
        [
            "Connection reset by peer",
            "Connection refused by 10.0.0.9",
            "Disk full on /dev/sda1",
            "Disk full on /dev/sdb2",
        ]
    )
    assert templates[0] != templates[1]  # typed-aware similarity keeps them apart
    assert templates[2] == templates[3] == "Disk full on <PATH>"


def test_templates_and_ids_are_stable_across_runs_with_state(tmp_path: Path) -> None:
    state = tmp_path / "state" / "drain3.bin"
    first = TemplateMiner(state)
    before = _mine(["Job 1 done in 3s", "Job 2 done in 5s"], first)
    first.save()
    second = TemplateMiner(state)
    after = _mine(["Job 99 done in 1s", "Cache cleared"], second)
    assert after[0] == before[0] == "Job <NUM> done in <DURATION>"
    assert template_id(after[0]) == template_id(before[0])
    assert state.is_file()


def test_template_id_is_a_stable_hash() -> None:
    assert template_id("a <NUM> b") == template_id("a <NUM> b")
    assert template_id("a <NUM> b") != template_id("a <IP> b")
    assert len(template_id("x")) == 16


def test_merge_templates_differing_by_one_placeholder() -> None:
    merged = merge_templates(
        {
            1: "Connected to <IP> in <DURATION>",
            2: "Connected to <IPV6> in <DURATION>",
            3: "Connected to localhost in <DURATION>",
            4: "Disconnected from <IP>",
            5: "Connected to <IP> after <DURATION>",  # differs from 1 at a literal: kept
        }
    )
    assert merged[1] == merged[2] == merged[3] == "Connected to <*> in <DURATION>"
    assert merged[4] == "Disconnected from <IP>"
    assert merged[5] == "Connected to <IP> after <DURATION>"


def test_merge_requires_a_placeholder_at_the_differing_position() -> None:
    merged = merge_templates({1: "status ok", 2: "status ko", 3: "level <NUM> reached"})
    assert merged == {1: "status ok", 2: "status ko", 3: "level <NUM> reached"}


def test_merge_is_deterministic() -> None:
    templates = {i: f"item {x} done" for i, x in enumerate(["<NUM>", "<HEX>", "abc", "<IP>"], 1)}
    assert merge_templates(templates) == merge_templates(dict(reversed(templates.items())))


@pytest.mark.parametrize(
    ("masked", "values", "template", "params"),
    [
        ("Job <NUM> took <DURATION>", ["7", "3s"], "Job <NUM> took <DURATION>", ["7", "3s"]),
        ("User alice from <IP>", ["10.0.0.1"], "User <*> from <IP>", ["alice", "10.0.0.1"]),
        ("node-<NUM> up", ["3"], "<*> up", ["node-3"]),
        ("node-<NUM> up", ["3"], "node-<NUM> up", ["3"]),
        ("to <IPV6> in <DURATION>", ["::1", "2s"], "to <*> in <DURATION>", ["::1", "2s"]),
        ("a b", [], "a b c", []),  # misaligned: fall back to the mask values
    ],
)
def test_extract_params(masked: str, values: list[str], template: str, params: list[str]) -> None:
    assert extract_params(masked, values, template) == params


def test_literal_share() -> None:
    assert literal_share("a b <NUM> <*>") == 0.5
    assert literal_share("node-<NUM> up") == 0.5
    assert literal_share("") == 1.0


def test_mining_text_is_the_bounded_first_line() -> None:
    assert mining_text("  headline \nstack\ntrace") == "headline"
    assert len(mining_text("x" * 5000)) == MAX_MINING_CHARS


def test_empty_messages_have_the_empty_template() -> None:
    miner = TemplateMiner()
    assert miner.add("   ") == 0
    assert final_templates(miner.templates())[0].text == ""
