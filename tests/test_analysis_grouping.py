"""Lexical grouping of templates into issues, and matching across stores."""

from hypothesis import given
from hypothesis import strategies as st

from loglens.analysis.grouping import (
    constant_tokens,
    group_templates,
    match_templates,
    similar,
)


def test_constant_tokens_drop_placeholders_and_punctuation() -> None:
    assert constant_tokens("Connection to <IP> refused: (peer) <*> <NUM>ms") == frozenset(
        {"connection", "to", "refused", "peer"}
    )


def test_constant_tokens_keep_words_with_inner_symbols() -> None:
    assert constant_tokens("user_id=<NUM> GET /api/v1") == frozenset({"get", "/api/v1"})


def test_one_extra_word_still_merges() -> None:
    a = constant_tokens("Connection to <IP> refused by peer")
    b = constant_tokens("Connection to <IP> refused by remote peer")
    assert similar(a, b)  # Jaccard 5/6


def test_short_templates_never_merge() -> None:
    assert not similar(constant_tokens("<*> failed"), constant_tokens("<*> failed"))
    assert not similar(constant_tokens("job <*> failed"), constant_tokens("job <*> failed"))
    assert similar(constant_tokens("job <*> failed hard"), constant_tokens("job <*> failed hard"))


def test_different_messages_do_not_merge() -> None:
    a = constant_tokens("Payment gateway timeout after <NUM> ms")
    b = constant_tokens("Disk quota exceeded on volume <PATH>")
    assert not similar(a, b)


def test_group_templates_merges_transitively() -> None:
    templates = [
        ("c", "alpha beta gamma delta epsilon zeta eta theta iota"),
        ("a", "alpha beta gamma delta epsilon zeta eta theta"),
        ("b", "alpha beta gamma delta epsilon zeta eta theta iota kappa"),
        ("d", "something else entirely here"),
    ]
    # a~c (8/9), c~b (9/10), a~b (8/10): one group, d alone
    assert group_templates(templates) == [["a", "b", "c"], ["d"]]


def test_group_templates_chain_without_direct_link() -> None:
    a = "w1 w2 w3 w4 w5 w6 w7 w8"
    b = "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10"  # a~b: 8/10
    c = "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10 w11 w12"  # b~c: 10/12 = 0.83, a~c: 8/12
    assert not similar(constant_tokens(a), constant_tokens(c))
    assert group_templates([("x", a), ("y", b), ("z", c)]) == [["x", "y", "z"]]


def test_group_templates_of_nothing() -> None:
    assert group_templates([]) == []


@given(st.permutations(list(range(6))))
def test_grouping_does_not_depend_on_input_order(order: list[int]) -> None:
    templates = [
        ("t0", "Connection to <IP> refused by peer"),
        ("t1", "Connection to <IP> refused by remote peer"),
        ("t2", "Disk quota exceeded on volume <PATH>"),
        ("t3", "Payment gateway timeout after <NUM> ms"),
        ("t4", "Payment gateway timeout after <NUM> ms again"),
        ("t5", "<*> failed"),
    ]
    shuffled = [templates[i] for i in order]
    assert group_templates(shuffled) == [["t0", "t1"], ["t2"], ["t3", "t4"], ["t5"]]


def test_match_templates_prefers_exact_ids() -> None:
    base = [("t1", "Connection to <IP> refused by peer"), ("t2", "Disk full on <PATH>")]
    target = [("t1", "Connection to <IP> refused by peer"), ("t9", "Brand new message here")]
    assert match_templates(base, target) == {"t1": "t1"}


def test_match_templates_falls_back_to_lexical_similarity() -> None:
    base = [("a1", "Connection to <IP> refused by peer"), ("a2", "Connection to <*> lost now")]
    target = [("b1", "Connection to <*> refused by remote peer")]
    assert match_templates(base, target) == {"b1": "a1"}


def test_match_templates_breaks_ties_by_smallest_id() -> None:
    base = [("z", "one two three four"), ("m", "one two three four")]
    target = [("q", "one two three four")]
    assert match_templates(base, target) == {"q": "m"}
