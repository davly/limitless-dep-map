"""Renderer + Mirror-Mark stamp round-trip tests.

Exercises the deterministic SVG layout, the Mirror-Mark stamp, the R166
liability footer literal, and the CLI argparse surface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dep_map import cli  # noqa: E402
from dep_map.graph import Graph  # noqa: E402
from dep_map.render import (  # noqa: E402
    DEV_CORPUS_SHA,
    DEV_KEY,
    LIABILITY_FOOTER,
    LIABILITY_TAG,
    MARK_PREFIX,
    extract_mark,
    render_svg,
    sign,
)
from dep_map.scanner import Edge, NodeKind  # noqa: E402


def _e(c: str, p: str, k: str = "go") -> Edge:
    return Edge(consumer=c, producer=p, kind=k)


def _sample_graph() -> Graph:
    g = Graph.from_edges([
        _e("casino", "reality"),
        _e("ledger", "reality"),
        _e("folio", "limitless-beam-otp"),
    ])
    g.node_kinds = {
        "casino": NodeKind.FLAGSHIP,
        "ledger": NodeKind.FLAGSHIP,
        "folio": NodeKind.FLAGSHIP,
        "reality": NodeKind.FOUNDATION,
        "limitless-beam-otp": NodeKind.HUB,
    }
    return g


class TestSign(unittest.TestCase):
    """The sign() function transcribes apps/lore-mark-verify Sign() byte-for-byte."""

    def test_sign_prefix(self) -> None:
        mark = sign(DEV_CORPUS_SHA, b"hello", DEV_KEY)
        self.assertTrue(mark.startswith(MARK_PREFIX))

    def test_sign_is_deterministic(self) -> None:
        m1 = sign(DEV_CORPUS_SHA, b"hello", DEV_KEY)
        m2 = sign(DEV_CORPUS_SHA, b"hello", DEV_KEY)
        self.assertEqual(m1, m2)

    def test_sign_matches_canonical_algorithm(self) -> None:
        """Re-derive the mark independently and compare byte-for-byte."""
        payload = b"hello"
        mac = hmac.new(DEV_KEY, digestmod=hashlib.sha256)
        mac.update(bytes([0x01]))
        mac.update(DEV_CORPUS_SHA)
        mac.update(payload)
        digest = mac.digest()
        body = DEV_CORPUS_SHA[:8] + digest
        expected = "lore@v1:" + base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")
        self.assertEqual(sign(DEV_CORPUS_SHA, payload, DEV_KEY), expected)

    def test_sign_rejects_wrong_corpus_length(self) -> None:
        with self.assertRaises(ValueError):
            sign(b"too-short", b"hello", DEV_KEY)


class TestRenderSVG(unittest.TestCase):
    def test_renders_valid_xml(self) -> None:
        svg = render_svg(_sample_graph())
        # Should parse as XML and have an <svg> root.
        root = ET.fromstring(_strip_decl(svg))
        self.assertTrue(root.tag.endswith("svg"))

    def test_renders_title(self) -> None:
        svg = render_svg(_sample_graph(), title="Test title XYZ")
        self.assertIn("Test title XYZ", svg)

    def test_renders_node_count_meta(self) -> None:
        svg = render_svg(_sample_graph())
        self.assertIn("nodes=5", svg)
        self.assertIn("edges=3", svg)

    def test_renders_liability_footer(self) -> None:
        svg = render_svg(_sample_graph())
        # The footer is multi-line in the SVG text elements — sample a
        # distinctive substring that survives word-wrapping.
        self.assertIn("NOT LEGAL ADVICE", svg)
        self.assertIn(LIABILITY_TAG, svg)

    def test_renders_legend(self) -> None:
        svg = render_svg(_sample_graph())
        self.assertIn("flagship", svg)
        self.assertIn("foundation", svg)
        self.assertIn("hub", svg)
        self.assertIn("R145.C cohort firewall edge", svg)

    def test_stamp_comment_present(self) -> None:
        svg = render_svg(_sample_graph())
        first_lines = svg.splitlines()[:3]
        joined = "\n".join(first_lines)
        self.assertIn("lore@v1:", joined)
        self.assertIn("L43_MIRROR_MARK_V1", joined)

    def test_extract_mark_round_trip(self) -> None:
        svg = render_svg(_sample_graph())
        mark = extract_mark(svg)
        self.assertTrue(mark.startswith("lore@v1:"))
        self.assertGreater(len(mark), len("lore@v1:"))

    def test_extract_mark_raises_on_unstamped_svg(self) -> None:
        with self.assertRaises(ValueError):
            extract_mark("<svg></svg>")

    def test_render_is_byte_deterministic(self) -> None:
        g = _sample_graph()
        a = render_svg(g)
        b = render_svg(g)
        self.assertEqual(a, b)


class TestRenderEdgeCases(unittest.TestCase):
    def test_renders_empty_graph_safely(self) -> None:
        # Render should not raise on an empty graph — the CLI is
        # responsible for refusing to write empty SVGs, but the renderer
        # itself is robust.
        svg = render_svg(Graph())
        self.assertIn("nodes=0", svg)
        self.assertIn("edges=0", svg)

    def test_firewall_edge_is_thicker(self) -> None:
        # Build a graph with one firewall edge and one non-firewall edge
        # of the same kind, and inspect their stroke widths.
        g = Graph.from_edges([
            _e("a", "limitless-rs", "rust"),
            _e("a", "non-hub-thing", "rust"),
        ])
        g.node_kinds = {
            "a": NodeKind.FLAGSHIP,
            "limitless-rs": NodeKind.HUB,
            "non-hub-thing": NodeKind.UNKNOWN,
        }
        svg = render_svg(g)
        # Firewall edges render at stroke-width 3.0; non-firewall at 1.0.
        self.assertIn('stroke-width="3.0"', svg)
        self.assertIn('stroke-width="1.0"', svg)


class TestLiabilityFooter(unittest.TestCase):
    def test_footer_carries_canonical_phrases(self) -> None:
        # R166 cohort-canonical phrases (also present in cohort-walker's
        # liability footer Go literal).
        self.assertIn("NOT LEGAL ADVICE", LIABILITY_FOOTER)
        self.assertIn("INFORMATIONAL USE ONLY", LIABILITY_FOOTER)
        self.assertIn("dep-map", LIABILITY_FOOTER)
        self.assertIn("MUST", LIABILITY_FOOTER)


class TestCLI(unittest.TestCase):
    def test_parser_accepts_render(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(["render", "--root", "/tmp/x", "--out", "/tmp/x.svg"])
        self.assertEqual(ns.cmd, "render")
        self.assertEqual(ns.layer, None)
        self.assertFalse(ns.firewall_only)

    def test_parser_accepts_layer(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args([
            "render", "--root", "/tmp/x", "--out", "/tmp/x.svg",
            "--layer", "infra",
        ])
        self.assertEqual(ns.layer, "infra")

    def test_parser_accepts_firewall(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args([
            "render", "--root", "/tmp/x", "--out", "/tmp/x.svg",
            "--firewall-only",
        ])
        self.assertTrue(ns.firewall_only)

    def test_layer_kinds_infra(self) -> None:
        kinds = cli._layer_kinds("infra")
        self.assertIn(NodeKind.INFRASTRUCTURE, kinds)
        self.assertIn(NodeKind.ENGINE, kinds)


def _strip_decl(svg: str) -> str:
    """Strip the XML declaration + Mirror-Mark comment so ET.fromstring works.

    ``xml.etree.ElementTree.fromstring`` rejects an XML declaration when
    called on a substring; we strip it for the parse-validity test.
    """
    lines = svg.splitlines()
    body_lines = []
    seen_svg = False
    for ln in lines:
        if seen_svg:
            body_lines.append(ln)
            continue
        if ln.lstrip().startswith("<svg"):
            seen_svg = True
            body_lines.append(ln)
    return "\n".join(body_lines)


if __name__ == "__main__":
    unittest.main()
