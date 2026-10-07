"""Template mining with Drain (drain3), on masked messages.

Messages are first masked (``masking.py``), so ``Connected to 10.0.0.5`` and
``Connected to 10.0.0.9`` are both ``Connected to <IP>``. Drain then groups masked
messages into clusters whose template replaces varying tokens with ``<*>``. On top of
Drain:

- the miner's state is saved to a file, so templates stay stable across runs;
- templates differing at a single position where at least one of them has a placeholder
  are merged (``Disk <NUM> full`` and ``Disk <*> full``), since Drain can split them;
- ``template_id`` is a stable hash of the final template text;
- params are recovered by aligning the masked message with its template.

Mining works on the first line of a message (the event's headline; stack traces vary
too much to template), truncated to :data:`MAX_MINING_CHARS`.
"""

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from drain3 import TemplateMiner as Drain3Miner
from drain3.drain import Drain
from drain3.file_persistence import FilePersistence
from drain3.template_miner_config import TemplateMinerConfig

from loglens.parsers.masking import PLACEHOLDER

MAX_MINING_CHARS: Final = 1000
MAX_LEAF_CANDIDATES: Final = 256
"""Clusters of a Drain leaf compared with a new message (the most recent ones).

Real logs keep leaves far smaller; free text with random first words fills one leaf
with thousands of clusters, which made matching quadratic."""
WILDCARD: Final = "<*>"

logging.getLogger("drain3").setLevel(logging.WARNING)


@dataclass(frozen=True)
class MiningConfig:
    """Drain parameters (see the drain3 documentation)."""

    similarity_threshold: float = 0.4
    depth: int = 4
    max_children: int = 100
    max_clusters: int = 200_000


@dataclass(frozen=True)
class Template:
    """A final template: stable id, text, and the share of its tokens that are literal."""

    template_id: str
    text: str
    literal_share: float


def mining_text(message: str) -> str:
    """The part of a message that is templated: its first line, bounded in length."""
    return message.partition("\n")[0].strip()[:MAX_MINING_CHARS]


def template_id(text: str) -> str:
    """A stable 16-hex-digit id for a template text."""
    return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


def is_placeholder(token: str) -> bool:
    """True for a whole-token placeholder (``<IP>``, ``<*>``)."""
    return token[:1] == "<" and token[-1:] == ">" and PLACEHOLDER.fullmatch(token) is not None


def literal_share(text: str) -> float:
    """Share of the template's tokens that contain no placeholder (1.0 for no tokens)."""
    tokens = text.split()
    if not tokens:
        return 1.0
    return sum(1 for token in tokens if PLACEHOLDER.search(token) is None) / len(tokens)


class TypedDrain(Drain):  # type: ignore[misc]
    """Drain whose similarity treats typed placeholders as evidence of type, not identity.

    Two rules on top of plain Drain (``similar tokens / all tokens``):

    - a typed placeholder facing a literal or another type (``peer`` vs ``<IP>``) counts
      against the similarity: masking already proved those values are of different
      kinds, so ``Connection reset by peer`` and ``Connection refused by <IP>`` stay apart;
    - two equal typed placeholders (``<NUM>`` vs ``<NUM>``) are neutral: they say the
      values have the same kind, not that the events are the same, so ``<NUM> normal``
      and ``<NUM> running`` stay apart. Only literal tokens count as similar.

    Pairs differing at a single position are still unified afterwards by
    :func:`merge_templates`.
    """

    def fast_match(
        self, cluster_ids: Sequence[int], tokens: list[str], sim_th: float, include_params: bool
    ) -> object:
        """Drain's best match, among the leaf's most recent clusters only."""
        return super().fast_match(
            cluster_ids[-MAX_LEAF_CANDIDATES:], tokens, sim_th, include_params
        )

    def get_seq_distance(
        self, seq1: Sequence[str], seq2: Sequence[str], include_params: bool
    ) -> tuple[float, int]:
        considered = 0
        similar = 0.0
        params = 0
        for template_token, token in zip(seq1, seq2, strict=True):
            if template_token == self.param_str:
                params += 1
                considered += 1
            elif template_token == token:
                if is_placeholder(template_token):
                    continue  # same kind of value: neutral
                similar += 1
                considered += 1
            else:
                considered += 1
                if is_placeholder(template_token) or is_placeholder(token):
                    similar -= 1  # a typed value against a different kind of value
        if include_params:
            similar += params
        return (similar / considered if considered else 1.0), params


class TemplateMiner:
    """Drain clustering of masked messages, with optional persisted state."""

    def __init__(self, state_path: Path | None = None, config: MiningConfig | None = None) -> None:
        config = config or MiningConfig()
        drain_config = TemplateMinerConfig()
        drain_config.profiling_enabled = False
        drain_config.drain_sim_th = config.similarity_threshold
        drain_config.drain_depth = config.depth
        drain_config.drain_max_children = config.max_children
        drain_config.drain_max_clusters = config.max_clusters
        drain_config.parametrize_numeric_tokens = True
        drain_config.masking_instructions = []
        drain_config.snapshot_interval_minutes = 10**6  # we save explicitly
        drain_config.snapshot_compress_state = True
        self._persistence = None
        if state_path is not None:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            self._persistence = FilePersistence(str(state_path))
        # The state file is attached only to load and save: drain3 otherwise saves its whole
        # state after every new cluster, which is quadratic on logs with many templates.
        self._drain = Drain3Miner(self._persistence, drain_config)
        self._drain.persistence_handler = None
        self._drain.drain.__class__ = TypedDrain  # also persisted, so reloads keep the rule

    def add(self, masked: str) -> int:
        """Cluster one masked message; returns its cluster id (0 for an empty message)."""
        if not masked.split():
            return 0
        result = self._drain.add_log_message(masked)
        return int(result["cluster_id"])

    def templates(self) -> dict[int, str]:
        """Current template of every cluster, by cluster id."""
        return {
            cluster.cluster_id: cluster.get_template() for cluster in self._drain.drain.clusters
        }

    def save(self) -> None:
        """Persist the state, once (no-op without a state file)."""
        if self._persistence is not None:
            self._drain.persistence_handler = self._persistence
            try:
                self._drain.save_state("loglens")
            finally:
                self._drain.persistence_handler = None


def merge_templates(templates: dict[int, str]) -> dict[int, str]:
    """Merge templates that differ at one position where at least one has a placeholder.

    Returns the (possibly merged) template text for every cluster id. Merges are
    transitive; merged positions become ``<*>``. Deterministic for a given input.
    """
    parent = {cluster: cluster for cluster in templates}

    def find(cluster: int) -> int:
        while parent[cluster] != cluster:
            parent[cluster] = parent[parent[cluster]]
            cluster = parent[cluster]
        return cluster

    tokens = {cluster: text.split() for cluster, text in templates.items()}
    buckets: dict[tuple[int, int, tuple[str, ...]], list[int]] = defaultdict(list)
    for cluster in sorted(templates):
        words = tokens[cluster]
        for position in range(len(words)):
            key = (len(words), position, (*words[:position], *words[position + 1 :]))
            buckets[key].append(cluster)
    for (_, position, _), clusters in buckets.items():
        anchors = [c for c in clusters if is_placeholder(tokens[c][position])]
        if not anchors:
            continue
        for cluster in clusters:
            root_a, root_b = find(anchors[0]), find(cluster)
            if root_a != root_b:
                parent[max(root_a, root_b)] = min(root_a, root_b)

    groups: dict[int, list[int]] = defaultdict(list)
    for cluster in templates:
        groups[find(cluster)].append(cluster)
    merged: dict[int, str] = {}
    for members in groups.values():
        columns = zip(*(tokens[m] for m in members), strict=True)
        text = " ".join(col[0] if len(set(col)) == 1 else WILDCARD for col in columns)
        for member in members:
            merged[member] = text if len(members) > 1 else templates[member]
    return merged


def final_templates(templates: dict[int, str]) -> dict[int, Template]:
    """Merged templates with their ids and literal shares, by cluster id."""
    result: dict[int, Template] = {0: Template(template_id(""), "", 1.0)}
    for cluster, text in merge_templates(templates).items():
        result[cluster] = Template(template_id(text), text, literal_share(text))
    return result


def extract_params(masked: str, mask_values: Sequence[str], template: str) -> list[str]:
    """The values behind the template's placeholders, in order.

    Typed placeholders take their value from ``mask_values``. A ``<*>`` position takes the
    message token, with any typed placeholders in it filled back in. If the template does
    not align with the message token by token, the mask values are returned as they are.
    """
    message_tokens = masked.split()
    template_tokens = template.split()
    if len(message_tokens) != len(template_tokens):
        return list(mask_values)
    values: Iterator[str] = iter(mask_values)
    params: list[str] = []
    for template_token, message_token in zip(template_tokens, message_tokens, strict=True):
        count = len(PLACEHOLDER.findall(message_token))
        if template_token == WILDCARD or (
            is_placeholder(template_token) and template_token != message_token
        ):
            params.append(_fill(message_token, values))
        else:
            params.extend(next(values, "") for _ in range(count))
    return params


def _fill(token: str, values: Iterator[str]) -> str:
    return PLACEHOLDER.sub(lambda _match: next(values, ""), token)


def mine(masked_messages: Iterable[str], miner: TemplateMiner) -> Iterator[tuple[str, int]]:
    """Cluster messages in order; yields (masked message, cluster id)."""
    for masked in masked_messages:
        yield masked, miner.add(masked)
