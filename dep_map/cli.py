"""Command-line entry point.

Two sub-commands.

``dep-map render`` — emit the dependency map as SVG, with three snapshot
modes:

* default — every detected edge (``dep_map_full_*.svg``).
* ``--layer=infra`` — only infrastructure + engine nodes
  (``dep_map_infra_only_*.svg``).
* ``--firewall-only`` — only edges whose producer is a cohort hub
  (``dep_map_cohort_firewall_*.svg``).

``dep-map query`` — answer one DAG query as a deterministic JSON
envelope on stdout (``{"node": ..., "query": ..., "result": ...}`` with
sorted object keys). This surfaces the underlying :class:`Graph` queries
— previously library-only and unreachable from the CLI — for scripting
and CI. Query kinds: ``blast-radius`` / ``upstream`` / ``consumers`` /
``producers`` (node-scoped, need ``--node``); ``has-cycle`` / ``topo`` /
``hub-degree`` (graph-scoped).

Exit codes for ``render`` (stable across versions):

* 0 — render succeeded, SVG written to ``--out``.
* 1 — invalid arguments (handled by argparse).
* 2 — render walked but produced an empty graph (root path likely
  wrong). Empty SVGs are NOT written; the operator gets a clear error.
* 3 — IO error writing the SVG (disk full / permission denied).

Exit codes for ``query``:

* 0 — query answered, JSON written to stdout.
* 1 — invalid arguments (bad ``--root``; ``--node`` required-but-missing
  or supplied-but-rejected).
* 4 — query could not be answered (``topo`` on a cyclic graph).

R145 stdlib-only — imports ``argparse``, ``json``, ``pathlib``, ``sys``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dep_map.graph import Graph
from dep_map.render import render_svg
from dep_map.scanner import NodeKind, Scanner

# Query kinds the ``query`` sub-command can answer. The four node-scoped
# queries require ``--node``; the three graph-scoped queries reject it.
_NODE_QUERIES: frozenset[str] = frozenset(
    {"blast-radius", "upstream", "consumers", "producers"}
)
_GRAPH_QUERIES: frozenset[str] = frozenset({"has-cycle", "topo", "hub-degree"})
QUERY_KINDS: tuple[str, ...] = (
    # Sorted so --help lists them deterministically.
    "blast-radius",
    "consumers",
    "has-cycle",
    "hub-degree",
    "producers",
    "topo",
    "upstream",
)


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

    query = sub.add_parser(
        "query",
        help="Answer a deterministic DAG query about the graph as sorted JSON.",
        description=(
            "Walks the monorepo manifest tree, builds the dependency "
            "graph, and answers one DAG query as a deterministic JSON "
            "envelope on stdout. Output is byte-reproducible: object keys "
            "are sorted and result collections are emitted in a stable "
            "order (node lists sorted by name; hub-degree by descending "
            "degree then name; topo in topological order). The default "
            "render sub-command produces an SVG; this one exposes the "
            "underlying graph queries for scripting and CI."
        ),
    )
    query.add_argument(
        "kind",
        choices=list(QUERY_KINDS),
        metavar="KIND",
        help=(
            "Query to answer. Node-scoped (need --node): blast-radius "
            "(transitive consumers), upstream (transitive producers), "
            "consumers (direct), producers (direct). Graph-scoped (no "
            "--node): has-cycle, topo, hub-degree."
        ),
    )
    query.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Absolute path to the monorepo root (same as render --root).",
    )
    query.add_argument(
        "--node",
        default=None,
        help="Node name for node-scoped queries (e.g. foundation/reality).",
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


def _query_result(graph: Graph, kind: str, node: str | None):
    """Compute one query answer from an already-built :class:`Graph`.

    Returns a JSON-serialisable, deterministically-ordered value:

    * node-scoped set queries -> a name-sorted ``list[str]``;
    * ``has-cycle`` -> ``bool``;
    * ``topo`` -> ``list[str]`` in topological order (raises
      :class:`ValueError` on a cyclic graph, propagated to the caller);
    * ``hub-degree`` -> ``list[[name, degree]]`` in descending-degree,
      ascending-name order (mirrors :meth:`Graph.hub_degree`).
    """
    if kind == "blast-radius":
        return sorted(graph.transitive_consumers(node))  # type: ignore[arg-type]
    if kind == "upstream":
        return sorted(graph.transitive_producers(node))  # type: ignore[arg-type]
    if kind == "consumers":
        return sorted(graph.consumers_of(node))  # type: ignore[arg-type]
    if kind == "producers":
        return sorted(graph.producers_of(node))  # type: ignore[arg-type]
    if kind == "has-cycle":
        return graph.has_cycle()
    if kind == "topo":
        return graph.topological_order()
    if kind == "hub-degree":
        return [[name, degree] for name, degree in graph.hub_degree().items()]
    # Unreachable: argparse `choices` gates kind.
    raise ValueError(f"unknown query kind: {kind!r}")


def _run_query(args: argparse.Namespace) -> int:
    """Execute the ``query`` sub-command. Returns the process exit code.

    Exit codes (distinct from render's, documented in the module
    docstring):

    * 0 — query answered, JSON envelope written to stdout.
    * 1 — invalid arguments (bad ``--root``; ``--node`` required-but-
      missing or supplied-but-rejected for the chosen query).
    * 4 — query could not be answered: ``topo`` on a cyclic graph
      (use ``has-cycle`` to detect first).
    """
    root: Path = args.root.resolve()
    if not root.is_dir():
        print(f"dep-map: --root {root} is not a directory", file=sys.stderr)
        return 1

    kind: str = args.kind
    node: str | None = args.node
    if kind in _NODE_QUERIES and node is None:
        print(
            f"dep-map: query '{kind}' requires --node <name>", file=sys.stderr
        )
        return 1
    if kind not in _NODE_QUERIES and node is not None:
        print(
            f"dep-map: query '{kind}' does not take --node", file=sys.stderr
        )
        return 1

    scanner = Scanner(root=root)
    graph = Graph.from_scanner(scanner)

    try:
        result = _query_result(graph, kind, node)
    except ValueError as exc:
        print(f"dep-map: cannot answer '{kind}': {exc}", file=sys.stderr)
        return 4

    envelope = {"query": kind, "node": node, "result": result}
    print(json.dumps(envelope, sort_keys=True, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run dep-map. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "query":
        return _run_query(args)
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
