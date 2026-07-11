"""Bidirectional dependency graph.

A thin in-memory adjacency representation that stays substrate-agnostic.
The scanner emits :class:`~dep_map.scanner.Edge` triples; this module
collects them into an O(1)-lookup graph that can answer "who consumes
``reality``?" and "what does ``insights`` depend on?" without
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
#
# dm-BU8: derived from the single source-of-truth hub table
# (``HUB_NAMES`` in scanner.py) rather than re-listed. The R145.C firewall
# wants every cohort shared-lib (BEAM OTP, C crypto, evidence bundle,
# Mirror-Mark verifier, ...) to count as a pin source, so the default is
# "every hub is a firewall hub" and only explicit exclusions are listed
# below. Deriving instead of duplicating removes the drift risk where a
# hub added to ``HUB_NAMES`` but forgotten here (or vice-versa) silently
# distorts the firewall audit. This preserves the prior membership
# byte-for-byte: the sole pre-unification difference was ``forge-go``.
#
# ``forge-go`` is the lone hub kept out of the firewall snapshot,
# matching the membership before unification (it was never in this set).
_NON_FIREWALL_HUBS: frozenset[str] = frozenset({"forge-go"})
FIREWALL_HUBS: frozenset[str] = HUB_NAMES - _NON_FIREWALL_HUBS

# Safeguard threshold for :meth:`Graph.hub_degree`: any node with at
# least this many direct consumers is a de-facto hub and is reported
# even when ``HUB_NAMES`` does not list it. This exists because the hub
# allowlist has already silently lost the estate's four biggest hubs
# once — the entries were spelled path-style (``foundation/reality``),
# a form no parser branch can emit, so ``reality`` (108 consumers) was
# invisible to the ranking while ``limitless-evidence-bundle`` (4) was
# reported as the top hub. With the safeguard, a spelling drift between
# HUB_NAMES and the scanner's emitted producer names degrades to "the
# hub shows up unlabelled" instead of "the hub disappears". The value 3
# is calibrated against the live estate: after the respelling, no
# non-allowlisted node reaches it today (next tier is in-degree 2), so
# the safeguard adds zero noise now and pure protection later.
EMERGENT_HUB_MIN_DEGREE: int = 3


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

        Used by the ``query hub-degree`` CLI mode, which emits the
        result as part of a byte-reproducible JSON answer ranking where
        a cohort migration would land hardest.

        Hub membership is the union of two sources:

        * the curated ``HUB_NAMES`` allowlist — a well-known hub is
          reported even at degree 0 (a hub nobody consumes yet is still
          a hub); and
        * any **emergent** hub — a node whose in-degree is at least
          :data:`EMERGENT_HUB_MIN_DEGREE`, whether or not the allowlist
          names it. This is the safeguard against dead allowlist
          entries (see the constant's comment): a heavily-consumed node
          can no longer be hidden by a spelling drift between
          ``HUB_NAMES`` and the scanner's emitted producer names.

        The returned dict is ordered by **descending degree, then
        ascending node name** so iteration is deterministic. The previous
        implementation iterated ``HUB_NAMES`` (a ``frozenset``), whose
        traversal order over strings is randomised per-process by
        ``PYTHONHASHSEED`` — that made the key order non-reproducible and
        ignored the documented "by popularity" ordering. We materialise
        the degrees first, then sort, so the contract holds regardless of
        hash seed.
        """
        nodes = self.all_nodes()
        degrees = {
            node: len(self.incoming.get(node, set()))
            for node in HUB_NAMES
            if node in nodes
        }
        for node in nodes:
            if node in degrees:
                continue
            degree = len(self.incoming.get(node, set()))
            if degree >= EMERGENT_HUB_MIN_DEGREE:
                degrees[node] = degree
        return {
            node: degree
            for node, degree in sorted(
                degrees.items(), key=lambda kv: (-kv[1], kv[0])
            )
        }

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
    # Full-graph export (dm-BU1).
    #
    # The query sub-command historically answered one *scalar/list* DAG
    # question at a time (blast-radius, topo, ...). The whole graph —
    # every edge with its substrate kind, every node with its layer — was
    # never machine-readable; an operator had to render the SVG and read
    # it by eye. These exporters surface the entire DAG as
    # deterministically-ordered, JSON-serialisable plain data so CI and
    # scripts can diff the dependency graph byte-for-byte. Every list is
    # explicitly sorted and every dict key is stable, so two runs over an
    # identical filesystem produce byte-identical JSON regardless of
    # ``PYTHONHASHSEED``.
    # ------------------------------------------------------------------

    def export_edges(self) -> list[dict[str, str]]:
        """Every declared edge as ``{consumer, kind, producer}``, sorted.

        One object per ``(consumer, producer, kind)`` triple — the same
        granularity as :attr:`edges` (the source of truth), so
        ``len(export_edges()) == edge_count()``. A consumer/producer pair
        declared in two substrates (e.g. a Go and a Rust edge) appears as
        two objects with distinct ``kind`` values, surfacing the edge
        substrate that was previously visible only inside the SVG. Sorted
        by ``(consumer, producer, kind)`` for byte-reproducibility.
        """
        return [
            {"consumer": e.consumer, "kind": e.kind, "producer": e.producer}
            for e in sorted(
                self.edges, key=lambda e: (e.consumer, e.producer, e.kind)
            )
        ]

    def export_nodes(self) -> list[dict[str, str]]:
        """Every node as ``{kind, name}``, sorted by name.

        ``kind`` is the node's layer (:class:`NodeKind` value:
        ``flagship`` / ``infrastructure`` / ``engine`` / ``foundation`` /
        ``sdk`` / ``hub`` / ``unknown``) — the layer/substrate
        classification that previously only drove SVG colour and was
        never emitted as data. Nodes with no recorded classification
        report ``unknown``. Sorted by name for byte-reproducibility.
        """
        return [
            {
                "kind": self.node_kinds.get(n, NodeKind.UNKNOWN).value,
                "name": n,
            }
            for n in sorted(self.all_nodes())
        ]

    def export_graph(self) -> dict[str, list[dict[str, str]]]:
        """The whole DAG as ``{"edges": [...], "nodes": [...]}``.

        A superset of :meth:`export_edges` + :meth:`export_nodes` — the
        single payload a consumer needs to reconstruct the graph. Both
        sub-lists are deterministically sorted.
        """
        return {"edges": self.export_edges(), "nodes": self.export_nodes()}

    def export_stats(self) -> dict[str, object]:
        """Deterministic summary counts for the whole graph.

        Fields:

        * ``edge_count`` — number of declared ``(consumer, producer,
          kind)`` triples (== ``len(export_edges())``).
        * ``edge_kinds`` — histogram of edges by substrate kind.
        * ``has_cycle`` — whether the declared-edge graph has a cycle.
        * ``node_count`` — number of distinct nodes.
        * ``node_kinds`` — histogram of nodes by :class:`NodeKind` layer.

        The two histograms are plain dicts; ``json.dumps(sort_keys=True)``
        renders their keys in sorted order, so the emitted JSON is
        byte-reproducible. ``edge_kinds`` sums to ``edge_count`` and
        ``node_kinds`` sums to ``node_count``.
        """
        edge_kinds: dict[str, int] = {}
        for e in self.edges:
            edge_kinds[e.kind] = edge_kinds.get(e.kind, 0) + 1
        node_kinds: dict[str, int] = {}
        for n in self.all_nodes():
            layer = self.node_kinds.get(n, NodeKind.UNKNOWN).value
            node_kinds[layer] = node_kinds.get(layer, 0) + 1
        return {
            "edge_count": self.edge_count(),
            "edge_kinds": edge_kinds,
            "has_cycle": self.has_cycle(),
            "node_count": self.node_count(),
            "node_kinds": node_kinds,
        }

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
