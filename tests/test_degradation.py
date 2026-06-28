"""R143 malformed-manifest degradation — end-to-end, every substrate (dm-BU2).

R143 promises that a manifest the scanner cannot parse degrades honestly:
it is skipped (a single LOUD-ONCE warning fires for parser-detectable
corruption) and the walk continues. One bad manifest must NEVER crash the
whole scan or drop the edges of every other (good) manifest.

That property was previously asserted only at the unit level
(``LoudOnce`` fires once) and only for the two substrates whose parser
raises a decode error. It was never verified end-to-end across every
substrate — and two real-world shapes silently broke it: a
``package.json`` whose top-level value is a JSON array, and a
``pyproject.toml`` whose ``[project]`` is not a table, both crashed the
entire scan with an uncaught ``AttributeError``, dropping every other
edge. These tests build a corrupt manifest of each kind alongside a
known-good sibling and assert:

* the scan does not raise (no crash);
* the good sibling's edge survives (the rest is not dropped);
* the corrupt manifest contributes no spurious edges;
* where corruption is parser-detectable (TOML / JSON / wrong-type
  section) the R143 LOUD-ONCE warning fires and names the bad file.

Line-based substrates (``go.mod`` / ``rebar.config`` / ``mix.exs``) read
with ``errors="replace"`` and match by regex, so arbitrary bytes can
never raise — they degrade *silently* (no warning, no edges). The tests
assert that honest behaviour rather than pretending a warning fires.

Stdlib-only (``unittest`` + ``io`` + ``tempfile`` + ``pathlib``) per the
R145 firewall.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dep_map.scanner import Edge, LoudOnce, Scanner  # noqa: E402

# The single good sibling every degradation test plants. Its edge must
# survive any corrupt manifest elsewhere in the tree.
GOOD_EDGE = Edge(consumer="good", producer="reality", kind="go")
_GOOD_GOMOD = "module github.com/davly/good\nrequire github.com/davly/reality v0.0.0\n"


def _mkroot() -> Path:
    root = Path(tempfile.mkdtemp(prefix="depmap_degrade_test_"))
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


def _write(path: Path, body: "str | bytes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")


def _scan_with(corrupt_rel: str, corrupt_body: "str | bytes") -> "tuple[list[Edge], str]":
    """Plant the good sibling + one corrupt manifest; return (edges, stderr).

    The scan is run with a captured stderr so R143 warnings are
    observable. Any exception escaping ``scan_sorted`` is a degradation
    failure and propagates to the test (which then fails loudly).
    """
    root = _mkroot()
    _write(root / "flagships" / "good" / "go.mod", _GOOD_GOMOD)
    _write(root / corrupt_rel, corrupt_body)
    sink = io.StringIO()
    edges = Scanner(root=root, _stderr=sink).scan_sorted()
    return edges, sink.getvalue()


class TestSilentlyDegradingSubstrates(unittest.TestCase):
    """Line-based parsers: arbitrary bytes degrade to nothing, no crash."""

    def test_go_mod_garbage_degrades_silently(self) -> None:
        edges, warn = _scan_with(
            "flagships/bad/go.mod", b"\x00\x01\x02 not a valid <<< go.mod >>>"
        )
        self.assertEqual(edges, [GOOD_EDGE])
        self.assertNotIn("[LOUD-ONCE-WARNING]", warn)

    def test_rebar_config_garbage_degrades_silently(self) -> None:
        edges, warn = _scan_with(
            "flagships/bad/rebar.config", "!!! this is not ((( erlang config"
        )
        self.assertEqual(edges, [GOOD_EDGE])
        self.assertNotIn("[LOUD-ONCE-WARNING]", warn)

    def test_mix_exs_garbage_degrades_silently(self) -> None:
        edges, warn = _scan_with(
            "flagships/bad/mix.exs", "%%% not <<< elixir ((( source"
        )
        self.assertEqual(edges, [GOOD_EDGE])
        self.assertNotIn("[LOUD-ONCE-WARNING]", warn)


class TestParseErrorSubstrates(unittest.TestCase):
    """Decode-error parsers: corruption fires one R143 warning + skips."""

    def _assert_warned_about(self, warn: str, needle: str) -> None:
        self.assertIn("[LOUD-ONCE-WARNING]", warn)
        self.assertIn("R143_LOUD_ONCE_WARNING_FLAG", warn)
        self.assertIn(needle, warn)

    def test_cargo_toml_broken_toml_warns_and_skips(self) -> None:
        edges, warn = _scan_with(
            "flagships/bad/Cargo.toml", "this is = = not [[[ valid toml"
        )
        self.assertEqual(edges, [GOOD_EDGE])
        self._assert_warned_about(warn, "Cargo.toml")

    def test_pyproject_broken_toml_warns_and_skips(self) -> None:
        edges, warn = _scan_with(
            "flagships/bad/pyproject.toml", "not = = valid ]]] toml"
        )
        self.assertEqual(edges, [GOOD_EDGE])
        self._assert_warned_about(warn, "pyproject.toml")

    def test_package_json_broken_json_warns_and_skips(self) -> None:
        edges, warn = _scan_with(
            "flagships/bad/package.json", "{not valid json,,,"
        )
        self.assertEqual(edges, [GOOD_EDGE])
        self._assert_warned_about(warn, "package.json")


class TestWrongTypeManifestsDegrade(unittest.TestCase):
    """The two shapes that used to crash the whole scan with AttributeError.

    Both are syntactically valid (parse fine) but structurally wrong: the
    per-parser type guards missed them and the bare ``.get`` blew up,
    propagating an uncaught ``AttributeError`` out of the walk and
    dropping every other edge. They must now degrade like any other
    unparseable manifest: warn + skip + keep the rest.
    """

    def _assert_warned_about(self, warn: str, needle: str) -> None:
        self.assertIn("[LOUD-ONCE-WARNING]", warn)
        self.assertIn(needle, warn)

    def test_package_json_toplevel_array_does_not_crash(self) -> None:
        edges, warn = _scan_with("flagships/bad/package.json", "[1, 2, 3]")
        self.assertEqual(edges, [GOOD_EDGE])
        self._assert_warned_about(warn, "package.json")

    def test_package_json_toplevel_scalar_does_not_crash(self) -> None:
        edges, warn = _scan_with("flagships/bad/package.json", "42")
        self.assertEqual(edges, [GOOD_EDGE])
        self._assert_warned_about(warn, "package.json")

    def test_pyproject_project_as_string_does_not_crash(self) -> None:
        edges, warn = _scan_with(
            "flagships/bad/pyproject.toml", 'project = "oops, not a table"\n'
        )
        self.assertEqual(edges, [GOOD_EDGE])
        self._assert_warned_about(warn, "pyproject.toml")

    def test_pyproject_project_as_array_does_not_crash(self) -> None:
        edges, warn = _scan_with(
            "flagships/bad/pyproject.toml", 'project = ["a", "b"]\n'
        )
        self.assertEqual(edges, [GOOD_EDGE])
        self._assert_warned_about(warn, "pyproject.toml")


class TestEveryCorruptManifestAtOnce(unittest.TestCase):
    """All six corrupt shapes in one tree: the rest must still come through.

    LOUD-ONCE means only the first parser-detectable corruption emits a
    line, but every good edge across the tree must survive and the scan
    must not crash — the strongest form of "without dropping the rest".
    """

    def test_many_good_edges_survive_a_field_of_corruption(self) -> None:
        root = _mkroot()
        # Several good siblings across layers.
        _write(root / "flagships" / "good1" / "go.mod", _GOOD_GOMOD.replace("good", "good1"))
        _write(root / "infrastructure" / "good2" / "go.mod", _GOOD_GOMOD.replace("good", "good2"))
        _write(
            root / "flagships" / "good3" / "Cargo.toml",
            '[package]\nname = "good3"\n[dependencies]\nlimitless-rs = "0.1"\n',
        )
        # One corrupt manifest of every substrate.
        _write(root / "flagships" / "bad-go" / "go.mod", b"\x00 garbage")
        _write(root / "flagships" / "bad-cargo" / "Cargo.toml", "= = broken [[[")
        _write(root / "flagships" / "bad-py" / "pyproject.toml", 'project = "scalar"\n')
        _write(root / "flagships" / "bad-ts" / "package.json", "[1,2,3]")
        _write(root / "flagships" / "bad-rebar" / "rebar.config", "!!! not erlang")
        _write(root / "flagships" / "bad-mix" / "mix.exs", "%%% not elixir")

        sink = io.StringIO()
        edges = Scanner(root=root, _stderr=sink).scan_sorted()

        self.assertIn(Edge(consumer="good1", producer="reality", kind="go"), edges)
        self.assertIn(Edge(consumer="good2", producer="reality", kind="go"), edges)
        self.assertIn(
            Edge(consumer="good3", producer="limitless-rs", kind="rust"), edges
        )
        # No edge originates from any of the bad-* consumers.
        self.assertFalse(
            [e for e in edges if e.consumer.startswith("bad-")],
            msg=f"corrupt manifests leaked edges: {edges}",
        )
        # LOUD-ONCE: at most one warning line for the whole walk.
        self.assertEqual(sink.getvalue().count("[LOUD-ONCE-WARNING]"), 1)


class TestDegradationDeterminism(unittest.TestCase):
    def test_repeated_scans_with_corruption_are_identical(self) -> None:
        # The degraded result must itself be deterministic.
        first, _ = _scan_with("flagships/bad/package.json", "[1,2,3]")
        second, _ = _scan_with("flagships/bad/package.json", "[1,2,3]")
        self.assertEqual(first, second)
        self.assertEqual(first, [GOOD_EDGE])


if __name__ == "__main__":
    unittest.main()
