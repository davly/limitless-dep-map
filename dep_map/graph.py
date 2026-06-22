"""Bidirectional dependency graph.

A thin in-memory adjacency representation that stays substrate-agnostic.
The scanner emits :class:`~dep_map.scanner.Edge` triples; this module
collects them into an O(1)-lookup graph that can answer "who consumes
``foundation/reality``?" and "what does ``insights`` depend on?" without
re-walking disk.

R145 firewall: stdlib only (``collections``, ``dataclasses``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from dep_map.scanner import Edge, HUB_NAMES, NodeKind, Scanner


# Cohort hubs whose consumer edges participate in the R145.C firewall.
# The firewall snapshot draws only edges whose producer is in this set.
# Membership is intentionally broader than ``HUB_NAMES`` minus a few
# entries — we want every shared-lib (BEAM OTP, C crypto, evidence
# bundle, Mirror-Mark verifier) to count as a firewall pin source so the
# audit answer "is every cross-substrate port pinned?" stays honest.
FIREWALL_HUBS: frozenset[str] = frozenset(
    {
        "limitless-beam-otp",
        "limitless-c-crypto",
        "limitless-py",
        "limitless-rs",
        "limitless-ts",
        "limitless-jvm",
        "limitless-hs",
        "limitless-evidence-bundle",
        "limitless-ai-watermark",
        "limitless-almanac-cohort",
        "lore-mark-verify",
        "limitless-cohort-map",
        "limitless-dep-map",
        "limitless-sdk",
        "limitless-proto",
        "limitless-solidity",
        "limitless-cpp",
        "limitless-dotnet",
        "limitless-ui",
        "foundation/reality",
        "foundation/pkg",
        "foundation/aicore",
        "foundation/knowledge",
        "nexus-ai",
    }
)


@dataclass
class Graph:
    """Bidirectional adjacency over ``(consumer, producer)`` edges.

    Two indices, two purposes:

    * ``out`` maps consumer -> set of producers — answers "what does X
      depend on?". This is the most-asked question at render time
      because every node is drawn as ``edge.consumer``.
    * ``incoming`` maps producer -> set of consumers — answers "who
      consumes hub Y?". Used by the R145.C firewall snapshot and by
      degree-centrality ordering of hubs.

    The ``edges`` list is the source of truth (de-duped, stable order
    after :meth:`finalise`). ``out`` and ``incoming`` are populated
    eagerly so the graph is a simple value object — no caches that can
    go stale.
    """

    edges: list[Edge] = field(default_factory=list)
    out: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    incoming: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    node_kinds: dict[str, NodeKind] = field(default_factory=dict)
    edge_kind: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))

    # ------------------------------------------------------------------
    # Construction.
    # ------------------------------------------------------------------

    def add_edge(self, edge: Edge) -> None:
        """Add a single edge (idempotent on duplicate triples)."""
        if edge.producer in self.out[edge.consumer] and edge.kind in self.edge_kind[(edge.consumer, edge.producer)]:
            return
        self.edges.append(edge)
        self.out[edge.consumer].add(edge.producer)
        self.incoming[edge.producer].add(edge.consumer)
        self.edge_kind[(edge.consumer, edge.producer)].add(edge.kind)

    def add_edges(self, edges: Iterable[Edge]) -> None:
        for e in edges:
            self.add_edge(e)

    @classmethod
    def from_edges(cls, edges: Iterable[Edge]) -> "Graph":
        g = cls()
        g.add_edges(edges)
        return g

    @classmethod
    def from_scanner(cls, scanner: Scanner) -> "Graph":
        """Convenience: walk a scanner end-to-end and return a populated graph."""
        g = cls()
        g.add_edges(scanner.scan())
        g.classify_nodes(scanner)
        return g

    def classify_nodes(self, scanner: Scanner) -> None:
        """Fill :attr:`node_kinds` using the scanner's path layout.

        We classify every consumer + every producer. Consumers we know
        the path of (we just walked it). Producers we resolve by best-
        effort: hubs become :attr:`NodeKind.HUB`; foundation paths
        become :attr:`NodeKind.FOUNDATION`; remaining producers default
        to :attr:`NodeKind.UNKNOWN` and are filled in later if they
        appear as a consumer elsewhere in the walk.
        """
        # Build a name->layer map by re-walking; cheap because layer
        # directories are shallow.
        layer_map: dict[str, NodeKind] = {}
        for layer in scanner.LAYERS:
            layer_path = scanner.root / layer
            if not layer_path.is_dir():
                continue
            for child in layer_path.iterdir():
                if not child.is_dir():
                    continue
                # Synthesise a fake manifest path so classify() works.
                fake = child / "go.mod"
                layer_map[child.name] = scanner.classify(fake)
        for node in self.all_nodes():
            if node in layer_map:
                self.node_kinds[node] = layer_map[node]
            elif node in HUB_NAMES or node.startswith("limitless-") or node.startswith("foundation/"):
                self.node_kinds[node] = NodeKind.HUB
            else:
                self.node_kinds[node] = NodeKind.UNKNOWN

    # ------------------------------------------------------------------
    # Query.
    # ------------------------------------------------------------------

    def all_nodes(self) -> set[str]:
        """Set of every node referenced by any edge (consumer or producer)."""
        nodes: set[str] = set()
        for e in self.edges:
            nodes.add(e.consumer)
            nodes.add(e.producer)
        return nodes

    def consumers_of(self, producer: str) -> set[str]:
        """Return the (potentially empty) set of consumers of ``producer``."""
        return set(self.incoming.get(producer, set()))

    def producers_of(self, consumer: str) -> set[str]:
        """Return the (potentially empty) set of producers of ``consumer``."""
        return set(self.out.get(consumer, set()))

    def edge_count(self) -> int:
        return len(self.edges)

    def node_count(self) -> int:
        return len(self.all_nodes())

    def hub_degree(self) -> dict[str, int]:
        """Map every hub node to its in-degree (number of consumers).

        Used by the renderer to order hubs by popularity — the most-
        consumed hub gets the centre slot.
        """
        return {node: len(self.incoming.get(node, set())) for node in HUB_NAMES if node in self.all_nodes()}

    def transitive_consumers(self, producer: str) -> set[str]:
        """All nodes that consume ``producer`` directly OR transitively.

        Reverse-edge BFS over ``incoming``. This is the **blast radius**
        of a change to ``producer`` — every node a cohort migration of
        ``producer`` would ripple into. Excludes ``producer`` itself.
        Returns ``set()`` for an unknown node.
        """
        if producer not in self.all_nodes():
            return set()
        seen: set[str] = set()
        stack = [producer]
        while stack:
            cur = stack.pop()
            for consumer in self.incoming.get(cur, set()):
                if consumer not in seen:
                    seen.add(consumer)
                    stack.append(consumer)
        return seen

    def transitive_producers(self, consumer: str) -> set[str]:
        """All nodes ``consumer`` depends on directly OR transitively.

        Forward-edge BFS over ``out``. The full upstream closure of
        ``consumer``. Excludes ``consumer`` itself. ``set()`` if unknown.
        """
        if consumer not in self.all_nodes():
            return set()
        seen: set[str] = set()
        stack = [consumer]
        while stack:
            cur = stack.pop()
            for producer in self.out.get(cur, set()):
                if producer not in seen:
                    seen.add(producer)
                    stack.append(producer)
        return seen

    def topological_order(self) -> list[str]:
        """Producers-first ordering via Kahn's algorithm.

        Deterministic: ties broken by sorted node name (R145.C audit
        snapshots must be byte-reproducible). Raises :class:`ValueError`
        if the declared-edge graph contains a cycle.

        Edge semantics: ``consumer -> producer`` means consumer depends
        on producer, so producers (in-degree 0 in the *depends-on*
        sense) come first.
        """
        nodes = self.all_nodes()
        # in-degree = number of producers each consumer depends on.
        indeg = {n: 0 for n in nodes}
        for consumer in nodes:
            indeg[consumer] = len(self.out.get(consumer, set()) & nodes)
        ready = sorted([n for n, d in indeg.items() if d == 0])
        out_order: list[str] = []
        while ready:
            cur = ready.pop(0)
            out_order.append(cur)
            newly: list[str] = []
            for consumer in sorted(self.incoming.get(cur, set())):
                if consumer in indeg:
                    indeg[consumer] -= 1
                    if indeg[consumer] == 0:
                        newly.append(consumer)
            ready = sorted(ready + newly)
        if len(out_order) != len(nodes):
            raise ValueError(
                f"cycle detected: {len(out_order)} of {len(nodes)} nodes ordered"
            )
        return out_order

    def has_cycle(self) -> bool:
        """True if the declared-edge graph contains an import cycle.

        A cross-substrate declared-dependency cycle is an R145.C smell
        the dep-map audit should surface.
        """
        try:
            self.topological_order()
        except ValueError:
            return True
        return False

    # ------------------------------------------------------------------
    # Filter sub-views (used by CLI snapshot modes).
    # ------------------------------------------------------------------

    def filter_layer(self, kinds: Iterable[NodeKind]) -> "Graph":
        """Return a new graph keeping only edges whose endpoints are in ``kinds``.

        Used by ``--layer=infra`` to surface only ``infrastructure/`` +
        ``engines/`` nodes. Edges whose other endpoint falls outside the
        layer are dropped — the view is "the infra layer talking to
        itself".
        """
        allowed: set[NodeKind] = set(kinds)
        kept = Graph()
        for e in self.edges:
            ck = self.node_kinds.get(e.consumer, NodeKind.UNKNOWN)
            pk = self.node_kinds.get(e.producer, NodeKind.UNKNOWN)
            if ck in allowed and pk in allowed:
                kept.add_edge(e)
        # Carry over node-kind classification for surviving nodes.
        for node in kept.all_nodes():
            kept.node_kinds[node] = self.node_kinds.get(node, NodeKind.UNKNOWN)
        return kept

    def filter_firewall(self) -> "Graph":
        """Return a new graph keeping only R145.C cohort firewall edges.

        An edge counts as a firewall pin when the **producer** is a known
        cohort hub (see :data:`FIREWALL_HUBS`). The firewall snapshot
        answers "which flagships consume the cohort hubs", which is the
        question R145.C exists to surface.
        """
        kept = Graph()
        for e in self.edges:
            if e.producer in FIREWALL_HUBS:
                kept.add_edge(e)
        for node in kept.all_nodes():
            kept.node_kinds[node] = self.node_kinds.get(node, NodeKind.UNKNOWN)
        return kept

    # ------------------------------------------------------------------
    # Iteration / repr.
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[Edge]:
        return iter(self.edges)

    def __len__(self) -> int:
        return self.edge_count()

    def __contains__(self, edge: Edge) -> bool:
        return edge.producer in self.out.get(edge.consumer, set())
