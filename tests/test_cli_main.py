"""End-to-end ``cli.main()`` tests — the documented exit-code contract.

``test_render.py`` exercises the argparse surface (``build_parser`` +
``_layer_kinds``) but stops short of running ``main()``, so the actual
orchestration — scan -> graph -> filter -> render -> write, and the four
exit codes the README publishes as a stable contract — was untested.

This module fills that gap. Each test drives ``cli.main(argv)`` against a
real on-disk fake monorepo under ``tempfile.TemporaryDirectory`` and
asserts on the returned exit code and the side effect (SVG written / not
written). stdout/stderr are captured so the suite stays quiet.

Exit-code contract under test (README "Exit codes" table + ``cli`` module
docstring):

* 0 — render succeeded, SVG written to ``--out``.
* 1 — invalid arguments (``--root`` not a directory).
* 2 — empty graph after filters; no SVG written.
* 3 — IO error writing the SVG.

Stdlib-only (``unittest`` + ``contextlib`` + ``io`` + ``pathlib`` +
``tempfile``) per the R145 firewall. Additive: no production code is
touched.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dep_map import __version__, cli  # noqa: E402
from dep_map.render import MARK_PREFIX  # noqa: E402


def _mkroot() -> Path:
    """Create a tempdir with the standard layer skeleton."""
    root = Path(tempfile.mkdtemp(prefix="depmap_cli_test_"))
    for layer in ("flagships", "infrastructure", "engines", "foundation", "sdk"):
        (root / layer).mkdir()
    return root


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Invoke ``cli.main`` capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _seed_flagship(root: Path, name: str = "casino", producer: str = "reality") -> None:
    """Write a minimal go.mod that yields exactly one cohort edge."""
    _write(
        root / "flagships" / name / "go.mod",
        f"module github.com/davly/{name}\n"
        f"require github.com/davly/{producer} v0.0.0\n",
    )


class TestExitZeroSuccess(unittest.TestCase):
    def test_writes_svg_and_returns_zero(self) -> None:
        root = _mkroot()
        _seed_flagship(root)
        out = root / "dep_map.svg"
        code, stdout, _ = _run(["render", "--root", str(root), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertTrue(out.is_file())
        # The success log reports the node/edge counts.
        self.assertIn("wrote", stdout)
        self.assertIn("nodes=2", stdout)
        self.assertIn("edges=1", stdout)

    def test_written_svg_carries_mirror_mark_stamp(self) -> None:
        # The end-to-end path must produce a verifiable artefact, not just
        # a non-empty file.
        root = _mkroot()
        _seed_flagship(root)
        out = root / "dep_map.svg"
        code, _, _ = _run(["render", "--root", str(root), "--out", str(out)])
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn(MARK_PREFIX, text)
        self.assertIn("L43_MIRROR_MARK_V1", text)
        self.assertIn("R166_LIABILITY_FOOTER_CONST", text)

    def test_creates_missing_output_parent_dirs(self) -> None:
        # main() does parent.mkdir(parents=True); a nested --out must work.
        root = _mkroot()
        _seed_flagship(root)
        out = root / "nested" / "deeper" / "dep_map.svg"
        code, _, _ = _run(["render", "--root", str(root), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertTrue(out.is_file())

    def test_overwrites_existing_output(self) -> None:
        root = _mkroot()
        _seed_flagship(root)
        out = root / "dep_map.svg"
        out.write_text("stale contents", encoding="utf-8")
        code, _, _ = _run(["render", "--root", str(root), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertNotIn("stale contents", out.read_text(encoding="utf-8"))


class TestExitOneBadRoot(unittest.TestCase):
    def test_missing_root_dir_returns_one(self) -> None:
        root = _mkroot()
        missing = root / "does-not-exist"
        out = root / "dep_map.svg"
        code, _, stderr = _run(
            ["render", "--root", str(missing), "--out", str(out)]
        )
        self.assertEqual(code, 1)
        self.assertIn("is not a directory", stderr)
        self.assertFalse(out.exists())

    def test_root_pointing_at_a_file_returns_one(self) -> None:
        root = _mkroot()
        a_file = root / "not_a_dir"
        a_file.write_text("x", encoding="utf-8")
        out = root / "dep_map.svg"
        code, _, _ = _run(["render", "--root", str(a_file), "--out", str(out)])
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())


class TestExitTwoEmptyGraph(unittest.TestCase):
    def test_empty_layers_return_two_and_write_nothing(self) -> None:
        root = _mkroot()  # all layers exist but contain no manifests
        out = root / "dep_map.svg"
        code, _, stderr = _run(["render", "--root", str(root), "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("empty graph", stderr)
        self.assertFalse(out.exists())

    def test_firewall_filter_emptying_graph_returns_two(self) -> None:
        # An edge exists, but its producer is not a firewall hub, so the
        # --firewall-only filter empties the graph -> exit 2, no write.
        root = _mkroot()
        _write(
            root / "flagships" / "casino" / "go.mod",
            "module github.com/davly/casino\n"
            "require github.com/davly/some-non-hub-app v0.0.0\n",
        )
        out = root / "dep_map.svg"
        code, _, _ = _run(
            ["render", "--root", str(root), "--out", str(out), "--firewall-only"]
        )
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def test_layer_filter_emptying_graph_returns_two(self) -> None:
        # A flagship edge exists, but --layer foundation keeps only
        # foundation<->foundation edges, emptying the graph.
        root = _mkroot()
        _seed_flagship(root)
        out = root / "dep_map.svg"
        code, _, _ = _run(
            ["render", "--root", str(root), "--out", str(out), "--layer", "foundation"]
        )
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())


class TestExitThreeIOError(unittest.TestCase):
    def test_unwritable_output_path_returns_three(self) -> None:
        # Point --out through a regular file, so parent.mkdir() / write
        # cannot create the path. main() catches OSError -> exit 3.
        root = _mkroot()
        _seed_flagship(root)
        clash = root / "afile"
        clash.write_text("x", encoding="utf-8")
        out = clash / "sub" / "dep_map.svg"
        code, _, stderr = _run(["render", "--root", str(root), "--out", str(out)])
        self.assertEqual(code, 3)
        self.assertIn("failed to write", stderr)


class TestFilterModesProduceOutput(unittest.TestCase):
    """Happy-path coverage for the snapshot filter modes via main()."""

    def test_firewall_only_keeps_hub_edge_and_writes(self) -> None:
        root = _mkroot()
        _seed_flagship(root, name="casino", producer="reality")
        # reality is a firewall hub (via foundation/reality alias not used
        # here) — use limitless-rs to be unambiguous.
        _write(
            root / "flagships" / "foundry" / "Cargo.toml",
            '[package]\nname = "foundry"\n[dependencies]\nlimitless-rs = "0.1"\n',
        )
        out = root / "dep_map.svg"
        code, stdout, _ = _run(
            ["render", "--root", str(root), "--out", str(out), "--firewall-only"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(out.is_file())
        # limitless-rs is a firewall hub; its edge survives.
        self.assertIn("foundry", out.read_text(encoding="utf-8"))

    def test_layer_infra_keeps_infra_edges(self) -> None:
        # The infra view keeps INFRASTRUCTURE/ENGINE/HUB nodes only. An
        # infra->hub edge (nexus -> limitless-rs) survives the filter; a
        # foundation producer would be dropped (see _layer_kinds).
        root = _mkroot()
        _write(
            root / "infrastructure" / "nexus" / "Cargo.toml",
            '[package]\nname = "nexus"\n[dependencies]\nlimitless-rs = "0.1"\n',
        )
        out = root / "dep_map.svg"
        code, _, _ = _run(
            ["render", "--root", str(root), "--out", str(out), "--layer", "infra"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(out.is_file())
        self.assertIn("nexus", out.read_text(encoding="utf-8"))

    def test_custom_title_and_dimensions_flow_through(self) -> None:
        root = _mkroot()
        _seed_flagship(root)
        out = root / "dep_map.svg"
        code, _, _ = _run([
            "render", "--root", str(root), "--out", str(out),
            "--title", "Custom Map Heading 9000",
            "--width", "900", "--height", "700",
        ])
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("Custom Map Heading 9000", text)
        self.assertIn('width="900"', text)
        self.assertIn('height="700"', text)


class TestVersionFlag(unittest.TestCase):
    """dm-version: ``dep-map --version`` prints the version and exits 0.

    The version action must fire before the required sub-command is
    enforced, so ``--version`` works with no sub-command supplied.
    """

    def test_version_flag_prints_and_exits_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        printed = out.getvalue()
        self.assertIn("dep-map", printed)
        self.assertIn(__version__, printed)

    def test_version_string_matches_package_version(self) -> None:
        # The printed token is exactly "dep-map <__version__>".
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                cli.main(["--version"])
        self.assertEqual(out.getvalue().strip(), f"dep-map {__version__}")


if __name__ == "__main__":
    unittest.main()
