"""Hummingbird's unpinned repository must move the package layer's cache key.

The package transaction is cached in the registry and keyed by its COPY'd
inputs. Hummingbird's repository is rolling and unpinned, so without a key
that follows it, a nightly build replays yesterday's transaction from cache
and new Hummingbird packages never reach testing until something unrelated
busts the layer. `just build-ghcr` resolves the repository's publish day and
passes it as `ARG HUMMINGBIRD_REPO_DAY`, declared above the transaction.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = (ROOT / "Containerfile").read_text(encoding="utf-8")
JUSTFILE = (ROOT / "Justfile").read_text(encoding="utf-8")


def recipe(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\b.*?(?=^\S)", JUSTFILE, re.M | re.S)
    if not match:
        raise AssertionError(f"Justfile recipe {name} not found")
    return match.group(0)


class HummingbirdRepoDayTest(unittest.TestCase):
    def test_arg_is_declared_directly_above_the_transaction(self):
        lines = CONTAINERFILE.splitlines()
        arg = lines.index("ARG HUMMINGBIRD_REPO_DAY=unset")
        transaction = next(
            i for i, line in enumerate(lines)
            if "utah-install-packages" in line and i > arg)
        between = lines[arg + 1:transaction]
        self.assertTrue(between[0].startswith("RUN "), between[:1])
        self.assertFalse(
            any(line.startswith(("RUN ", "COPY ", "FROM ")) for line in between[1:]),
            "the ARG must key the package transaction, not an earlier layer")

    def test_arg_is_declared_after_the_inputs_it_does_not_affect(self):
        # Layer discipline: an ARG keys every layer below it, so it must not
        # sit above the script and asset COPYs.
        self.assertLess(
            CONTAINERFILE.index("COPY system_files/shared /tmp/utah-local"),
            CONTAINERFILE.index("ARG HUMMINGBIRD_REPO_DAY"))

    def test_build_ghcr_passes_the_day(self):
        body = recipe("build-ghcr")
        self.assertIn('--build-arg HUMMINGBIRD_REPO_DAY="$hb_revision"', body)
        # The repo URL comes from the repo file, never a second copy of it.
        self.assertIn("packages/hummingbird.repo", body)
        self.assertNotIn("packages.redhat.com", body)


if __name__ == "__main__":
    unittest.main()
