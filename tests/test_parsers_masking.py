"""Tests for variable masking with typed placeholders."""

import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loglens.parsers.masking import (
    DEFAULT_RULES,
    Masker,
    MaskingConfigError,
    MaskRule,
    default_masker,
    load_masking_rules,
)


@pytest.mark.parametrize(
    ("text", "masked", "params"),
    [
        ("from 10.0.0.5:8080 ok", "from <IP> ok", ["10.0.0.5:8080"]),
        ("via fe80::1ff:fe23:4567:890a", "via <IPV6>", ["fe80::1ff:fe23:4567:890a"]),
        ("mac 00:1A:2B:3C:4D:5E up", "mac <MAC> up", ["00:1A:2B:3C:4D:5E"]),
        (
            "id 3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            "id <UUID>",
            ["3f2504e0-4f89-11d3-9a0c-0305e82c3301"],
        ),
        (
            "hash 9f86d081884c7d65 and 0xBEEF",
            "hash <HEX> and <HEX>",
            ["9f86d081884c7d65", "0xBEEF"],
        ),
        ("took 12ms then 1.5 s", "took <DURATION> then <DURATION>", ["12ms", "1.5 s"]),
        ("wrote 12.5 MB and 300KiB", "wrote <SIZE> and <SIZE>", ["12.5 MB", "300KiB"]),
        ("at /var/log/app.log", "at <PATH>", ["/var/log/app.log"]),
        (r"at C:\logs\app.txt", "at <PATH>", [r"C:\logs\app.txt"]),
        ("see https://x.example/a?b=1", "see <URL>", ["https://x.example/a?b=1"]),
        ("mail bob@example.org now", "mail <EMAIL> now", ["bob@example.org"]),
        (
            "at 2026-03-14T09:12:01.5Z / 10:00:00",
            "at <DATETIME> / <DATETIME>",
            ["2026-03-14T09:12:01.5Z", "10:00:00"],
        ),
        ("got -5 and 3.14 of 7", "got <NUM> and <NUM> of <NUM>", ["-5", "3.14", "7"]),
    ],
)
def test_each_placeholder(text: str, masked: str, params: list[str]) -> None:
    result = default_masker().mask(text)
    assert (result.text, list(result.params)) == (masked, params)


@pytest.mark.parametrize(
    "text",
    [
        "version v1.2.3 build",  # versions are not numbers
        "worker-12 blk_-1608 x86_64",  # digits glued to identifiers are left to the miner
        "decade facade deadbeef",  # hex-looking words without digits
        "ratio-5",
    ],
)
def test_identifiers_are_not_split(text: str) -> None:
    assert default_masker().mask(text).text == text


def test_earlier_rules_win() -> None:
    result = default_masker().mask("GET https://h.example/api/v1 from 10.0.0.1")
    assert result.text == "GET <URL> from <IP>"  # the URL's path is not a <PATH>


def test_custom_rules_file(tmp_path: Path) -> None:
    path = tmp_path / "masking.yaml"
    path.write_text(
        "rules:\n"
        "  - name: TICKET\n    regex: 'JIRA-(\\d+)'\n"
        "  - name: NUM\n    regex: '\\d+'\n    placeholder: '<N>'\n",
        encoding="utf-8",
    )
    rules = load_masking_rules(path)
    assert [rule.placeholder for rule in rules] == ["<TICKET>", "<N>"]
    result = Masker(rules).mask("fix JIRA-42 in 3 days")
    assert (result.text, result.params) == ("fix <TICKET> in <N> days", ("JIRA-42", "3"))


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("[]", "non-empty 'rules' list"),
        ("rules: []", "non-empty 'rules' list"),
        ("rules:\n  - regex: x\n", "needs a 'name'"),
        ("rules:\n  - name: lower\n    regex: x\n", "UPPER_CASE name"),
        ("rules:\n  - name: BAD\n    regex: '(x'\n", "does not compile"),
        ("rules:\n  - name: X\n    regex: x\n    placeholder: nope\n", "look like <NAME>"),
        ("rules: [unclosed", "cannot read"),
    ],
)
def test_invalid_rules_files(tmp_path: Path, body: str, error: str) -> None:
    path = tmp_path / "m.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(MaskingConfigError, match=error):
        load_masking_rules(path)


def test_masker_needs_rules() -> None:
    with pytest.raises(MaskingConfigError):
        Masker(())
    with pytest.raises(MaskingConfigError):
        Masker((MaskRule("X", "(", "<X>"),))


def test_default_rules_cover_every_documented_type() -> None:
    assert {rule.name for rule in DEFAULT_RULES} >= {
        "IP", "IPV6", "MAC", "UUID", "HEX", "NUM", "PATH", "URL", "EMAIL", "DURATION",
        "SIZE", "DATETIME",
    }  # fmt: skip


@given(st.text(max_size=200))
def test_params_reconstruct_the_text(text: str) -> None:
    """Masking loses nothing: filling placeholders back in gives the original text."""
    result = default_masker().mask(text)
    values = iter(result.params)
    rebuilt = re.sub(
        r"<(?:IP|IPV6|MAC|UUID|HEX|NUM|PATH|URL|EMAIL|DURATION|SIZE|DATETIME)>",
        lambda _m: next(values),
        result.text,
    )
    if "<" not in text:  # placeholders already present in the input would be ambiguous
        assert rebuilt == text
