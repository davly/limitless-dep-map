"""End-to-end tests for the ``dep-map query`` sub-command.

The DAG queries (blast-radius, upstream, consumers, producers, has-cycle,
topo, hub-degree) lived only on :class:`dep_map.graph.Graph` and were
unreachable from the CLI. The ``query`` sub-command exposes them as a
deterministic JSON envelope. These tests drive ``cli.main(argv)`` against
a real on-disk fake monorepo and assert on the parsed JSON + exit codes.

Determinism is part of the contract under test: object keys are sorted,
node-set results are name-sorted, hub-degree is descending-degree then
name, and re-running the same query yields byte-identical stdout.

Stdlib-only (``json`` + ``contextlib`` + ``io`` + ``tempfile`` +
``unittest``) per the R145 firewall.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dep_map import cli  # noqa: E402


def _mkroot() -> Path:
    root = Path(tempfile.mkdtemp(prefix="depmap_query_test_"))
    for layer in ("flagships", "infrastructure", "engines", "foundation", "sdk"):
        (root / layer).mkdir()
    return root


def _gomod(root: Path, name: str, *requires: str) -> None:
    """Write flagships/<name>/go.mod requiring each github.com/davly/<req>."""
    body = [f"module github.com/davly/{name}"]
    for req in requires:
        body.append(f"require github.com/davly/{req} v0.0.0")
    path = root / "flagships" / name / "go.mod"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _chain_root() -> Path:
    # report -> ledger -> casino -> reality   (consumer -> producer)
    root = _mkroot()
    _gomod(root, "casino", "reality")
    _gomod(root, "ledger", "casino")
    _gomod(root, "report", "ledger")
    return root


class TestNodeScopedQueries(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _chain_root()

    def _q(self, kind: str, node: str) -> object:
        code, stdout, err = _run(
            ["query", "--root", str(self.root), kind, "--node", node]
        )
        self.assertEqual(code, 0, msg=err)
        env = json.loads(stdout)
        self.assertEqual(env["query"], kind)
        self.assertEqual(env["node"], node)
        return env["result"]

    def test_blast_radius_is_transitive_consumers_sorted(self) -> None:
        self.assertEqual(
            self._q("blast-radius", "reality"),
            ["casino", "ledger", "report"],
        )

    def test_upstream_is_transitive_producers_sorted(self) -> None:
        self.assertEqual(
            self._q("upstream", "report"),
            ["casino", "ledger", "reality"],
        )

    def test_consumers_are_direct_only(self) -> None:
        self.assertEqual(self._q("consumers", "casino"), ["ledger"])

    def test_producers_are_direct_only(self) -> None:
        self.assertEqual(self._q("producers", "report"), ["ledger"])

    def test_unknown_node_is_not_a_false_clean(self) -> None:
        # dm-BU3: an unknown node is no longer exit-0/[] like a real node
        # with no dependents — it exits 5 and is flagged known=false.
        code, stdout, _ = _run(
            ["query", "--root", str(self.root), "blast-radius", "--node", "ghost"]
        )
        self.assertEqual(code, 5)
        env = json.loads(stdout)
        self.assertIs(env["known"], False)
        self.assertEqual(env["result"], [])


class TestUnknownNodeHonesty(unittest.TestCase):
    """dm-BU3: a typo'd --node must not masquerade as "no dependents".

    Before the fix, ``blast-radius --node typo`` returned ``[]`` with
    exit 0 — byte-identical to a real node that genuinely has no
    dependents. The envelope now carries an explicit ``"known"`` flag and
    an unknown node yields a distinct non-zero exit code, so neither a
    JSON consumer nor a shell ``if`` can be fooled.
    """

    def setUp(self) -> None:
        # report -> ledger -> casino -> reality (consumer -> producer).
        self.root = _chain_root()

    def test_unknown_node_is_flagged_not_known(self) -> None:
        code, stdout, err = _run(
            ["query", "--root", str(self.root), "blast-radius", "--node", "ghost"]
        )
        env = json.loads(stdout)
        self.assertIs(env["known"], False)
        self.assertEqual(env["result"], [])
        self.assertNotEqual(code, 0, msg="unknown node must not exit 0")
        self.assertIn("ghost", err)

    def test_known_node_with_empty_result_is_marked_known(self) -> None:
        # reality is a real node that simply depends on nothing.
        code, stdout, _ = _run(
            ["query", "--root", str(self.root), "producers", "--node", "reality"]
        )
        self.assertEqual(code, 0)
        env = json.loads(stdout)
        self.assertIs(env["known"], True)
        self.assertEqual(env["result"], [])

    def test_unknown_and_known_empty_are_distinguishable(self) -> None:
        # The crux of the false-clean: an unknown node and a known-but-
        # childless node must differ in BOTH exit code and envelope.
        unk = _run(
            ["query", "--root", str(self.root), "producers", "--node", "ghost"]
        )
        known = _run(
            ["query", "--root", str(self.root), "producers", "--node", "reality"]
        )
        self.assertNotEqual(unk[0], known[0], msg="exit codes must differ")
        self.assertNotEqual(unk[1], known[1], msg="stdout envelopes must differ")

    def test_graph_scoped_query_known_is_null(self) -> None:
        # has-cycle/topo/hub-degree have no node; "known" is null.
        code, stdout, _ = _run(["query", "--root", str(self.root), "has-cycle"])
        self.assertEqual(code, 0)
        env = json.loads(stdout)
        self.assertIsNone(env["known"])


class TestGraphScopedQueries(unittest.TestCase):
    def test_has_cycle_false_on_acyclic(self) -> None:
        root = _chain_root()
        code, stdout, _ = _run(["query", "--root", str(root), "has-cycle"])
        self.assertEqual(code, 0)
        env = json.loads(stdout)
        self.assertIs(env["result"], False)
        self.assertIsNone(env["node"])

    def test_has_cycle_true_on_cyclic(self) -> None:
        root = _mkroot()
        _gomod(root, "a", "b")
        _gomod(root, "b", "c")
        _gomod(root, "c", "a")
        code, stdout, _ = _run(["query", "--root", str(root), "has-cycle"])
        self.assertEqual(code, 0)
        self.assertIs(json.loads(stdout)["result"], True)

    def test_topo_orders_producers_first(self) -> None:
        root = _chain_root()
        code, stdout, _ = _run(["query", "--root", str(root), "topo"])
        self.assertEqual(code, 0)
        order = json.loads(stdout)["result"]
        self.assertEqual(order, ["reality", "casino", "ledger", "report"])

    def test_topo_on_cycle_returns_exit_four(self) -> None:
        root = _mkroot()
        _gomod(root, "a", "b")
        _gomod(root, "b", "c")
        _gomod(root, "c", "a")
        code, stdout, err = _run(["query", "--root", str(root), "topo"])
        self.assertEqual(code, 4)
        self.assertEqual(stdout, "")
        self.assertIn("cannot answer 'topo'", err)

    def test_hub_degree_is_descending_degree_then_name(self) -> None:
        root = _mkroot()
        _gomod(root, "a", "limitless-py", "limitless-rs")
        _gomod(root, "b", "limitless-py")
        _gomod(root, "c", "limitless-py")
        code, stdout, _ = _run(["query", "--root", str(root), "hub-degree"])
        self.assertEqual(code, 0)
        result = json.loads(stdout)["result"]
        self.assertEqual(result, [["limitless-py", 3], ["limitless-rs", 1]])


class TestQueryArgErrors(unittest.TestCase):
    def test_node_query_without_node_is_exit_one(self) -> None:
        root = _chain_root()
        code, _, err = _run(["query", "--root", str(root), "blast-radius"])
        self.assertEqual(code, 1)
        self.assertIn("requires --node", err)

    def test_graph_query_with_node_is_exit_one(self) -> None:
        root = _chain_root()
        code, _, err = _run(
            ["query", "--root", str(root), "has-cycle", "--node", "reality"]
        )
        self.assertEqual(code, 1)
        self.assertIn("does not take --node", err)

    def test_bad_root_is_exit_one(self) -> None:
        root = _chain_root()
        missing = root / "nope"
        code, _, err = _run(
            ["query", "--root", str(missing), "topo"]
        )
        self.assertEqual(code, 1)
        self.assertIn("is not a directory", err)


class TestQueryDeterminismAndShape(unittest.TestCase):
    def test_envelope_keys_are_sorted_and_complete(self) -> None:
        root = _chain_root()
        _, stdout, _ = _run(
            ["query", "--root", str(root), "blast-radius", "--node", "reality"]
        )
        env = json.loads(stdout)
        self.assertEqual(sorted(env.keys()), ["known", "node", "query", "result"])
        # sort_keys=True means the serialised key order is known, node,
        # query, result.
        self.assertLess(stdout.index('"known"'), stdout.index('"node"'))
        self.assertLess(stdout.index('"node"'), stdout.index('"query"'))
        self.assertLess(stdout.index('"query"'), stdout.index('"result"'))

    def test_repeated_runs_are_byte_identical(self) -> None:
        root = _mkroot()
        _gomod(root, "a", "limitless-py", "limitless-rs")
        _gomod(root, "b", "limitless-py")
        _gomod(root, "c", "limitless-py")
        first = _run(["query", "--root", str(root), "hub-degree"])[1]
        second = _run(["query", "--root", str(root), "hub-degree"])[1]
        self.assertEqual(first, second)
        self.assertTrue(first.strip())


if __name__ == "__main__":
    unittest.main()
