"""dep-map — Limitless ecosystem dependency-graph renderer.

Pure-stdlib Python 3.11+ tool that walks the Limitless monorepo
(``flagships/``, ``infrastructure/``, ``engines/``, ``foundation/``,
``sdk/``) and emits an SVG dependency map of cohort-internal edges.

The renderer is deliberately layered so each substrate detector and each
SVG glyph is unit-testable in isolation:

- :mod:`dep_map.scanner` — walks per-substrate manifest files and emits
  ``(consumer, producer, kind)`` triples. Substrates supported:

  * Go modules (``go.mod``) — ``require`` lines naming
    ``github.com/davly/*`` or ``foundation/pkg/*``.
  * Rust crates (``Cargo.toml``) — ``[dependencies]`` keys starting
    ``limitless-`` or path-deps pointing inside the monorepo.
  * Python packages (``pyproject.toml``) — ``dependencies = []`` items
    whose distribution name starts ``limitless-``.
  * TypeScript packages (``package.json``) — ``dependencies`` keys
    starting ``@limitless/``.
  * BEAM (``rebar.config`` / ``mix.exs``) — ``{:limitless_*,`` /
    ``{limitless_*,`` deps.

- :mod:`dep_map.graph` — builds an in-memory bidirectional adjacency
  ``Graph`` from a list of edges. Producer→consumer and consumer→producer
  iteration are both O(1) per source.

- :mod:`dep_map.render` — emits a pure-stdlib SVG (``xml.etree``) using
  a deterministic radial layout. Cohort-firewall edges (R145.C pins) are
  bolded; SDK + shared-lib hub nodes (``limitless-py`` etc.) render
  larger; infra-layer nodes use a distinct fill from flagship nodes.

- :mod:`dep_map.cli` — argparse-driven entry point exposing
  ``dep-map render --root=<path> --out=<svg>`` plus the snapshot sub-
  filters used to produce the three committed snapshots:

  * ``--all`` (default) — every detected edge.
  * ``--layer=infra`` — only ``infrastructure/`` and ``engines/`` nodes.
  * ``--firewall-only`` — only edges that participate in a R145.C cohort
    firewall pin (currently: consumers of ``limitless-beam-otp``,
    ``limitless-c-crypto``, and the cohort SDKs).

The dep-map is **not** a build-graph driver. It is an audit-grade
visualisation of declared monorepo edges, intended to surface:

1. **Cohort firewall integrity** — R145.C requires every cross-substrate
   port to pin a shared library. The firewall SVG snapshot answers
   "which flagships consume the cohort hubs" at a glance.
2. **Layer leakage** — when a flagship reaches across the layer boundary
   (flagship importing engine internals, or engine importing flagship),
   the edge shows up.
3. **Hub degree** — degree-centrality of ``limitless-py`` /
   ``limitless-rs`` / ``reality`` etc. signals where a cohort
   migration would land hardest. (Hub names are the scanner-emitted
   producer spellings — the Go repo tail, e.g. ``reality``, not
   ``foundation/reality``.)

R145 (stdlib-only firewall) — this package imports nothing outside the
Python standard library. ``xml.etree.ElementTree`` produces the SVG;
``tomllib`` parses TOML; ``json`` parses package.json; manual line-scan
parses go.mod / mix.exs / rebar.config.

R143 (LOUD-ONCE-WARNING) — unknown manifests fire a single structured
``[LOUD-ONCE-WARNING] audit_rule=R143_LOUD_ONCE_WARNING_FLAG`` stderr
line and skip; the walker never aborts on bad input.

R166 (LIABILITY-FOOTER-CONST) — see
:data:`dep_map.render.LIABILITY_FOOTER`.

L43 Mirror-Mark v1 — each emitted SVG carries a ``<!-- lore@v1: ... -->``
comment derived from the SVG body bytes; the comment is verifiable cold
against the cohort-canonical KAT-1 corpus key via ``lore-mark-verify
verify``.
"""

__version__ = "0.1.0"

# Re-export the high-traffic surfaces so callers can do
# ``from dep_map import Scanner, Graph, render_svg`` without reaching
# into sub-modules. The sub-modules remain the source of truth.
from dep_map.scanner import Scanner, Edge, NodeKind  # noqa: E402,F401
from dep_map.graph import Graph  # noqa: E402,F401
from dep_map.render import render_svg, LIABILITY_FOOTER  # noqa: E402,F401

__all__ = [
    "Scanner",
    "Edge",
    "NodeKind",
    "Graph",
    "render_svg",
    "LIABILITY_FOOTER",
    "__version__",
]
