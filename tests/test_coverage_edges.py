"""Branch + error-path coverage completion across all four modules (dm-BU5).

Earlier waves pinned the happy paths and the headline degradation shapes.
This module closes the *remaining* untested branches — the honest-
degradation and defensive edges that a manifest walker hits in the wild:

* ``scanner`` — classify/consumer-name fallbacks for out-of-tree paths,
  the loud-once unknown-filename guard, dedup of an edge declared by two
  sub-manifests of one consumer, comment-line skipping, and every
  per-substrate type guard (a ``dependencies`` value that is not a table,
  a dict dep without a ``path``, a self-pointing path dep, an
  ``optional-dependencies`` group that is not a list, a dist string that
  does not match the PEP 508 name regex, the ``node_modules`` short
  circuit).
* ``graph`` — the already-seen back-edges in both transitive BFS walks,
  the unknown-node early return for ``transitive_producers``, the
  remaining-in-degree branch of the topological sort, and the non-dir
  child skip in ``classify_nodes``.
* ``render`` — the multi-hub angle branch, a ``foundation/``-prefixed
  node rendering at hub radius, ``extract_mark`` on a no-trailing-space
  comment, and the ``_wrap_text`` empty-input / oversized-first-word
  edges.
* ``cli`` — every ``_layer_kinds`` arm including the default fallback,
  the ``_query_result`` unreachable-kind guard, the ``main`` non-render/
  non-query defensive return, and the ``__main__`` entry guard.

A handful of branches are genuinely unreachable defence-in-depth and are
documented here rather than faked (the HONESTY gate forbids contriving a
test that does not exercise the real path):

* ``scanner`` non-``str`` mapping keys (Cargo ``deps.items`` /
  package.json ``for name in deps``) — ``tomllib`` and ``json.load``
  only ever produce ``str`` keys, so the ``isinstance(name, str)`` guard
  can never be False from a real manifest.
* ``graph.topological_order`` ``consumer in indeg`` False — ``indeg`` is
  built from ``all_nodes()`` and ``incoming`` only references edge
  endpoints, so every consumer is always present.
* ``render`` edge with a missing endpoint position — positions cover
  ``all_nodes()`` and edges only reference those nodes.

Stdlib-only (``unittest`` + ``contextlib`` + ``io`` + ``runpy`` +
``tempfile`` + ``unittest.mock``) per the R145 firewall. Additive: no
production code is touched.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dep_map import cli  # noqa: E402
from dep_map.graph import Graph  # noqa: E402
from dep_map.render import _wrap_text, extract_mark, render_svg  # noqa: E402
from dep_map.scanner import Edge, NodeKind, Scanner  # noqa: E402


def _mkroot() -> Path:
    root = Path(tempfile.mkdtemp(prefix="depmap_cov_test_"))
    for layer in (
        "flagships",
        "infrastructure",
        "engines",
        "foundation",
        "sdk",
        "apps",
        "tools",
    ):
        (root / layer).mkdir()
    return root


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# scanner — classify / consumer-name fallbacks.
# ---------------------------------------------------------------------------

class TestScannerClassifyEdges(unittest.TestCase):
    def test_classify_path_outside_root_is_unknown(self) -> None:
        # relative_to(root) raises ValueError for a path in another tree;
        # classify must degrade to UNKNOWN rather than propagate.
        root = _mkroot()
        foreign = Path(tempfile.mkdtemp(prefix="depmap_cov_foreign_"))
        scanner = Scanner(root=root)
        self.assertEqual(
            scanner.classify(foreign / "flagships" / "x" / "go.mod"),
            NodeKind.UNKNOWN,
        )

    def test_classify_root_itself_is_unknown(self) -> None:
        # path == root => empty relative parts => UNKNOWN.
        root = _mkroot()
        self.assertEqual(Scanner(root=root).classify(root), NodeKind.UNKNOWN)

    def test_classify_apps_non_hub_tool_is_unknown(self) -> None:
        # An apps/ entry that is not a published cohort hub stays UNKNOWN.
        root = _mkroot()
        self.assertEqual(
            Scanner(root=root).classify(root / "apps" / "randomtool" / "go.mod"),
            NodeKind.UNKNOWN,
        )

    def test_classify_tools_non_hub_is_unknown(self) -> None:
        root = _mkroot()
        self.assertEqual(
            Scanner(root=root).classify(root / "tools" / "scratch" / "go.mod"),
            NodeKind.UNKNOWN,
        )


class TestConsumerNameFallbacks(unittest.TestCase):
    def test_consumer_name_outside_layer_uses_parent_name(self) -> None:
        # A manifest path not under root/<layer> falls back to the parent
        # directory name instead of raising.
        root = _mkroot()
        foreign = Path(tempfile.mkdtemp(prefix="depmap_cov_cn_"))
        scanner = Scanner(root=root)
        self.assertEqual(
            scanner._consumer_name(foreign / "weird" / "go.mod", "flagships"),
            "weird",
        )

    def test_consumer_name_at_layer_root_uses_parent_name(self) -> None:
        # manifest == root/<layer> => empty relative parts => parent name.
        root = _mkroot()
        scanner = Scanner(root=root)
        layer_dir = root / "flagships"
        self.assertEqual(
            scanner._consumer_name(layer_dir, "flagships"),
            layer_dir.parent.name,
        )


# ---------------------------------------------------------------------------
# scanner — walk / dispatch / dedup.
# ---------------------------------------------------------------------------

class TestScannerWalkAndDispatch(unittest.TestCase):
    def test_unknown_manifest_filename_warns_and_skips(self) -> None:
        # Defence-in-depth: _parse_manifest dispatched on a name outside
        # MANIFEST_FILES fires one loud-once warning and yields nothing.
        root = _mkroot()
        sink = io.StringIO()
        scanner = Scanner(root=root, _stderr=sink)
        edges = list(scanner._parse_manifest(root / "x" / "unknown.txt", "x"))
        self.assertEqual(edges, [])
        warn = sink.getvalue()
        self.assertIn("[LOUD-ONCE-WARNING]", warn)
        self.assertIn("unknown manifest filename", warn)

    def test_duplicate_edge_across_submanifests_is_deduped(self) -> None:
        # Two go.mod files under one flagship (top + nested backend) that
        # declare the same require collapse to one edge (same consumer
        # name => same triple => seen-set drops the second).
        root = _mkroot()
        body = (
            "module github.com/davly/foo\n"
            "require github.com/davly/reality v0.0.0\n"
        )
        _write(root / "flagships" / "foo" / "go.mod", body)
        _write(root / "flagships" / "foo" / "backend" / "go.mod", body)
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges, [Edge(consumer="foo", producer="reality", kind="go")]
        )

    def test_walk_iterates_past_non_manifest_files(self) -> None:
        # A manifest dir that also holds non-manifest files (sorted both
        # before and after the manifest) still yields the edge and keeps
        # walking past the manifest.
        root = _mkroot()
        d = root / "flagships" / "bar"
        _write(d / "aaa.txt", "before the manifest\n")
        _write(
            d / "go.mod",
            "module github.com/davly/bar\n"
            "require github.com/davly/reality v0.0.0\n",
        )
        _write(d / "zzz.txt", "after the manifest\n")
        _write(d / "README.md", "readme\n")
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges, [Edge(consumer="bar", producer="reality", kind="go")]
        )


# ---------------------------------------------------------------------------
# scanner — per-substrate type guards (honest degradation, no crash).
# ---------------------------------------------------------------------------

class TestGoCommentSkipping(unittest.TestCase):
    def test_full_line_comment_is_skipped(self) -> None:
        root = _mkroot()
        _write(
            root / "flagships" / "cmt" / "go.mod",
            "module github.com/davly/cmt\n"
            "// a leading comment line the parser must skip\n"
            "require github.com/davly/reality v0.0.0\n",
        )
        edges = Scanner(root=root).scan_sorted()
        self.assertEqual(
            edges, [Edge(consumer="cmt", producer="reality", kind="go")]
        )


class TestCargoTypeGuards(unittest.TestCase):
    def test_dependencies_not_a_table_is_skipped(self) -> None:
        # `dependencies` declared as a top-level scalar (not a table) =>
        # the isinstance(deps, dict) guard skips it, no crash. It must be a
        # top-level key (before any [section]) so data.get("dependencies")
        # actually returns the scalar.
        root = _mkroot()
        _write(
            root / "flagships" / "x" / "Cargo.toml",
            'dependencies = "oops not a table"\n[package]\nname = "x"\n',
        )
        self.assertEqual(Scanner(root=root).scan_sorted(), [])

    def test_dict_dep_without_path_is_skipped(self) -> None:
        # A table dep with no `path` key contributes no edge; a sibling
        # hub dep still does.
        root = _mkroot()
        _write(
            root / "flagships" / "x" / "Cargo.toml",
            '[package]\nname = "x"\n[dependencies]\n'
            'serde = { version = "1.0" }\n'
            'limitless-rs = "0.1"\n',
        )
        self.assertEqual(
            Scanner(root=root).scan_sorted(),
            [Edge(consumer="x", producer="limitless-rs", kind="rust")],
        )

    def test_self_pointing_path_dep_is_skipped(self) -> None:
        # A path dep whose last segment equals the consumer name is a
        # self-reference and is dropped (producer == consumer guard).
        root = _mkroot()
        _write(
            root / "flagships" / "selfref" / "Cargo.toml",
            '[package]\nname = "selfref"\n[dependencies]\n'
            'local = { path = "../selfref" }\n',
        )
        self.assertEqual(Scanner(root=root).scan_sorted(), [])


class TestPyprojectTypeGuards(unittest.TestCase):
    def test_dependencies_not_a_list_is_skipped(self) -> None:
        root = _mkroot()
        _write(
            root / "flagships" / "x" / "pyproject.toml",
            '[project]\nname = "x"\ndependencies = "oops not a list"\n',
        )
        self.assertEqual(Scanner(root=root).scan_sorted(), [])

    def test_optional_dependencies_not_a_dict_is_skipped(self) -> None:
        root = _mkroot()
        _write(
            root / "flagships" / "x" / "pyproject.toml",
            '[project]\nname = "x"\ndependencies = []\n'
            'optional-dependencies = "oops not a table"\n',
        )
        self.assertEqual(Scanner(root=root).scan_sorted(), [])

    def test_optional_group_not_a_list_is_skipped(self) -> None:
        # A group inside optional-dependencies that is not a list is
        # skipped; a good group's hub dep still surfaces.
        root = _mkroot()
        _write(
            root / "flagships" / "x" / "pyproject.toml",
            '[project]\nname = "x"\ndependencies = []\n'
            "[project.optional-dependencies]\n"
            'bad = "not a list"\n'
            'good = ["limitless-py>=0.1"]\n',
        )
        self.assertEqual(
            Scanner(root=root).scan_sorted(),
            [Edge(consumer="x", producer="limitless-py", kind="python")],
        )

    def test_dist_string_not_matching_name_regex_is_skipped(self) -> None:
        # A dependency string that does not start with a PEP 508 name
        # char yields no regex match and is skipped; the good dep stays.
        root = _mkroot()
        _write(
            root / "flagships" / "x" / "pyproject.toml",
            '[project]\nname = "x"\n'
            'dependencies = ["!!!notaname", "limitless-py>=0.1"]\n',
        )
        self.assertEqual(
            Scanner(root=root).scan_sorted(),
            [Edge(consumer="x", producer="limitless-py", kind="python")],
        )


class TestPackageJsonTypeGuards(unittest.TestCase):
    def test_node_modules_path_short_circuits(self) -> None:
        # The defence-in-depth guard returns [] before opening the file
        # (SKIP_DIRS normally prunes node_modules during the walk).
        root = _mkroot()
        scanner = Scanner(root=root)
        edges = scanner._parse_package_json(
            root / "flagships" / "x" / "node_modules" / "pkg" / "package.json",
            "x",
        )
        self.assertEqual(edges, [])

    def test_dependencies_not_an_object_is_skipped(self) -> None:
        root = _mkroot()
        _write(
            root / "flagships" / "x" / "package.json",
            '{"name": "x", "dependencies": "oops not an object"}',
        )
        self.assertEqual(Scanner(root=root).scan_sorted(), [])


# ---------------------------------------------------------------------------
# graph — BFS back-edges, unknown-node returns, topo remaining-in-degree.
# ---------------------------------------------------------------------------

def _e(c: str, p: str, k: str = "go") -> Edge:
    return Edge(consumer=c, producer=p, kind=k)


class TestGraphTraversalBranches(unittest.TestCase):
    def test_transitive_consumers_visits_shared_node_once(self) -> None:
        # Diamond on the reverse edges: `top` reaches `hub` via both `a`
        # and `b`, exercising the already-seen back-edge.
        g = Graph.from_edges([
            _e("a", "hub"),
            _e("b", "hub"),
            _e("top", "a"),
            _e("top", "b"),
        ])
        self.assertEqual(
            g.transitive_consumers("hub"), {"a", "b", "top"}
        )

    def test_transitive_producers_unknown_node_is_empty(self) -> None:
        g = Graph.from_edges([_e("a", "b")])
        self.assertEqual(g.transitive_producers("ghost"), set())

    def test_transitive_producers_visits_shared_node_once(self) -> None:
        # Forward diamond: `top` reaches `base` via both `a` and `b`.
        g = Graph.from_edges([
            _e("top", "a"),
            _e("top", "b"),
            _e("a", "base"),
            _e("b", "base"),
        ])
        self.assertEqual(
            g.transitive_producers("top"), {"a", "b", "base"}
        )

    def test_topo_handles_node_with_multiple_producers(self) -> None:
        # `c` depends on both `a` and `b`; the first decrement leaves a
        # non-zero in-degree (the remaining-in-degree branch) and only the
        # second drops it to zero.
        g = Graph.from_edges([_e("c", "a"), _e("c", "b")])
        order = g.topological_order()
        self.assertEqual(order[:2], ["a", "b"])
        self.assertEqual(order[-1], "c")
        self.assertEqual(set(order), {"a", "b", "c"})


class TestClassifyNodesSkipsNonDirs(unittest.TestCase):
    def test_file_directly_under_layer_is_skipped(self) -> None:
        # A stray file sitting directly under a layer dir must not break
        # classify_nodes' iterdir loop.
        root = _mkroot()
        _write(
            root / "flagships" / "casino" / "go.mod",
            "module github.com/davly/casino\n"
            "require github.com/davly/reality v0.0.0\n",
        )
        _write(root / "flagships" / "stray-file.txt", "not a directory\n")
        scanner = Scanner(root=root)
        graph = Graph.from_scanner(scanner)
        self.assertEqual(graph.node_kinds.get("casino"), NodeKind.FLAGSHIP)


# ---------------------------------------------------------------------------
# render — layout + mark + wrap edges.
# ---------------------------------------------------------------------------

class TestRenderLayoutBranches(unittest.TestCase):
    def test_multi_hub_layout_renders(self) -> None:
        # Two hubs => the multi-hub angle branch (n_hubs > 1) is taken.
        g = Graph.from_edges([
            _e("a", "limitless-rs", "rust"),
            _e("a", "limitless-py", "python"),
        ])
        svg = render_svg(g)
        self.assertIn("limitless-rs", svg)
        self.assertIn("limitless-py", svg)

    def test_foundation_prefixed_node_renders_at_hub_radius(self) -> None:
        # A `foundation/`-prefixed producer takes the r = 18.0 branch.
        g = Graph.from_edges([_e("casino", "foundation/pkg")])
        g.node_kinds = {
            "casino": NodeKind.FLAGSHIP,
            "foundation/pkg": NodeKind.HUB,
        }
        svg = render_svg(g)
        self.assertIn("foundation/pkg", svg)
        self.assertIn('r="18.0"', svg)


class TestExtractMarkAndWrap(unittest.TestCase):
    def test_extract_mark_with_no_trailing_space(self) -> None:
        # A comment where the mark is immediately followed by `-->` (no
        # space) takes the find('-->') fallback.
        svg = "<svg>\n<!-- lore@v1:ABCDEF-->\n</svg>"
        self.assertEqual(extract_mark(svg), "lore@v1:ABCDEF")

    def test_wrap_text_empty_input(self) -> None:
        self.assertEqual(_wrap_text(""), [])

    def test_wrap_text_oversized_first_word(self) -> None:
        # The first word alone exceeds max_chars while `cur` is empty,
        # exercising the `if cur` False branch in the over-width path.
        self.assertEqual(_wrap_text("superlongword", max_chars=4), ["superlongword"])


# ---------------------------------------------------------------------------
# cli — layer-kind arms, query-result guard, main fallbacks, entry guard.
# ---------------------------------------------------------------------------

class TestLayerKindsArms(unittest.TestCase):
    def test_flagship(self) -> None:
        self.assertEqual(
            cli._layer_kinds("flagship"), [NodeKind.FLAGSHIP, NodeKind.HUB]
        )

    def test_engine(self) -> None:
        self.assertEqual(
            cli._layer_kinds("engine"), [NodeKind.ENGINE, NodeKind.HUB]
        )

    def test_sdk(self) -> None:
        self.assertEqual(cli._layer_kinds("sdk"), [NodeKind.SDK, NodeKind.HUB])

    def test_unknown_layer_returns_all_kinds(self) -> None:
        # The default fallback returns every NodeKind (an effective no-op
        # filter). argparse `choices` gate this in normal CLI use; the
        # direct call covers the defensive default.
        self.assertEqual(cli._layer_kinds("bogus"), list(NodeKind))


class TestQueryResultGuard(unittest.TestCase):
    def test_unknown_kind_raises_value_error(self) -> None:
        # argparse choices gate `kind`; the guard is defence-in-depth and
        # is reachable only by a direct call with an off-list kind.
        with self.assertRaises(ValueError):
            cli._query_result(Graph(), "not-a-real-kind", None)


class TestMainDefensiveAndEntry(unittest.TestCase):
    def test_main_non_render_non_query_returns_one(self) -> None:
        # The defensive `cmd != render` arm is unreachable through
        # argparse (subparsers are required + choice-gated), so we inject a
        # namespace with a bogus cmd via a stubbed parser.
        ns = argparse.Namespace(cmd="bogus")
        with mock.patch.object(cli, "build_parser") as fake_build:
            fake_build.return_value.parse_args.return_value = ns
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main([])
        self.assertEqual(code, 1)

    def test_module_entry_point_runs_main(self) -> None:
        # Execute cli.py as __main__ so the `if __name__ == "__main__"`
        # guard (sys.exit(main())) is exercised. A valid query invocation
        # makes main() return 0 => SystemExit(0).
        root = _mkroot()
        _write(
            root / "flagships" / "casino" / "go.mod",
            "module github.com/davly/casino\n"
            "require github.com/davly/reality v0.0.0\n",
        )
        cli_path = Path(cli.__file__)
        argv = ["dep-map", "query", "--root", str(root), "has-cycle"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    runpy.run_path(str(cli_path), run_name="__main__")
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
