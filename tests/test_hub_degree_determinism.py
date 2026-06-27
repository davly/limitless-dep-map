"""Cross-seed determinism guard for :meth:`Graph.hub_degree`.

``hub_degree`` is consumed by the renderer to decide which hub gets the
centre slot, and by the ``query hub-degree`` CLI mode which emits the
result as part of a byte-reproducible JSON answer. Both uses require the
*ordering* to be stable: it must not depend on ``PYTHONHASHSEED``.

The historical bug: ``hub_degree`` built its result dict by iterating
``HUB_NAMES`` (a ``frozenset``). ``frozenset`` iteration order over
strings is randomised per-process by hash-seed, so the returned dict's
key order changed run to run, and it was *not* ordered by degree as the
docstring promised.

This test pins the contract two ways:

1. ``test_order_matches_degree_then_name`` — in-process, asserts the
   documented ``(-degree, name)`` ordering.
2. ``test_stable_across_hash_seeds`` — spawns the same construction under
   several distinct ``PYTHONHASHSEED`` values in subprocesses and asserts
   the emitted key order is byte-identical across all of them. With the
   old frozenset-iteration implementation this fails (the orders differ);
   with the sorted implementation it passes.

Stdlib-only (``json`` + ``os`` + ``subprocess`` + ``sys`` + ``unittest``)
per the R145 firewall.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dep_map.graph import Graph  # noqa: E402
from dep_map.scanner import Edge  # noqa: E402


# A fixture with deliberate ties so BOTH ordering keys are exercised:
#   limitless-py  -> degree 3   (unique top)
#   limitless-rs  -> degree 2   } tie broken by name (rs < ts)
#   limitless-ts  -> degree 2   }
#   forge-go      -> degree 1   } three-way tie broken by name
#   limitless-hs  -> degree 1   } (forge-go < limitless-hs < limitless-jvm)
#   limitless-jvm -> degree 1   }
# All six names are members of HUB_NAMES.
_FIXTURE_EDGES = [
    ("a", "limitless-py"),
    ("b", "limitless-py"),
    ("c", "limitless-py"),
    ("a", "limitless-rs"),
    ("b", "limitless-rs"),
    ("a", "limitless-ts"),
    ("b", "limitless-ts"),
    ("a", "forge-go"),
    ("a", "limitless-hs"),
    ("a", "limitless-jvm"),
]

_EXPECTED_ORDER = [
    "limitless-py",
    "limitless-rs",
    "limitless-ts",
    "forge-go",
    "limitless-hs",
    "limitless-jvm",
]

# Subprocess program: build the fixture graph and print the ordered list
# of (hub, degree) pairs as JSON. Kept dependency-free so it can run under
# a bare ``python -c`` with only PYTHONPATH pointing at the repo root.
_SUBPROC_CODE = (
    "import json\n"
    "from dep_map.graph import Graph\n"
    "from dep_map.scanner import Edge\n"
    "edges = [Edge(consumer=c, producer=p, kind='go') for c, p in "
    + repr(_FIXTURE_EDGES)
    + "]\n"
    "g = Graph.from_edges(edges)\n"
    "print(json.dumps(list(g.hub_degree().items())))\n"
)


class TestHubDegreeOrdering(unittest.TestCase):
    def test_order_matches_degree_then_name(self) -> None:
        g = Graph.from_edges(
            [Edge(consumer=c, producer=p, kind="go") for c, p in _FIXTURE_EDGES]
        )
        deg = g.hub_degree()
        self.assertEqual(list(deg.keys()), _EXPECTED_ORDER)
        # Degrees themselves stay correct.
        self.assertEqual(deg["limitless-py"], 3)
        self.assertEqual(deg["limitless-rs"], 2)
        self.assertEqual(deg["forge-go"], 1)

    def test_descending_degree_is_non_increasing(self) -> None:
        g = Graph.from_edges(
            [Edge(consumer=c, producer=p, kind="go") for c, p in _FIXTURE_EDGES]
        )
        degrees = list(g.hub_degree().values())
        self.assertEqual(degrees, sorted(degrees, reverse=True))


class TestHubDegreeCrossSeed(unittest.TestCase):
    def _run_with_seed(self, seed: str) -> list[list[object]]:
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.run(
            [sys.executable, "-c", _SUBPROC_CODE],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"subprocess failed (seed={seed}): {proc.stderr}",
        )
        return json.loads(proc.stdout)

    def test_stable_across_hash_seeds(self) -> None:
        seeds = ["0", "1", "7", "42", "1337", "98765"]
        results = [self._run_with_seed(s) for s in seeds]
        # Every seed must yield byte-identical ordering...
        first = results[0]
        for seed, res in zip(seeds, results):
            self.assertEqual(
                res,
                first,
                msg=f"hub_degree order diverged under PYTHONHASHSEED={seed}",
            )
        # ...and that ordering must be the documented one.
        ordered_keys = [pair[0] for pair in first]
        self.assertEqual(ordered_keys, _EXPECTED_ORDER)


if __name__ == "__main__":
    unittest.main()
