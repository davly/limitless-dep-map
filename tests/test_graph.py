"""Graph adjacency + filter tests.

Pure in-memory tests — they construct :class:`Edge` lists directly and
exercise the :class:`Graph` API without touching disk.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dep_map.graph import (  # noqa: E402
    _NON_FIREWALL_HUBS,
    EMERGENT_HUB_MIN_DEGREE,
    FIREWALL_HUBS,
    Graph,
)
from dep_map.scanner import HUB_NAMES, Edge, NodeKind  # noqa: E402


def _e(c: str, p: str, k: str = "go") -> Edge:
    return Edge(consumer=c, producer=p, kind=k)


class TestGraphConstruction(unittest.TestCase):
    def test_add_edge_populates_indices(self) -> None:
        g = Graph()
        g.add_edge(_e("casino", "reality"))
        self.assertIn("reality", g.out["casino"])
        self.assertIn("casino", g.incoming["reality"])
        self.assertEqual(g.edge_count(), 1)
        self.assertEqual(g.node_count(), 2)

    def test_idempotent_on_duplicate(self) -> None:
        g = Graph()
        g.add_edge(_e("casino", "reality"))
        g.add_edge(_e("casino", "reality"))
        self.assertEqual(g.edge_count(), 1)

    def test_different_kinds_are_distinct(self) -> None:
        g = Graph()
        g.add_edge(_e("foo", "bar", "go"))
        g.add_edge(_e("foo", "bar", "rust"))
        # Same consumer/producer pair but two kinds: stored as one edge
        # in the producer set (the indices don't carry kind), but the
        # edges list carries both.
        self.assertEqual(g.edge_count(), 2)
        self.assertEqual(g.edge_kind[("foo", "bar")], {"go", "rust"})

    def test_from_edges_factory(self) -> None:
        g = Graph.from_edges([_e("a", "b"), _e("b", "c")])
        self.assertEqual(g.edge_count(), 2)
        self.assertEqual(g.node_count(), 3)


class TestQuery(unittest.TestCase):
    def setUp(self) -> None:
        self.g = Graph.from_edges([
            _e("casino", "reality"),
            _e("ledger", "reality"),
            _e("folio", "limitless-beam-otp"),
            _e("dispatch", "limitless-beam-otp", "beam"),
            _e("dispatch", "reality", "go"),
        ])

    def test_consumers_of_hub(self) -> None:
        self.assertEqual(
            self.g.consumers_of("limitless-beam-otp"),
            {"folio", "dispatch"},
        )

    def test_producers_of_consumer(self) -> None:
        self.assertEqual(
            self.g.producers_of("dispatch"),
            {"limitless-beam-otp", "reality"},
        )

    def test_consumers_of_missing(self) -> None:
        self.assertEqual(self.g.consumers_of("ghost"), set())

    def test_contains(self) -> None:
        self.assertIn(_e("casino", "reality"), self.g)
        self.assertNotIn(_e("casino", "limitless-beam-otp"), self.g)


class TestFilters(unittest.TestCase):
    def setUp(self) -> None:
        self.g = Graph.from_edges([
            _e("casino", "reality"),
            _e("nexus", "reality"),
            _e("oracle", "reality"),
            _e("foundry", "limitless-rs", "rust"),
        ])
        self.g.node_kinds = {
            "casino": NodeKind.FLAGSHIP,
            "nexus": NodeKind.INFRASTRUCTURE,
            "oracle": NodeKind.ENGINE,
            "reality": NodeKind.FOUNDATION,
            "foundry": NodeKind.FLAGSHIP,
            "limitless-rs": NodeKind.HUB,
        }

    def test_filter_layer_infra(self) -> None:
        sub = self.g.filter_layer([NodeKind.INFRASTRUCTURE, NodeKind.ENGINE, NodeKind.FOUNDATION])
        # nexus -> reality (infra->foundation) and oracle -> reality
        # (engine->foundation) survive; casino->reality (flagship)
        # drops; foundry->limitless-rs drops.
        self.assertEqual(sub.edge_count(), 2)
        nodes = sub.all_nodes()
        self.assertIn("nexus", nodes)
        self.assertIn("oracle", nodes)
        self.assertIn("reality", nodes)
        self.assertNotIn("casino", nodes)
        self.assertNotIn("foundry", nodes)

    def test_filter_firewall_only_hubs(self) -> None:
        sub = self.g.filter_firewall()
        # All cohort-hub producers are kept. Bare "reality" is the
        # parser-emitted spelling and IS a firewall hub, so
        # casino/nexus/oracle -> reality survive alongside
        # foundry -> limitless-rs. (Historically the hub table spelled
        # it "foundation/reality" — a name no parser emits — and this
        # test passed vacuously because the reality edges were silently
        # dropped before the predicate loop ran.)
        self.assertEqual(sub.edge_count(), 4)
        for edge in sub.edges:
            self.assertTrue(
                edge.producer in FIREWALL_HUBS,
                f"non-hub edge survived firewall filter: {edge}",
            )

    def test_filter_firewall_keeps_known_hub(self) -> None:
        # Ensure limitless-rs (canonical hub) is kept.
        self.assertIn("limitless-rs", FIREWALL_HUBS)
        sub = self.g.filter_firewall()
        self.assertIn(_e("foundry", "limitless-rs", "rust"), sub.edges)


class TestFirewallHubsDerivation(unittest.TestCase):
    """dm-BU8: FIREWALL_HUBS is derived from the single hub table.

    There is no longer a hand-maintained second list. FIREWALL_HUBS is
    exactly HUB_NAMES minus the explicit non-firewall exclusions, so a hub
    added to HUB_NAMES cannot silently fall out of the firewall audit.
    """

    def test_firewall_is_hub_names_minus_exclusions(self) -> None:
        self.assertEqual(FIREWALL_HUBS, HUB_NAMES - _NON_FIREWALL_HUBS)

    def test_firewall_is_a_subset_of_hub_names(self) -> None:
        self.assertTrue(FIREWALL_HUBS <= HUB_NAMES)

    def test_only_excluded_hub_is_forge_go(self) -> None:
        # Locks in the pre-unification membership (the sole difference was
        # forge-go); a new exclusion must be a deliberate edit here.
        self.assertEqual(HUB_NAMES - FIREWALL_HUBS, frozenset({"forge-go"}))

    def test_every_hub_except_exclusions_is_a_firewall_pin(self) -> None:
        for hub in HUB_NAMES:
            self.assertEqual(
                hub in FIREWALL_HUBS,
                hub not in _NON_FIREWALL_HUBS,
                msg=f"{hub} firewall membership diverged from the table",
            )


class TestHubDegree(unittest.TestCase):
    def test_degree_centrality_orders_hubs(self) -> None:
        g = Graph.from_edges([
            _e("a", "limitless-py", "python"),
            _e("b", "limitless-py", "python"),
            _e("c", "limitless-py", "python"),
            _e("a", "limitless-rs", "rust"),
        ])
        degree = g.hub_degree()
        self.assertEqual(degree.get("limitless-py"), 3)
        self.assertEqual(degree.get("limitless-rs"), 1)

    def test_foundation_hubs_ranked_under_emitted_names(self) -> None:
        # Regression (2026-07-11): reality/aicore/knowledge/foundation are
        # what the Go parser emits (repo tail of github.com/davly/<repo>),
        # and they must rank via HUB_NAMES membership — every fixture node
        # here is BELOW the emergent-degree threshold, so the safeguard
        # cannot mask a missing allowlist entry.
        g = Graph.from_edges([
            _e("casino", "reality"),
            _e("nexus", "aicore"),
            _e("oracle", "knowledge"),
            _e("ledger", "foundation"),
        ])
        degree = g.hub_degree()
        self.assertEqual(degree.get("reality"), 1)
        self.assertEqual(degree.get("aicore"), 1)
        self.assertEqual(degree.get("knowledge"), 1)
        self.assertEqual(degree.get("foundation"), 1)

    def test_emergent_hub_above_threshold_is_reported(self) -> None:
        # Safeguard: a node with >= EMERGENT_HUB_MIN_DEGREE consumers is a
        # de-facto hub and must be reported even though HUB_NAMES omits
        # it — a dead/misspelled allowlist entry can no longer hide a
        # heavily-consumed hub.
        g = Graph.from_edges([
            _e("a", "hotlib"),
            _e("b", "hotlib"),
            _e("c", "hotlib"),
        ])
        self.assertNotIn("hotlib", HUB_NAMES)
        self.assertEqual(g.hub_degree().get("hotlib"), 3)

    def test_below_threshold_non_hub_stays_hidden(self) -> None:
        # The hub lens is preserved: a low-degree non-allowlisted node
        # does not leak into the ranking.
        g = Graph.from_edges([
            _e("a", "warmlib"),
            _e("b", "warmlib"),
            _e("a", "limitless-py", "python"),
        ])
        degree = g.hub_degree()
        self.assertNotIn("warmlib", degree)
        self.assertEqual(degree.get("limitless-py"), 1)

    def test_emergent_threshold_is_locked(self) -> None:
        # Locks the calibrated value (see graph.py comment): 3 adds zero
        # noise on the live estate today. Changing it must be a
        # deliberate edit here.
        self.assertEqual(EMERGENT_HUB_MIN_DEGREE, 3)


class TestIteration(unittest.TestCase):
    def test_iter_yields_edges(self) -> None:
        g = Graph.from_edges([_e("a", "b"), _e("c", "d")])
        edges = list(iter(g))
        self.assertEqual(len(edges), 2)

    def test_len_matches_edge_count(self) -> None:
        g = Graph.from_edges([_e("a", "b"), _e("c", "d"), _e("e", "f")])
        self.assertEqual(len(g), 3)


class TestTransitiveReachability(unittest.TestCase):
    def setUp(self) -> None:
        # casino -> reality ; ledger -> casino ; report -> ledger
        # so reality's transitive consumers = {casino, ledger, report}
        self.g = Graph.from_edges([
            _e("casino", "reality"),
            _e("ledger", "casino"),
            _e("report", "ledger"),
            _e("folio", "limitless-rs", "rust"),
        ])

    def test_transitive_consumers_blast_radius(self) -> None:
        self.assertEqual(
            self.g.transitive_consumers("reality"),
            {"casino", "ledger", "report"},
        )

    def test_transitive_consumers_excludes_self(self) -> None:
        self.assertNotIn("reality", self.g.transitive_consumers("reality"))

    def test_transitive_consumers_leaf(self) -> None:
        self.assertEqual(self.g.transitive_consumers("report"), set())

    def test_transitive_consumers_unknown(self) -> None:
        self.assertEqual(self.g.transitive_consumers("ghost"), set())

    def test_transitive_producers_full_upstream(self) -> None:
        self.assertEqual(
            self.g.transitive_producers("report"),
            {"ledger", "casino", "reality"},
        )

    def test_transitive_producers_excludes_self(self) -> None:
        self.assertNotIn("report", self.g.transitive_producers("report"))


class TestFullGraphExport(unittest.TestCase):
    """dm-BU1: export_edges / export_nodes / export_graph / export_stats.

    Library-level proof of the deterministic, JSON-serialisable whole-DAG
    export — independent of the CLI envelope.
    """

    def setUp(self) -> None:
        self.g = Graph.from_edges([
            _e("casino", "reality", "go"),
            _e("foundry", "limitless-rs", "rust"),
        ])
        self.g.node_kinds = {
            "casino": NodeKind.FLAGSHIP,
            "foundry": NodeKind.FLAGSHIP,
            "reality": NodeKind.FOUNDATION,
            "limitless-rs": NodeKind.HUB,
        }

    def test_export_edges_sorted_schema(self) -> None:
        self.assertEqual(
            self.g.export_edges(),
            [
                {"consumer": "casino", "kind": "go", "producer": "reality"},
                {"consumer": "foundry", "kind": "rust", "producer": "limitless-rs"},
            ],
        )

    def test_export_edge_count_matches_edge_count(self) -> None:
        self.assertEqual(len(self.g.export_edges()), self.g.edge_count())

    def test_export_nodes_surfaces_layers(self) -> None:
        self.assertEqual(
            self.g.export_nodes(),
            [
                {"kind": "flagship", "name": "casino"},
                {"kind": "flagship", "name": "foundry"},
                {"kind": "hub", "name": "limitless-rs"},
                {"kind": "foundation", "name": "reality"},
            ],
        )

    def test_export_nodes_defaults_to_unknown_layer(self) -> None:
        g = Graph.from_edges([_e("a", "b")])  # node_kinds never populated
        self.assertEqual(
            g.export_nodes(),
            [
                {"kind": "unknown", "name": "a"},
                {"kind": "unknown", "name": "b"},
            ],
        )

    def test_export_graph_combines_edges_and_nodes(self) -> None:
        gg = self.g.export_graph()
        self.assertEqual(set(gg), {"edges", "nodes"})
        self.assertEqual(gg["edges"], self.g.export_edges())
        self.assertEqual(gg["nodes"], self.g.export_nodes())

    def test_export_stats(self) -> None:
        self.assertEqual(
            self.g.export_stats(),
            {
                "edge_count": 2,
                "edge_kinds": {"go": 1, "rust": 1},
                "has_cycle": False,
                "node_count": 4,
                "node_kinds": {"flagship": 2, "foundation": 1, "hub": 1},
            },
        )

    def test_export_edges_groups_same_pair_distinct_kinds(self) -> None:
        g = Graph.from_edges([
            _e("poly", "limitless-rs", "go"),
            _e("poly", "limitless-rs", "rust"),
        ])
        self.assertEqual(
            g.export_edges(),
            [
                {"consumer": "poly", "kind": "go", "producer": "limitless-rs"},
                {"consumer": "poly", "kind": "rust", "producer": "limitless-rs"},
            ],
        )


class TestTopologicalOrderAndCycle(unittest.TestCase):
    def test_acyclic_orders_producers_first(self) -> None:
        g = Graph.from_edges([
            _e("casino", "reality"),
            _e("ledger", "casino"),
        ])
        order = g.topological_order()
        # producer must precede the consumer that depends on it
        self.assertLess(order.index("reality"), order.index("casino"))
        self.assertLess(order.index("casino"), order.index("ledger"))
        self.assertFalse(g.has_cycle())

    def test_deterministic_tie_break(self) -> None:
        g = Graph.from_edges([_e("b", "hub"), _e("a", "hub")])
        # 'hub' first (in-degree 0), then a, b sorted.
        self.assertEqual(g.topological_order(), ["hub", "a", "b"])

    def test_cycle_detected(self) -> None:
        g = Graph.from_edges([
            _e("a", "b"),
            _e("b", "c"),
            _e("c", "a"),
        ])
        self.assertTrue(g.has_cycle())
        with self.assertRaises(ValueError):
            g.topological_order()

    def test_empty_graph_ok(self) -> None:
        g = Graph()
        self.assertEqual(g.topological_order(), [])
        self.assertFalse(g.has_cycle())


if __name__ == "__main__":
    unittest.main()
