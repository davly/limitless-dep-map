"""Graph adjacency + filter tests.

Pure in-memory tests — they construct :class:`Edge` lists directly and
exercise the :class:`Graph` API without touching disk.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dep_map.graph import FIREWALL_HUBS, Graph  # noqa: E402
from dep_map.scanner import Edge, NodeKind  # noqa: E402


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
        # All cohort-hub producers are kept (reality, limitless-rs).
        # casino->reality, nexus->reality, oracle->reality survive
        # because reality is in FIREWALL_HUBS via foundation/reality
        # (we use the bare "reality" name in this fixture so depend on
        # the alias path). Check by predicate.
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
