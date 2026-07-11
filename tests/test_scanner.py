"""Per-substrate scanner unit tests.

We build small fake monorepo trees under ``tempfile.TemporaryDirectory``
and assert that :meth:`Scanner.scan` emits exactly the expected edges.

The tests are stdlib-only (``unittest`` + ``pathlib`` + ``tempfile``)
and run in <1 s on a cold disk.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add the repo root to sys.path so ``from dep_map ...`` resolves when
# tests are invoked directly (``python -m unittest discover``).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dep_map.scanner import (  # noqa: E402
    Edge,
    HUB_NAMES,
    LoudOnce,
    NodeKind,
    Scanner,
)


def _mkroot() -> Path:
    """Create a tempdir with the standard five-layer monorepo skeleton."""
    root = Path(tempfile.mkdtemp(prefix="depmap_test_"))
    for layer in ("flagships", "infrastructure", "engines", "foundation", "sdk", "apps", "tools"):
        (root / layer).mkdir()
    return root


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestGoMod(unittest.TestCase):
    def test_block_require_with_davly_dep(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "casino" / "go.mod", """
module github.com/davly/casino
go 1.24
require (
    github.com/davly/reality v0.0.0
    github.com/stretchr/testify v1.11.1
)
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges,
            [Edge(consumer="casino", producer="reality", kind="go")],
        )

    def test_single_line_require(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "anvil" / "go.mod", """
module github.com/davly/anvil
go 1.22
require github.com/davly/foundation v1.0.0
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertIn(Edge(consumer="anvil", producer="foundation", kind="go"), edges)

    def test_foundation_pkg_subpath(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "drift" / "go.mod", """
module github.com/davly/drift
go 1.22
require foundation/pkg/mirrormark v0.0.0
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertIn(
            Edge(consumer="drift", producer="foundation/pkg", kind="go"),
            edges,
        )

    def test_skips_self_reference(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "casino" / "go.mod", """
module github.com/davly/casino
require github.com/davly/casino v0.0.0
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(edges, [])

    def test_drops_indirect_block_require(self) -> None:
        # B7/dm-BU4: a ``// indirect`` require is a transitive pin, not a
        # direct dependency. The declared-edges map drops it (the _go_edge
        # docstring already promised this; the parser now honours it).
        root = _mkroot()
        _write(root / "flagships" / "casino" / "go.mod", """
module github.com/davly/casino
go 1.24
require (
    github.com/davly/reality v0.0.0
    github.com/davly/ghostlib v0.0.0 // indirect
)
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges,
            [Edge(consumer="casino", producer="reality", kind="go")],
        )

    def test_drops_indirect_single_line_require(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "anvil" / "go.mod", """
module github.com/davly/anvil
go 1.22
require github.com/davly/foundation v1.0.0
require github.com/davly/transitive v0.0.0 // indirect
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges,
            [Edge(consumer="anvil", producer="foundation", kind="go")],
        )

    def test_indirect_marker_variants_are_dropped(self) -> None:
        # ``go mod tidy`` writes "// indirect"; tolerate spacing and a
        # trailing reason ("// indirect; needed for build").
        root = _mkroot()
        _write(root / "flagships" / "vault" / "go.mod", """
module github.com/davly/vault
go 1.24
require (
    github.com/davly/keep v0.0.0
    github.com/davly/a v0.0.0 //indirect
    github.com/davly/b v0.0.0 //   indirect
    github.com/davly/c v0.0.0 // indirect; pinned for reproducible build
)
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges,
            [Edge(consumer="vault", producer="keep", kind="go")],
        )

    def test_skips_third_party(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "casino" / "go.mod", """
module github.com/davly/casino
require (
    github.com/stretchr/testify v1.11.1
    golang.org/x/crypto v0.50.0
)
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(edges, [])


class TestCargoToml(unittest.TestCase):
    def test_limitless_dep(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "foundry" / "Cargo.toml", """
[package]
name = "foundry"
version = "0.1.0"
edition = "2021"

[dependencies]
limitless-rs = "0.1"
serde = "1.0"
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges,
            [Edge(consumer="foundry", producer="limitless-rs", kind="rust")],
        )

    def test_path_dep_inside_monorepo(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "cipher-next" / "Cargo.toml", """
[package]
name = "cipher-next"

[dependencies]
reality-rs = { path = "../../foundation/reality-rs" }
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertIn(
            Edge(consumer="cipher-next", producer="reality-rs", kind="rust"),
            edges,
        )

    def test_skips_non_limitless(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "needle" / "Cargo.toml", """
[package]
name = "needle"

[dependencies]
tokio = "1.0"
serde = "1.0"
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(edges, [])


class TestPyproject(unittest.TestCase):
    def test_pep621_dependency(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "dreamcatcher" / "pyproject.toml", """
[build-system]
requires = ["setuptools>=68.0"]

[project]
name = "dreamcatcher"
version = "0.1.0"
dependencies = [
    "limitless-py>=0.1",
    "pytest>=8.0",
]
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges,
            [Edge(consumer="dreamcatcher", producer="limitless-py", kind="python")],
        )

    def test_optional_dependencies(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "iris" / "pyproject.toml", """
[project]
name = "iris"
dependencies = []

[project.optional-dependencies]
nexus = ["limitless-py>=0.1"]
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertIn(
            Edge(consumer="iris", producer="limitless-py", kind="python"),
            edges,
        )


class TestPackageJson(unittest.TestCase):
    def test_at_limitless_scope(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "atlas-ui" / "package.json", """
{
  "name": "atlas-ui",
  "dependencies": {
    "@limitless/sdk-ts": "^0.1.0",
    "lodash": "^4.17.21"
  }
}
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges,
            [Edge(consumer="atlas-ui", producer="sdk-ts", kind="ts")],
        )

    def test_limitless_prefix(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "conjure" / "package.json", """
{
  "name": "conjure",
  "dependencies": {
    "limitless-ts": "^0.1.0"
  }
}
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertIn(
            Edge(consumer="conjure", producer="limitless-ts", kind="ts"),
            edges,
        )

    def test_skips_node_modules_path(self) -> None:
        root = _mkroot()
        nm = root / "flagships" / "foo" / "node_modules" / "@babel" / "core"
        _write(nm / "package.json", """{"name":"@babel/core","dependencies":{"@limitless/x":"1"}}""")
        edges = Scanner(root=root).scan_sorted()
        # node_modules is in SKIP_DIRS so we should not see the edge.
        self.assertEqual(edges, [])


class TestBeam(unittest.TestCase):
    def test_rebar_dep(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "sentinel-prime" / "rebar.config", """
{erl_opts, [debug_info]}.
{deps, [
    {limitless_beam_otp, "0.1"},
    {cowboy, "2.12.0"}
]}.
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertIn(
            Edge(consumer="sentinel-prime", producer="limitless-beam-otp", kind="beam"),
            edges,
        )

    def test_mix_dep(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "dispatch" / "mix.exs", """
defmodule Dispatch.MixProject do
  defp deps do
    [
      {:limitless_beam_otp, "~> 0.1"},
      {:jason, "~> 1.4"}
    ]
  end
end
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertIn(
            Edge(consumer="dispatch", producer="limitless-beam-otp", kind="beam"),
            edges,
        )


class TestClassification(unittest.TestCase):
    def test_classify_flagship(self) -> None:
        root = _mkroot()
        scanner = Scanner(root=root)
        self.assertEqual(
            scanner.classify(root / "flagships" / "casino" / "go.mod"),
            NodeKind.FLAGSHIP,
        )

    def test_classify_infrastructure(self) -> None:
        root = _mkroot()
        scanner = Scanner(root=root)
        self.assertEqual(
            scanner.classify(root / "infrastructure" / "nexus" / "go.mod"),
            NodeKind.INFRASTRUCTURE,
        )

    def test_classify_engine(self) -> None:
        root = _mkroot()
        scanner = Scanner(root=root)
        self.assertEqual(
            scanner.classify(root / "engines" / "oracle" / "go.mod"),
            NodeKind.ENGINE,
        )

    def test_classify_foundation(self) -> None:
        root = _mkroot()
        scanner = Scanner(root=root)
        self.assertEqual(
            scanner.classify(root / "foundation" / "reality" / "go.mod"),
            NodeKind.FOUNDATION,
        )

    def test_classify_sdk(self) -> None:
        root = _mkroot()
        scanner = Scanner(root=root)
        self.assertEqual(
            scanner.classify(root / "sdk" / "limitless-py" / "pyproject.toml"),
            NodeKind.SDK,
        )

    def test_classify_unknown_layer(self) -> None:
        root = _mkroot()
        scanner = Scanner(root=root)
        self.assertEqual(
            scanner.classify(root / "weird" / "foo" / "go.mod"),
            NodeKind.UNKNOWN,
        )

    def test_classify_hub_in_apps(self) -> None:
        root = _mkroot()
        scanner = Scanner(root=root)
        self.assertEqual(
            scanner.classify(root / "apps" / "limitless-beam-otp" / "rebar.config"),
            NodeKind.HUB,
        )


class TestLoudOnce(unittest.TestCase):
    """R143 LOUD-ONCE-WARNING shape test — wire byte-shape matches Go canonical."""

    def test_fires_once(self) -> None:
        sink = io.StringIO()
        guard = LoudOnce()
        self.assertTrue(guard.fire(sink, "first"))
        self.assertFalse(guard.fire(sink, "second"))
        out = sink.getvalue()
        # One emission only.
        self.assertEqual(out.count("[LOUD-ONCE-WARNING]"), 1)
        self.assertIn("audit_rule=R143_LOUD_ONCE_WARNING_FLAG", out)
        self.assertIn('message="first"', out)

    def test_reset_is_test_only(self) -> None:
        sink = io.StringIO()
        guard = LoudOnce()
        guard.fire(sink, "x")
        guard.reset()
        self.assertTrue(guard.fire(sink, "y"))


class TestSkipDirs(unittest.TestCase):
    def test_skips_target_dir(self) -> None:
        root = _mkroot()
        _write(root / "flagships" / "foo" / "target" / "Cargo.toml", """
[package]
name="x"
[dependencies]
limitless-rs = "0.1"
""")
        # No top-level Cargo.toml — only the nested one inside target/.
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(edges, [])


class TestDeterminism(unittest.TestCase):
    def test_scan_sorted_is_stable(self) -> None:
        root = _mkroot()
        for name in ("alpha", "beta", "gamma"):
            _write(root / "flagships" / name / "go.mod", f"""
module github.com/davly/{name}
require github.com/davly/reality v0.0.0
""")
        first = Scanner(root=root).scan_sorted()
        second = Scanner(root=root).scan_sorted()
        self.assertEqual(first, second)
        self.assertEqual(
            [e.consumer for e in first],
            ["alpha", "beta", "gamma"],
        )


class TestHubNames(unittest.TestCase):
    def test_known_hubs_are_present(self) -> None:
        for h in ("limitless-py", "limitless-rs", "limitless-ts", "limitless-beam-otp"):
            self.assertIn(h, HUB_NAMES)

    def test_foundation_hubs_use_parser_emitted_spellings(self) -> None:
        # Regression (2026-07-11): the foundation hubs were spelled
        # path-style ("foundation/reality") — a form NO parser branch
        # can emit (_go_edge produces the repo tail of
        # github.com/davly/<repo>) — so the estate's #1 hub (reality,
        # 108 consumers) was silently absent from hub-degree and the
        # firewall snapshot. The entries must use the emitted spellings.
        for h in ("reality", "aicore", "knowledge", "foundation"):
            self.assertIn(h, HUB_NAMES, msg=f"emitted hub spelling {h!r} missing")
        for dead in (
            "foundation/reality",
            "foundation/aicore",
            "foundation/knowledge",
        ):
            self.assertNotIn(
                dead, HUB_NAMES, msg=f"dead path-style entry {dead!r} present"
            )

    def test_every_hub_name_is_parser_emittable(self) -> None:
        # Spelling contract: no parser branch emits a producer containing
        # "/" except the literal "foundation/pkg" (_go_edge's bare
        # in-tree branch). Go emits the tail before the first slash;
        # Rust crate names, Python dist names (_PY_DIST_RE) and npm names
        # (post @limitless/ strip) cannot contain "/". Any other slashed
        # entry is dead: it can never intersect the scanned graph.
        for h in HUB_NAMES:
            if h == "foundation/pkg":
                continue
            self.assertNotIn(
                "/",
                h,
                msg=(
                    f"HUB_NAMES entry {h!r} contains '/' and can never be "
                    "emitted by any parser branch (dead entry)"
                ),
            )

    def test_go_edge_to_foundation_hub_lands_in_hub_names(self) -> None:
        # End-to-end: a go.mod requiring github.com/davly/reality emits a
        # producer that IS a hub. Without the respelling the emitted
        # producer ("reality") missed HUB_NAMES and every hub-membership
        # consumer (hub-degree, firewall, render sizing) dropped it.
        root = _mkroot()
        _write(root / "flagships" / "casino" / "go.mod", """
module github.com/davly/casino
require github.com/davly/reality v0.0.0
""")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(len(edges), 1)
        self.assertIn(edges[0].producer, HUB_NAMES)


if __name__ == "__main__":
    unittest.main()
