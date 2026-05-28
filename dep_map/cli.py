"""Command-line entry point.

Surfaces a single ``dep-map render`` sub-command with three snapshot
modes:

* default — every detected edge (``dep_map_full_*.svg``).
* ``--layer=infra`` — only infrastructure + engine nodes
  (``dep_map_infra_only_*.svg``).
* ``--firewall-only`` — only edges whose producer is a cohort hub
  (``dep_map_cohort_firewall_*.svg``).

Exit codes (stable across versions):

* 0 — render succeeded, SVG written to ``--out``.
* 1 — invalid arguments (handled by argparse).
* 2 — render walked but produced an empty graph (root path likely
  wrong). Empty SVGs are NOT written; the operator gets a clear error.
* 3 — IO error writing the SVG (disk full / permission denied).

R145 stdlib-only — imports ``argparse``, ``pathlib``, ``sys``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dep_map.graph import Graph
from dep_map.render import render_svg
from dep_map.scanner import NodeKind, Scanner


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse :class:`ArgumentParser` for dep-map.

    Factored out so tests can exercise the parser without spawning a
    subprocess.
    """
    parser = argparse.ArgumentParser(
        prog="dep-map",
        description="Limitless ecosystem dependency-graph SVG renderer.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    render = sub.add_parser(
        "render",
        help="Render the ecosystem dependency map to SVG.",
        description=(
            "Walks the monorepo manifest tree and emits a Mirror-Mark "
            "stamped SVG. The default mode emits every detected edge; "
            "--layer and --firewall-only narrow the view."
        ),
    )
    render.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Absolute path to the monorepo root (the directory that contains flagships/, infrastructure/, engines/, foundation/, sdk/).",
    )
    render.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output SVG path. Overwrites if present.",
    )
    render.add_argument(
        "--layer",
        choices=["infra", "flagship", "engine", "foundation", "sdk"],
        default=None,
        help="Restrict the graph to a single layer.",
    )
    render.add_argument(
        "--firewall-only",
        action="store_true",
        help="Restrict the graph to R145.C cohort firewall edges (producer is a known cohort hub).",
    )
    render.add_argument(
        "--title",
        default="Limitless ecosystem dependency map",
        help="Override the SVG title.",
    )
    render.add_argument(
        "--width",
        type=int,
        default=1600,
        help="SVG canvas width (default 1600).",
    )
    render.add_argument(
        "--height",
        type=int,
        default=1600,
        help="SVG canvas height (default 1600).",
    )
    return parser


def _layer_kinds(name: str) -> list[NodeKind]:
    """Translate ``--layer`` argument to the set of :class:`NodeKind` it covers."""
    if name == "infra":
        # The infra view includes engines because they live alongside
        # infrastructure projects in the monorepo and ship the same
        # cohort-shared libs (foundation/reality + nexus-ai).
        return [NodeKind.INFRASTRUCTURE, NodeKind.ENGINE, NodeKind.HUB]
    if name == "flagship":
        return [NodeKind.FLAGSHIP, NodeKind.HUB]
    if name == "engine":
        return [NodeKind.ENGINE, NodeKind.HUB]
    if name == "foundation":
        return [NodeKind.FOUNDATION, NodeKind.HUB]
    if name == "sdk":
        return [NodeKind.SDK, NodeKind.HUB]
    return list(NodeKind)


def main(argv: list[str] | None = None) -> int:
    """Run dep-map. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd != "render":
        parser.print_help(sys.stderr)
        return 1

    root: Path = args.root.resolve()
    if not root.is_dir():
        print(f"dep-map: --root {root} is not a directory", file=sys.stderr)
        return 1

    scanner = Scanner(root=root)
    graph = Graph.from_scanner(scanner)

    if args.layer is not None:
        graph = graph.filter_layer(_layer_kinds(args.layer))
    if args.firewall_only:
        graph = graph.filter_firewall()

    if graph.edge_count() == 0:
        print(
            f"dep-map: empty graph after filters (root={root}, "
            f"layer={args.layer!r}, firewall_only={args.firewall_only}). "
            "Refusing to write an empty SVG.",
            file=sys.stderr,
        )
        return 2

    svg = render_svg(graph, title=args.title, width=args.width, height=args.height)

    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(svg, encoding="utf-8")
    except OSError as exc:
        print(f"dep-map: failed to write {args.out}: {exc}", file=sys.stderr)
        return 3

    print(
        f"dep-map: wrote {args.out} "
        f"(nodes={graph.node_count()} edges={graph.edge_count()})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
