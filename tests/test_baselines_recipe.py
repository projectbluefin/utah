"""`just baselines` must find Dakota's SBOM, or say why it could not.

The recipe picks the newest successful projectbluefin/dakota publish.yml run
that still carries the `sbom-dakota` artifact. It filtered those runs by the
`main` branch, but Dakota's publish.yml only runs for `testing` and `next`, so
the lookup matched nothing. The `grep -q ... && { ...; }` loop then returned 1
and `set -e` ended the recipe with no message: the weekly "Refresh image
baselines" workflow failed silently every Monday.

The recipe body is extracted from the Justfile and executed with `gh`,
`podman` and `python3` stubbed on PATH, so the assertions are about what the
shell actually does rather than about the text of the recipe.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "Justfile"
RECIPE = "baselines"

# `gh run list` prints $RUNS; `gh api .../runs/<id>/artifacts` prints the
# artifact names listed in $ARTIFACTS/<id> (no file: no artifacts).
STUB_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$1 $2" in
  "run list") printf '%s' "$RUNS" ;;
  "api "*)
    id=$(printf '%s' "$2" | sed -n 's#.*/runs/\\([0-9]*\\)/artifacts#\\1#p')
    [ -f "$ARTIFACTS/$id" ] && cat "$ARTIFACTS/$id"
    ;;
esac
exit 0
"""

STUB_RECORD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
exit 0
"""


def recipe_body(name: str) -> str:
    """Return the shell body of `name`, dedented, with parameters defaulted."""
    lines = JUSTFILE.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{name}:") or line.startswith(f"{name} "):
            header, start = line, index + 1
            break
    else:
        raise AssertionError(f"{JUSTFILE} has no recipe named {name!r}")

    body: list[str] = []
    for line in lines[start:]:
        if line and not line.startswith((" ", "\t")):
            break
        body.append(line)
    text = textwrap.dedent("\n".join(body).rstrip())
    if not text.startswith("#!"):
        raise AssertionError(f"{name} is not a shebang recipe")

    defaults = dict(re.findall(r'(\w+)="([^"]*)"', header))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: defaults[m.group(1)], text)


class BaselinesDakotaLookup(unittest.TestCase):
    def run_recipe(self, runs: list[str], artifacts: dict[str, list[str]]):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, stub in (("gh", STUB_GH), ("podman", STUB_RECORD), ("python3", STUB_RECORD)):
            path = bin_dir / name
            path.write_text(stub)
            path.chmod(0o755)
        art_dir = tmp / "artifacts"
        art_dir.mkdir()
        for run_id, names in artifacts.items():
            (art_dir / run_id).write_text("".join(f"{n}\n" for n in names))
        script = tmp / "recipe.sh"
        script.write_text(recipe_body(RECIPE))
        script.chmod(0o755)

        env = dict(os.environ)
        env.update(
            PATH=f"{bin_dir}:{env['PATH']}",
            GH_CALLS=str(tmp / "gh-calls"),
            CALLS=str(tmp / "calls"),
            RUNS="".join(f"{r}\n" for r in runs),
            ARTIFACTS=str(art_dir),
        )
        proc = subprocess.run(["bash", str(script)], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=60)
        read = lambda p: p.read_text().splitlines() if p.exists() else []
        return proc, read(tmp / "gh-calls"), read(tmp / "calls")

    def test_queries_the_branch_dakota_publishes_from(self):
        _, gh_calls, _ = self.run_recipe(["11"], {"11": ["sbom-dakota"]})
        run_list = [c for c in gh_calls if c.startswith("run list")]
        self.assertEqual(len(run_list), 1)
        argv = run_list[0].split()
        self.assertIn("publish.yml", argv)
        self.assertEqual(argv[argv.index("-b") + 1], "testing")

    def test_uses_the_newest_run_that_still_has_the_sbom(self):
        proc, _, calls = self.run_recipe(
            ["30", "20", "10"],
            {"30": ["digest-default"], "20": ["digest-default", "sbom-dakota"],
             "10": ["sbom-dakota"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("scripts/image-baseline.py dakota 20 baselines/dakota", calls)
        self.assertEqual(calls[-1], "scripts/image-baseline.py gap")

    def test_no_sbom_run_fails_with_a_message(self):
        proc, _, calls = self.run_recipe(["30", "20"], {"30": ["digest-default"]})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("sbom-dakota", proc.stderr)
        self.assertFalse([c for c in calls if "image-baseline.py dakota" in c], calls)

    def test_no_runs_at_all_fails_with_a_message(self):
        proc, _, calls = self.run_recipe([], {})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no successful projectbluefin/dakota publish.yml run", proc.stderr)
        self.assertFalse([c for c in calls if "image-baseline.py dakota" in c], calls)


if __name__ == "__main__":
    unittest.main()
