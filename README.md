# limitless-dep-map

Ecosystem dependency-graph renderer for the Limitless monorepo.

Pure-Python 3.11+ stdlib tool that walks the five monorepo layers
(`flagships/`, `infrastructure/`, `engines/`, `foundation/`, `sdk/`)
and emits Mirror-Mark stamped SVG dependency maps. Companion to
`limitless-cohort-map` (structural snapshot) — this one is the
dependency graph.

## Why

- **Cohort firewall integrity** (R145.C) — surfaces "which flagships
  consume the cohort hubs" at a glance.
- **Layer leakage** — flagships reaching across the layer boundary.
- **Hub degree** — degree-centrality of `limitless-py` / `limitless-rs`
  / `reality` etc. so cohort migrations land planned. Hub names are the
  scanner-emitted producer spellings (the Go repo tail: `reality`, not
  `foundation/reality`); any node with ≥ 3 consumers is also reported
  as an emergent hub even if the curated list omits it.

## Install / Run

Pure stdlib — no `pip install` required.

```
git clone https://github.com/davly/limitless-dep-map.git
cd limitless-dep-map
PYTHONPATH=. python -m dep_map.cli render --root /path/to/limitless --out dep_map.svg
```

`dep-map --version` prints `dep-map <version>` and exits 0 (the tool
release version; distinct from the query envelope's `schema_version`).

### `render` — emit the dependency map as SVG

```
dep-map render --root <monorepo> --out <svg>            # full ecosystem (default)
dep-map render --root <monorepo> --out <svg> --layer infra
dep-map render --root <monorepo> --out <svg> --firewall-only
```

Flags: `--layer {infra,flagship,engine,foundation,sdk}` restricts to a
single layer; `--firewall-only` keeps only R145.C cohort-firewall edges
(producer is a known cohort hub); `--title`, `--width` (default 1600),
`--height` (default 1600) control the canvas.

`render` exit codes (stable across versions):

| Exit | Meaning |
|---|---|
| 0  | SVG written to `--out` |
| 1  | invalid arguments |
| 2  | empty graph after filters (root likely wrong); no SVG written |
| 3  | IO error writing the SVG (disk full / permission denied) |

### `query` — answer one DAG query as deterministic JSON

```
dep-map query --root <monorepo> <KIND> [--node <name>]
```

Writes a byte-reproducible JSON envelope to stdout (object keys sorted;
result collections in a stable order):

```json
{
  "known": true,
  "node": "reality",
  "query": "blast-radius",
  "result": ["casino", "ledger", "report"],
  "schema_version": 1
}
```

- `known` — honesty flag. For node-scoped queries it is `true` iff
  `--node` exists in the graph and `false` for an unknown (typo'd) node;
  `null` for graph-scoped queries (which take no node).
- `schema_version` — integer envelope-shape version (bumped only when a
  key is added/removed/retyped), so a consumer detects shape drift
  without speculatively parsing.

Query kinds:

| KIND | Scope | Result |
|---|---|---|
| `blast-radius` | node (`--node`) | transitive consumers, name-sorted |
| `upstream`     | node (`--node`) | transitive producers, name-sorted |
| `consumers`    | node (`--node`) | direct consumers, name-sorted |
| `producers`    | node (`--node`) | direct producers, name-sorted |
| `has-cycle`    | graph           | `bool` |
| `topo`         | graph           | nodes in topological order |
| `hub-degree`   | graph           | `[name, degree]` pairs, descending degree then name (curated `HUB_NAMES` + any emergent hub with ≥ 3 consumers) |
| `graph`        | graph           | `{edges, nodes}` — the whole DAG |
| `edges`        | graph           | every edge `{consumer, kind, producer}`, sorted |
| `nodes`        | graph           | every node `{kind, name}` with its layer, sorted |
| `stats`        | graph           | summary counts (edge/node totals, by-kind + by-layer histograms, `has_cycle`) |

`query` exit codes:

| Exit | Meaning |
|---|---|
| 0  | query answered, JSON envelope written to stdout |
| 1  | invalid arguments (bad `--root`; `--node` required-but-missing or supplied-but-rejected) |
| 2  | empty graph — the walk found no cohort manifest edges (existing-but-wrong `--root`); no envelope written, mirroring `render`'s empty-graph refusal |
| 4  | query could not be answered (`topo` on a cyclic graph — use `has-cycle` first) |
| 5  | node-scoped query against an unknown node (envelope still written with `"known": false`; non-zero exit stops a typo reading as "no dependents") |

## Substrates detected

| Manifest        | Edge shape                                                       |
|-----------------|------------------------------------------------------------------|
| `go.mod`        | `require github.com/davly/<repo>` + `foundation/pkg/*`           |
| `Cargo.toml`    | `[dependencies] limitless-* = ...`, path-deps inside monorepo    |
| `pyproject.toml`| `dependencies = ["limitless-...]`                                |
| `package.json`  | `dependencies: { "@limitless/...": ..., "limitless-...": ... }`  |
| `rebar.config`  | `{limitless_*, ...}`                                             |
| `mix.exs`       | `{:limitless_*, ...}`                                            |

## Snapshots

Committed under `snapshots/`:

- `dep_map_full_2026-05-28.svg` — every detected edge (~172 edges).
- `dep_map_infra_only_2026-05-28.svg` — infrastructure + engine layer.
- `dep_map_cohort_firewall_2026-05-28.svg` — R145.C firewall edges only.

Each SVG carries:

- An L43 Mirror-Mark v1 stamp (`lore@v1:` comment on line 2) verifiable
  cold against the cohort-canonical KAT-1 corpus via `sign()` re-
  derivation. Algorithm is HMAC-SHA256 transcribed byte-for-byte from
  `apps/lore-mark-verify/internal/verify/verify.go`.
- An R166 LIABILITY-FOOTER-CONST literal as in-SVG `<text>` footer
  ("NOT LEGAL ADVICE — TOOL OUTPUT FOR INFORMATIONAL USE ONLY") +
  machine-readable `audit_rule=R166_LIABILITY_FOOTER_CONST` tag.

## Cohort discipline

- **R145 stdlib-only firewall.** Zero third-party Python deps. The
  renderer uses `xml.etree.ElementTree`, `tomllib`, `json`, `hashlib`,
  `hmac`, `base64`, `math` — nothing else.
- **R143 LOUD-ONCE-WARNING.** Unrecognised manifests fire exactly one
  `[LOUD-ONCE-WARNING] audit_rule=R143_LOUD_ONCE_WARNING_FLAG ...`
  stderr line then skip.
- **R166 LIABILITY-FOOTER-CONST.** Every SVG carries the canonical
  shape liability footer + audit_rule tag.
- **L43 Mirror-Mark v1 verifiable.** The transcribed `sign()` matches
  `engines/oracle/internal/mirrormark/mark.go` byte-for-byte; round-
  trip tested in `tests/test_render.py`.

## Tests

```
PYTHONPATH=. python -m unittest discover -s tests -v
```

204 tests covering scanner per-substrate, graph adjacency + filters +
exports, hub-name spelling contract + emergent-hub safeguard, renderer
determinism (incl. cross-process) + Mirror-Mark round-trip + R166
footer, the CLI parser, both sub-commands end-to-end (render exit codes
0-3; query envelope, kinds, exit codes 0/1/2/4/5, `schema_version`),
and `--version`.

## Cohort cross-references

- `tools/cohort-walker/cohort/observability/loud_once.go` — R143 wire.
- `tools/cohort-walker/cohort/legal/liability_footer.go` — R166 cohort canonical.
- `apps/lore-mark-verify/internal/verify/verify.go` — Mirror-Mark v1 algorithm.
- `engines/oracle/internal/mirrormark/mark.go` — algorithm source-of-truth.
- `tools/cohort-map` — sibling tool (structural snapshot).
- `tools/cohort-walker` — sibling tool (drift scanner).

## Limits

This is a **declared-edges** map. It does not resolve indirect deps,
verify producers actually exist on disk, or detect runtime DI wiring.
Drift between declared and resolved edges is a separate audit (see
`apps/audit`, `apps/lighthouse`).

The SVG layout is deterministic radial: hubs on an inner ring, non-hub
consumers on concentric outer rings. The layout is intentional — the
Mirror-Mark stamp is computed over SVG body bytes, so any non-
determinism would break round-trip verification.
