"""Substrate-aware dependency manifest walker.

Walks the five Limitless monorepo layers (``flagships/``,
``infrastructure/``, ``engines/``, ``foundation/``, ``sdk/``) and yields
:class:`Edge` triples (consumer, producer, kind).

The scanner is intentionally **declaration-only** — it does not resolve
indirect deps or verify that producers actually exist on disk. Its job
is to faithfully report what each manifest says, so a downstream
:class:`~dep_map.graph.Graph` can render a "claimed-edges" SVG. Drift
between declared and resolved edges is a separate audit (see
``apps/audit`` and ``apps/lighthouse``).

R143 LOUD-ONCE-WARNING: unrecognised manifests fire a single stderr
warning per process (via the :class:`LoudOnce` helper below) then skip.

R145 stdlib-only firewall: the scanner imports only ``dataclasses``,
``enum``, ``io``, ``json``, ``os``, ``pathlib``, ``re``, ``sys``,
``tomllib`` from the Python 3.11+ standard library.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator


# ---------------------------------------------------------------------------
# R143 LOUD-ONCE wire — stdlib-only, single-shot stderr warning.
# ---------------------------------------------------------------------------

class LoudOnce:
    """Single-shot warning gate. Fires one structured line then goes quiet.

    Byte-shape of the emitted line matches the cohort wire in
    ``tools/cohort-walker/cohort/observability/loud_once.go``::

        [LOUD-ONCE-WARNING] audit_rule=R143_LOUD_ONCE_WARNING_FLAG message="..."

    Operators / CI grep ``R143_LOUD_ONCE_WARNING_FLAG`` to find emissions
    in their log capture.
    """

    AUDIT_RULE = "R143_LOUD_ONCE_WARNING_FLAG"

    def __init__(self) -> None:
        self._fired = False

    def fire(self, stderr: io.TextIOBase, message: str) -> bool:
        """Emit one warning line; return True iff this call was the first."""
        if self._fired:
            return False
        self._fired = True
        stderr.write(
            f'[LOUD-ONCE-WARNING] audit_rule={self.AUDIT_RULE} message="{message}"\n'
        )
        stderr.flush()
        return True

    def reset(self) -> None:
        """Test-only: re-arm the gate. Production code must never reset."""
        self._fired = False


# ---------------------------------------------------------------------------
# Public data model.
# ---------------------------------------------------------------------------

class NodeKind(str, Enum):
    """Layer classification for a graph node.

    Used by :mod:`dep_map.render` to colour and size nodes. ``UNKNOWN``
    is a deliberate sentinel rather than an exception — a producer that
    is referenced but not yet detected on disk (e.g. a deferred SDK
    extraction) should still appear in the graph as a hub of unknown
    provenance, with its layer to be filled in on the next scan.
    """

    FLAGSHIP = "flagship"
    INFRASTRUCTURE = "infrastructure"
    ENGINE = "engine"
    FOUNDATION = "foundation"
    SDK = "sdk"
    HUB = "hub"
    UNKNOWN = "unknown"


# Hubs are the well-known shared libraries every cohort participant pins.
# Membership matters at render time (bigger glyph, R145.C firewall pin
# colour). The list is data, not code — adding a hub here is the only
# thing required to bring it into the firewall snapshot.
HUB_NAMES = frozenset(
    {
        "limitless-py",
        "limitless-rs",
        "limitless-ts",
        "limitless-jvm",
        "limitless-hs",
        "limitless-beam-otp",
        "limitless-c-crypto",
        "limitless-evidence-bundle",
        "limitless-ai-watermark",
        "limitless-almanac-cohort",
        "lore-mark-verify",
        "limitless-cohort-map",
        "limitless-dep-map",
        "foundation/reality",
        "foundation/pkg",
        "foundation/aicore",
        "foundation/knowledge",
        "nexus-ai",
        "limitless-sdk",
        "limitless-proto",
        "limitless-solidity",
        "limitless-cpp",
        "limitless-dotnet",
        "limitless-ui",
        "forge-go",
    }
)


@dataclass(frozen=True)
class Edge:
    """A declared dependency edge.

    ``consumer`` and ``producer`` are short node names (e.g. ``casino``,
    ``foundation/reality``, ``limitless-beam-otp``). The ``kind`` field
    is the substrate that emitted the edge — useful both as a colour
    discriminator and to verify the renderer counted each manifest.
    """

    consumer: str
    producer: str
    kind: str  # "go" | "rust" | "python" | "ts" | "beam"

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.consumer, self.producer, self.kind)


@dataclass
class Scanner:
    """Walks the five monorepo layers and yields :class:`Edge` triples.

    The scanner is constructed with the absolute root of the monorepo
    (the directory that contains ``flagships/``, ``infrastructure/``,
    etc.). Call :meth:`scan` to get an iterator of edges; the call is
    re-entrant — each call walks disk fresh, so a long-running renderer
    can pick up newly-committed flagships without restart.

    Layer-detection is path-prefix-only — ``flagships/casino`` is a
    flagship; ``foundation/reality`` is a foundation node. The renderer
    consults the same :meth:`classify` method so colours and the layer
    filter stay in lock-step.
    """

    root: Path
    _loud: LoudOnce = field(default_factory=LoudOnce)
    _stderr: io.TextIOBase = field(default=None)  # type: ignore[assignment]

    # Layer paths the walker inspects. Order matters only for the human-
    # facing scan log; edges are deduped before they reach the graph.
    LAYERS: tuple[str, ...] = (
        "flagships",
        "infrastructure",
        "engines",
        "foundation",
        "sdk",
        "apps",
        "tools",
    )

    # Manifest files we know how to parse. Adding a new substrate is a
    # two-step change: add the filename here, add a parser branch in
    # :meth:`_parse_manifest`.
    MANIFEST_FILES: tuple[str, ...] = (
        "go.mod",
        "Cargo.toml",
        "pyproject.toml",
        "package.json",
        "rebar.config",
        "mix.exs",
    )

    # Directories to skip outright — large vendor trees and node_modules
    # blow up walk time and never contain cohort-internal edges.
    SKIP_DIRS: frozenset[str] = frozenset(
        {
            "node_modules",
            ".git",
            "target",
            "build",
            "dist",
            "vendor",
            ".pytest_cache",
            "_build",
            "deps",
            "venv",
            ".venv",
            "__pycache__",
            ".idea",
            ".vscode",
            "lib",  # foundry forge-std + openzeppelin live under flagship/lib
        }
    )

    def __post_init__(self) -> None:
        if self._stderr is None:
            self._stderr = sys.stderr

    # ------------------------------------------------------------------
    # Layer classification.
    # ------------------------------------------------------------------

    def classify(self, path: Path) -> NodeKind:
        """Return the :class:`NodeKind` for a manifest file at ``path``.

        The classification is purely path-prefix-based — symlinks and
        nested workspaces are ignored. The renderer uses this exact
        function so colour ↔ layer-filter alignment is guaranteed.
        """
        try:
            rel = path.relative_to(self.root).parts
        except ValueError:
            return NodeKind.UNKNOWN
        if not rel:
            return NodeKind.UNKNOWN
        head = rel[0]
        if head == "flagships":
            return NodeKind.FLAGSHIP
        if head == "infrastructure":
            return NodeKind.INFRASTRUCTURE
        if head == "engines":
            return NodeKind.ENGINE
        if head == "foundation":
            return NodeKind.FOUNDATION
        if head == "sdk":
            return NodeKind.SDK
        if head in {"apps", "tools"}:
            # Apps/tools that ship as published cohort hubs (limitless-*,
            # lore-mark-verify) classify as HUB; everything else stays
            # UNKNOWN until a downstream caller pins them.
            if len(rel) >= 2 and (
                rel[1].startswith("limitless-")
                or rel[1] in HUB_NAMES
                or rel[1].startswith("lore-")
            ):
                return NodeKind.HUB
            return NodeKind.UNKNOWN
        return NodeKind.UNKNOWN

    # ------------------------------------------------------------------
    # Top-level walk.
    # ------------------------------------------------------------------

    def scan(self) -> Iterator[Edge]:
        """Yield :class:`Edge` triples for every detected manifest.

        Edges are emitted in walk order. Callers wanting a deterministic
        order should sort the result — :meth:`scan_sorted` does that for
        you.
        """
        seen: set[tuple[str, str, str]] = set()
        for layer in self.LAYERS:
            layer_path = self.root / layer
            if not layer_path.is_dir():
                continue
            for manifest in self._walk_layer(layer_path):
                consumer = self._consumer_name(manifest, layer)
                for edge in self._parse_manifest(manifest, consumer):
                    key = edge.as_tuple()
                    if key in seen:
                        continue
                    seen.add(key)
                    yield edge

    def scan_sorted(self) -> list[Edge]:
        """Return all detected edges in stable, deterministic order."""
        edges = list(self.scan())
        edges.sort(key=lambda e: (e.consumer, e.producer, e.kind))
        return edges

    def _walk_layer(self, layer_path: Path) -> Iterator[Path]:
        """Yield every manifest file beneath ``layer_path``."""
        for dirpath, dirnames, filenames in os.walk(layer_path):
            # Prune skip-dirs in-place so os.walk doesn't descend.
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for fname in filenames:
                if fname in self.MANIFEST_FILES:
                    yield Path(dirpath) / fname

    def _consumer_name(self, manifest: Path, layer: str) -> str:
        """Derive the short consumer name for an edge.

        For a manifest at ``flagships/casino/go.mod`` the consumer is
        ``casino``. For nested manifests (``flagships/folio/backend/go.mod``
        or ``flagships/limitless-browser/crates/phantom-core/Cargo.toml``)
        we use the first path segment under the layer root, so backend +
        crate sub-manifests all roll up into one node per flagship.
        """
        try:
            rel = manifest.relative_to(self.root / layer).parts
        except ValueError:
            return manifest.parent.name
        if not rel:
            return manifest.parent.name
        return rel[0]

    # ------------------------------------------------------------------
    # Per-substrate manifest parsing.
    # ------------------------------------------------------------------

    def _parse_manifest(self, path: Path, consumer: str) -> Iterable[Edge]:
        """Dispatch to the substrate-specific parser, defaulting to skip."""
        name = path.name
        try:
            if name == "go.mod":
                return self._parse_go_mod(path, consumer)
            if name == "Cargo.toml":
                return self._parse_cargo_toml(path, consumer)
            if name == "pyproject.toml":
                return self._parse_pyproject_toml(path, consumer)
            if name == "package.json":
                return self._parse_package_json(path, consumer)
            if name == "rebar.config":
                return self._parse_rebar_config(path, consumer)
            if name == "mix.exs":
                return self._parse_mix_exs(path, consumer)
        except (OSError, ValueError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
            # Parsing failures fire a single R143 warning and skip.
            self._loud.fire(
                self._stderr,
                f"manifest parse failed: {path} ({exc.__class__.__name__})",
            )
            return ()
        # Unknown manifest file. Should never hit because MANIFEST_FILES
        # gates the walk, but defence-in-depth.
        self._loud.fire(self._stderr, f"unknown manifest filename: {path}")
        return ()

    # ----- Go ----------------------------------------------------------

    # ``require github.com/davly/<repo> v0.0.0`` or
    # ``require foundation/pkg/<sub> v0.0.0`` — both shapes appear in
    # cohort flagships. We pull out the import path.
    _GO_REQUIRE_RE = re.compile(
        r"^\s*(?:require\s+)?([a-zA-Z0-9_\-./]+)\s+v[0-9]"
    )

    def _parse_go_mod(self, path: Path, consumer: str) -> list[Edge]:
        edges: list[Edge] = []
        in_block = False
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                stripped = line.strip()
                if stripped.startswith("//"):
                    continue
                if stripped.startswith("require ("):
                    in_block = True
                    continue
                if in_block and stripped == ")":
                    in_block = False
                    continue
                if in_block:
                    m = self._GO_REQUIRE_RE.match(stripped)
                    if m:
                        edge = self._go_edge(consumer, m.group(1))
                        if edge is not None:
                            edges.append(edge)
                    continue
                if stripped.startswith("require "):
                    m = self._GO_REQUIRE_RE.match(stripped)
                    if m:
                        edge = self._go_edge(consumer, m.group(1))
                        if edge is not None:
                            edges.append(edge)
        return edges

    def _go_edge(self, consumer: str, import_path: str) -> Edge | None:
        """Translate a Go import path into an :class:`Edge`, or skip.

        We keep three families of edges:

        * ``github.com/davly/<repo>`` — cohort-published library.
        * ``github.com/davly/<repo>/...`` — sub-package of a cohort lib.
        * ``foundation/pkg/<sub>`` — bare in-tree foundation reference.

        Everything else (stdlib third-party deps, indirect deps) is
        dropped.
        """
        if import_path.startswith("github.com/davly/"):
            tail = import_path[len("github.com/davly/"):]
            producer = tail.split("/", 1)[0]
            if not producer or producer == consumer:
                return None
            return Edge(consumer=consumer, producer=producer, kind="go")
        if import_path.startswith("foundation/pkg/"):
            producer = "foundation/pkg"
            return Edge(consumer=consumer, producer=producer, kind="go")
        return None

    # ----- Rust --------------------------------------------------------

    def _parse_cargo_toml(self, path: Path, consumer: str) -> list[Edge]:
        edges: list[Edge] = []
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        for dep_table in ("dependencies", "dev-dependencies", "build-dependencies"):
            deps = data.get(dep_table) or {}
            if not isinstance(deps, dict):
                continue
            for name, spec in deps.items():
                if not isinstance(name, str):
                    continue
                if name.startswith("limitless-") or name in HUB_NAMES:
                    edges.append(Edge(consumer=consumer, producer=name, kind="rust"))
                    continue
                # Path-deps inside the monorepo are also cohort edges.
                if isinstance(spec, dict):
                    raw_path = spec.get("path")
                    if isinstance(raw_path, str):
                        # ``path = "../../foundation/reality-rs"`` -> producer
                        # = "reality-rs". We do not resolve absolute paths
                        # here; the producer name is the last segment.
                        producer = Path(raw_path).name
                        if producer and producer != consumer:
                            edges.append(
                                Edge(consumer=consumer, producer=producer, kind="rust")
                            )
        return edges

    # ----- Python ------------------------------------------------------

    # Strip extras / version specifiers from a PEP 508 dist name.
    _PY_DIST_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")

    def _parse_pyproject_toml(self, path: Path, consumer: str) -> list[Edge]:
        edges: list[Edge] = []
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        proj = data.get("project") or {}
        deps = proj.get("dependencies") or []
        optional = proj.get("optional-dependencies") or {}
        all_deps: list[str] = []
        if isinstance(deps, list):
            all_deps.extend(d for d in deps if isinstance(d, str))
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    all_deps.extend(d for d in group if isinstance(d, str))
        for dep in all_deps:
            m = self._PY_DIST_RE.match(dep)
            if not m:
                continue
            name = m.group(1)
            if name.startswith("limitless-") or name in HUB_NAMES:
                edges.append(Edge(consumer=consumer, producer=name, kind="python"))
        return edges

    # ----- TypeScript / package.json -----------------------------------

    def _parse_package_json(self, path: Path, consumer: str) -> list[Edge]:
        # Skip package.json files inside node_modules (defence-in-depth;
        # SKIP_DIRS should have pruned them already).
        if "node_modules" in path.parts:
            return []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        edges: list[Edge] = []
        for dep_table in ("dependencies", "devDependencies", "peerDependencies"):
            deps = data.get(dep_table) or {}
            if not isinstance(deps, dict):
                continue
            for name in deps:
                if not isinstance(name, str):
                    continue
                if name.startswith("@limitless/"):
                    producer = name[len("@limitless/"):]
                    edges.append(Edge(consumer=consumer, producer=producer, kind="ts"))
                elif name.startswith("limitless-") or name in HUB_NAMES:
                    edges.append(Edge(consumer=consumer, producer=name, kind="ts"))
        return edges

    # ----- BEAM (rebar.config + mix.exs) -------------------------------

    # ``{limitless_beam_otp, "0.1"}`` (Erlang) — tuple with bare atom name.
    _REBAR_DEP_RE = re.compile(r"\{(limitless_[A-Za-z0-9_]+)[\s,}]")

    def _parse_rebar_config(self, path: Path, consumer: str) -> list[Edge]:
        edges: list[Edge] = []
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in self._REBAR_DEP_RE.finditer(text):
            producer = m.group(1).replace("_", "-")
            edges.append(Edge(consumer=consumer, producer=producer, kind="beam"))
        return edges

    # ``{:limitless_beam_otp, "~> 0.1"}`` (Elixir) — atom name with colon.
    _MIX_DEP_RE = re.compile(r"\{\s*:(limitless_[A-Za-z0-9_]+)\s*,")

    def _parse_mix_exs(self, path: Path, consumer: str) -> list[Edge]:
        edges: list[Edge] = []
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in self._MIX_DEP_RE.finditer(text):
            producer = m.group(1).replace("_", "-")
            edges.append(Edge(consumer=consumer, producer=producer, kind="beam"))
        return edges
