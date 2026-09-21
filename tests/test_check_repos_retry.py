"""`just check-repos` must survive a registry hiccup without masking a real one.

`check-repos` is the `contract` preflight in `.github/workflows/build.yml`, and
it gates the entire flavor matrix. It ends in `podman run` against the pinned
base image, which pulls that image, so a CDN that drops a blob mid-read fails
the gate before any package is evaluated -- the container engine reports exit
125, meaning it could not run the container at all.

Retrying that is worth it. Retrying a genuine package-resolution failure is
not: the resolve is slow, and the answer would not change, so a real failure
would only get later and quieter. The recipe therefore discriminates on 125,
and these tests pin both halves of that contract -- retry on 125, pass straight
through on everything else.

The recipe body is extracted from the Justfile and executed with `python3` and
`sleep` stubbed on PATH, so the assertions are about what the shell actually
does rather than about the text of the recipe.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "Justfile"
RECIPE = "check-repos"

STUB_PYTHON3 = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
attempt=$(wc -l < "$CALLS")
code=$(sed -n "${attempt}p" "$CODES")
exit "${code:-0}"
"""

STUB_SLEEP = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SLEEPS"
exit 0
"""


def recipe_body(name: str) -> str:
    """Return the shell body of `name`, dedented, without the recipe header."""
    lines = JUSTFILE.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{name}:"):
            start = index + 1
            break
    else:
        raise AssertionError(f"{JUSTFILE} has no recipe named {name!r}")

    body: list[str] = []
    for line in lines[start:]:
        if line and not line.startswith((" ", "\t")):
            break
        body.append(line)
    while body and not body[-1].strip():
        body.pop()

    text = textwrap.dedent("\n".join(body))
    if not text.startswith("#!"):
        raise AssertionError(
            f"{name} is not a shebang recipe; this suite executes its body directly"
        )
    return text


class CheckReposRetry(unittest.TestCase):
    def run_recipe(self, exit_codes: list[int]) -> tuple[int, int, int]:
        """Run the recipe with `python3` yielding `exit_codes` in order.

        Returns (recipe exit status, python3 invocations, sleep invocations).
        """
        with tempfile.TemporaryDirectory(prefix="utah-check-repos-") as tmp:
            scratch = Path(tmp)
            stub_dir = scratch / "bin"
            stub_dir.mkdir()
            for name, source in (("python3", STUB_PYTHON3), ("sleep", STUB_SLEEP)):
                stub = stub_dir / name
                stub.write_text(source)
                stub.chmod(0o755)

            calls = scratch / "calls"
            sleeps = scratch / "sleeps"
            codes = scratch / "codes"
            calls.touch()
            sleeps.touch()
            codes.write_text("".join(f"{code}\n" for code in exit_codes))

            script = scratch / "recipe.sh"
            script.write_text(recipe_body(RECIPE))

            result = subprocess.run(
                ["bash", str(script)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={
                    "PATH": f"{stub_dir}:/usr/bin:/bin",
                    "CALLS": str(calls),
                    "SLEEPS": str(sleeps),
                    "CODES": str(codes),
                },
            )
            invoked = calls.read_text().splitlines()
            naps = len(sleeps.read_text().splitlines())

        for line in invoked:
            self.assertIn(
                "scripts/check-repo-availability.py", line,
                "the recipe must retry the availability gate itself",
            )
        return result.returncode, len(invoked), naps

    def test_success_runs_the_gate_exactly_once(self):
        status, invocations, naps = self.run_recipe([0])
        self.assertEqual(status, 0)
        self.assertEqual(invocations, 1)
        self.assertEqual(naps, 0)

    def test_resolution_failure_is_not_retried(self):
        """A real failure must stay immediate: one run, its own exit status."""
        status, invocations, naps = self.run_recipe([1])
        self.assertEqual(status, 1)
        self.assertEqual(invocations, 1, "a resolution failure must not be retried")
        self.assertEqual(naps, 0)

    def test_engine_failure_that_clears_is_retried_to_success(self):
        status, invocations, _ = self.run_recipe([125, 0])
        self.assertEqual(status, 0, "a transient engine failure must not fail the gate")
        self.assertEqual(invocations, 2)

    def test_engine_failure_gives_up_after_three_attempts(self):
        status, invocations, naps = self.run_recipe([125, 125, 125])
        self.assertEqual(status, 125)
        self.assertEqual(invocations, 3, "the gate must bound its retries")
        self.assertEqual(naps, 2, "no backoff is paid after the final attempt")

    def test_a_late_resolution_failure_still_surfaces_its_own_status(self):
        """125 then a real failure reports the real failure, not 125."""
        status, invocations, _ = self.run_recipe([125, 3])
        self.assertEqual(status, 3)
        self.assertEqual(invocations, 2)


if __name__ == "__main__":
    unittest.main()
