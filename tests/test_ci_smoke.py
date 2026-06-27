"""CI smoke test — the fast "is the package wired together?" guard.

This is the check a CI run leans on first: it does not exercise deep
behaviour (the other suites do) but it fails loudly if the package is
broken in a way that would make ``pipx install limitless-dep-map`` or a
bare import fall over:

* every public module imports and the re-export surface resolves;
* the ``[project.scripts]`` console-script entry point that makes the
  package pipx-installable is declared and points at a real callable
  (this is the property the toolkit install-all script depends on);
* both sub-commands (``render`` + ``query``) are wired into the parser;
* ``--help`` exits 0; and a minimal end-to-end ``query`` returns 0 with
  parseable JSON.

Stdlib-only (``argparse`` + ``contextlib`` + ``io`` + ``json`` +
``tempfile`` + ``tomllib`` + ``unittest``) per the R145 firewall, so it
runs under ``python -m unittest`` with no pip step.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dep_map import cli  # noqa: E402


class TestImportSurface(unittest.TestCase):
    def test_all_modules_import(self) -> None:
        import dep_map  # noqa: F401
        import dep_map.cli  # noqa: F401
        import dep_map.graph  # noqa: F401
        import dep_map.render  # noqa: F401
        import dep_map.scanner  # noqa: F401

    def test_top_level_reexports_resolve(self) -> None:
        from dep_map import (  # noqa: F401
            Edge,
            Graph,
            NodeKind,
            Scanner,
            render_svg,
        )


class TestPipxInstallableContract(unittest.TestCase):
    """Locks in uplift #26: the console-script entry point must exist."""

    def _scripts(self) -> dict:
        with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
        return data.get("project", {}).get("scripts", {})

    def test_console_script_declared(self) -> None:
        self.assertEqual(self._scripts().get("dep-map"), "dep_map.cli:main")

    def test_entry_point_target_is_callable(self) -> None:
        # The "module:attr" target must resolve to a real callable, else
        # the generated console script would crash at install/run time.
        from dep_map.cli import main

        self.assertTrue(callable(main))


class TestParserWiring(unittest.TestCase):
    def test_both_subcommands_registered(self) -> None:
        parser = cli.build_parser()
        sub_actions = [
            a
            for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        ]
        self.assertEqual(len(sub_actions), 1)
        self.assertEqual(set(sub_actions[0].choices), {"render", "query"})

    def test_help_exits_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


class TestEndToEndSmoke(unittest.TestCase):
    def test_query_round_trip(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="depmap_smoke_"))
        for layer in ("flagships", "infrastructure", "engines", "foundation", "sdk"):
            (root / layer).mkdir()
        gomod = root / "flagships" / "casino" / "go.mod"
        gomod.parent.mkdir(parents=True)
        gomod.write_text(
            "module github.com/davly/casino\n"
            "require github.com/davly/reality v0.0.0\n",
            encoding="utf-8",
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                ["query", "--root", str(root), "blast-radius", "--node", "reality"]
            )
        self.assertEqual(code, 0, msg=err.getvalue())
        env = json.loads(out.getvalue())
        self.assertEqual(env["result"], ["casino"])


if __name__ == "__main__":
    unittest.main()
