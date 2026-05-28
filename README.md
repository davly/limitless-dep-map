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
  / `foundation/reality` etc. so cohort migrations land planned.

## Install / Run

Pure stdlib — no `pip install` required.

```
git clone https://github.com/davly/limitless-dep-map.git
cd limitless-dep-map
PYTHONPATH=. python -m dep_map.cli render --root /path/to/limitless --out dep_map.svg
```

Sub-commands:

```
dep-map render --root <monorepo> --out <svg>            # full ecosystem
dep-map render --root <monorepo> --out <svg> --layer infra
dep-map render --root <monorepo> --out <svg> --firewall-only
```

Exit codes:

| Exit | Meaning |
|---|---|
| 0  | SVG written |
| 1  | invalid arguments |
| 2  | empty graph after filters (root likely wrong) |
| 3  | IO error writing the SVG |

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

61 tests covering scanner per-substrate, graph adjacency + filters,
renderer determinism + Mirror-Mark round-trip + R166 footer + CLI parser.

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
