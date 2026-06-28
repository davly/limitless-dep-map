"""Cross-process / hash-seed render determinism guard (dm-BU7).

The Mirror-Mark stamp is an HMAC over the SVG *bytes*, so any
non-determinism in the rendered output silently breaks cold round-trip
verification. ``test_render.TestRenderSVG.test_render_is_byte_deterministic``
already proves the renderer is deterministic *within one process* — but
that case cannot catch a leak of unsorted ``set`` / ``dict`` iteration
order, because the per-process hash seed is fixed for the whole test
run. CPython randomises the hash of ``str``/``bytes`` keys per process
(``PYTHONHASHSEED``), so two *different* processes can iterate the same
set in different orders. If the renderer ever iterated ``all_nodes()`` or
``node_kinds`` without sorting, the SVG would differ across processes and
the Mirror-Mark would stop being reproducible — exactly the failure this
guard exists to catch.

Strategy: render the *same* in-memory graph in a fresh subprocess under
each of ``PYTHONHASHSEED`` in ``{0, 1, 7, 42}`` (0 disables
randomisation; the others each force a distinct seed) and assert the raw
SVG bytes are byte-identical across all four.

Stdlib only (``os`` + ``subprocess`` + ``sys`` + ``tempfile`` +
``unittest``) per the R145 firewall.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Seeds to exercise. 0 disables hash randomisation entirely; 1/7/42 each
# force a distinct, non-zero seed so set/dict key iteration order varies
# between the runs (verified live by
# ``test_hashseed_env_actually_varies_hashing`` below — without that
# precondition this guard could pass vacuously).
SEEDS = (0, 1, 7, 42)

# Inline render program run in each subprocess. It builds a fixed sample
# graph that deliberately exercises the non-deterministic surfaces:
#
#   * ``all_nodes()`` returns a freshly-built ``set`` (10 nodes), whose
#     iteration order is hash-seed-dependent;
#   * ``node_kinds`` is a ``dict`` keyed by node name;
#   * edges span every substrate stroke (go/beam/rust/ts) so substrate
#     ordering is exercised too.
#
# The renderer is contracted to sort all of these before emitting, so the
# bytes must match across seeds. The program writes the *raw* UTF-8 bytes
# of ``render_svg`` to the path in ``argv[1]`` (binary mode — no newline
# translation) so the comparison is over exactly the bytes the
# Mirror-Mark is computed over. ``dep_map`` is importable because the
# test prepends ``REPO_ROOT`` to ``PYTHONPATH`` in the child env.
_RENDER_PROG = r"""
import sys
from pathlib import Path

from dep_map.graph import Graph
from dep_map.render import render_svg
from dep_map.scanner import Edge, NodeKind

g = Graph.from_edges([
    Edge(consumer="casino", producer="reality", kind="go"),
    Edge(consumer="ledger", producer="reality", kind="go"),
    Edge(consumer="echo", producer="reality", kind="go"),
    Edge(consumer="folio", producer="limitless-beam-otp", kind="beam"),
    Edge(consumer="insights", producer="limitless-rs", kind="rust"),
    Edge(consumer="parallax", producer="limitless-ts", kind="ts"),
])
g.node_kinds = {
    "casino": NodeKind.FLAGSHIP,
    "ledger": NodeKind.FLAGSHIP,
    "echo": NodeKind.ENGINE,
    "folio": NodeKind.FLAGSHIP,
    "insights": NodeKind.FLAGSHIP,
    "parallax": NodeKind.ENGINE,
    "reality": NodeKind.FOUNDATION,
    "limitless-beam-otp": NodeKind.HUB,
    "limitless-rs": NodeKind.HUB,
    "limitless-ts": NodeKind.HUB,
}
svg = render_svg(g)
Path(sys.argv[1]).write_bytes(svg.encode("utf-8"))
"""


def _child_env(seed: int) -> dict[str, str]:
    """Child-process env: pin ``PYTHONHASHSEED`` and expose the package."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    )
    return env


def _render_in_subprocess(seed: int, out_path: Path) -> bytes:
    """Render the sample graph in a fresh process under ``seed``; return bytes.

    Fails the calling test (via the returned/raised state) rather than
    silently producing empty output — an empty file from a crashed child
    must never read as a clean byte-identical match.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _RENDER_PROG, str(out_path)],
        env=_child_env(seed),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"render subprocess (PYTHONHASHSEED={seed}) exited "
            f"{proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    data = out_path.read_bytes()
    return data


class TestCrossProcessRenderDeterminism(unittest.TestCase):
    def test_hashseed_env_actually_varies_hashing(self) -> None:
        """Precondition: the seeds we use really do change str hashing.

        Without this the main guard could pass vacuously (e.g. if a build
        of CPython ignored ``PYTHONHASHSEED``). We require the non-zero
        seeds to yield at least two distinct ``hash(str)`` values, proving
        set/dict iteration order genuinely varies between the renders.
        """
        hashes = set()
        for seed in (1, 7, 42):
            proc = subprocess.run(
                [sys.executable, "-c", "print(hash('limitless-beam-otp'))"],
                env=_child_env(seed),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                proc.returncode, 0, msg=proc.stderr
            )
            hashes.add(proc.stdout.strip())
        self.assertGreater(
            len(hashes),
            1,
            msg=(
                "PYTHONHASHSEED did not change str hashing across seeds "
                f"{(1, 7, 42)} (got {hashes!r}); the cross-process "
                "determinism guard would be vacuous on this interpreter."
            ),
        )

    def test_render_is_byte_identical_across_hash_seeds(self) -> None:
        """The SVG bytes are identical across PYTHONHASHSEED in {0,1,7,42}."""
        with tempfile.TemporaryDirectory(prefix="depmap_crossproc_") as td:
            tmp = Path(td)
            by_seed: dict[int, bytes] = {}
            for seed in SEEDS:
                data = _render_in_subprocess(seed, tmp / f"render_seed_{seed}.svg")
                # Honesty guard: a real, stamped, non-empty SVG — never
                # let a degenerate/empty payload count as a clean match.
                self.assertGreater(
                    len(data), 0, msg=f"empty SVG from seed {seed}"
                )
                self.assertIn(
                    b"lore@v1:", data, msg=f"unstamped SVG from seed {seed}"
                )
                self.assertIn(b"<svg", data, msg=f"non-SVG output from seed {seed}")
                by_seed[seed] = data

            reference = by_seed[SEEDS[0]]
            for seed in SEEDS[1:]:
                self.assertEqual(
                    by_seed[seed],
                    reference,
                    msg=(
                        "render output differs between PYTHONHASHSEED="
                        f"{SEEDS[0]} and PYTHONHASHSEED={seed}: the SVG is "
                        "NOT cross-process deterministic, so the Mirror-Mark "
                        "stamp is not reproducible. Look for unsorted "
                        "set/dict iteration in dep_map.render."
                    ),
                )


if __name__ == "__main__":
    unittest.main()
