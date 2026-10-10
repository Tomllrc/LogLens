"""Lexical grouping: merge templates that describe the same problem.

Drain sometimes splits one message into several templates (an extra word, a different
token count). Two templates are *similar* when the Jaccard similarity of their constant
tokens (placeholders removed) is at least ``MIN_JACCARD`` and both have at least
``MIN_CONSTANT_TOKENS`` of them; groups are the transitive closure (union-find).
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from itertools import combinations
from typing import Final

MIN_JACCARD: Final = 0.8
MIN_CONSTANT_TOKENS: Final = 3

_PLACEHOLDER: Final = re.compile(r"<[A-Z*][A-Z0-9_*]*>")
_PUNCTUATION: Final = ".,;:!?()[]{}\"'"


def constant_tokens(template: str) -> frozenset[str]:
    """Lowercased words of a template that contain no placeholder, trimmed of punctuation."""
    tokens = (
        token.strip(_PUNCTUATION).lower()
        for token in template.split()
        if not _PLACEHOLDER.search(token)
    )
    return frozenset(token for token in tokens if token)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """|a & b| / |a | b| (0 for two empty sets)."""
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two constant-token sets describe the same message."""
    if len(a) < MIN_CONSTANT_TOKENS or len(b) < MIN_CONSTANT_TOKENS:
        return False
    return jaccard(a, b) >= MIN_JACCARD


def group_templates(templates: Sequence[tuple[str, str]]) -> list[list[str]]:
    """Group ``(template_id, template)`` pairs; deterministic whatever the input order.

    Only pairs sharing a token are compared (similar sets always share one), so the cost
    follows the number of related templates, not the square of all templates.
    """
    ids = sorted({template_id for template_id, _ in templates})
    tokens = {template_id: constant_tokens(text) for template_id, text in templates}
    parent = {template_id: template_id for template_id in ids}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    index: defaultdict[str, list[str]] = defaultdict(list)
    for template_id in ids:
        for token in tokens[template_id]:
            index[token].append(template_id)
    compared: set[tuple[str, str]] = set()
    for members in index.values():
        for a, b in combinations(members, 2):
            if (a, b) in compared:
                continue
            compared.add((a, b))
            if similar(tokens[a], tokens[b]):
                root_a, root_b = find(a), find(b)
                if root_a != root_b:
                    parent[max(root_a, root_b)] = min(root_a, root_b)
    groups: defaultdict[str, list[str]] = defaultdict(list)
    for template_id in ids:
        groups[find(template_id)].append(template_id)
    return sorted(groups.values())


def match_templates(
    baseline: Sequence[tuple[str, str]], target: Sequence[tuple[str, str]]
) -> dict[str, str]:
    """Map target template ids to baseline ids: same id, else the most similar template.

    Ties go to the smallest baseline id; target templates with no match are left out.
    """
    baseline_ids = {template_id for template_id, _ in baseline}
    baseline_tokens = sorted((template_id, constant_tokens(text)) for template_id, text in baseline)
    result: dict[str, str] = {}
    for template_id, text in target:
        if template_id in baseline_ids:
            result[template_id] = template_id
            continue
        tokens = constant_tokens(text)
        best: tuple[float, str] | None = None
        for candidate, candidate_tokens in baseline_tokens:
            if similar(tokens, candidate_tokens):
                score = jaccard(tokens, candidate_tokens)
                if best is None or score > best[0]:
                    best = (score, candidate)
        if best is not None:
            result[template_id] = best[1]
    return result
