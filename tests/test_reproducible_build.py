"""scripts/check-reproducible-build.sh must actually compare two builds.

utah#313 asks for an outcome -- a rebuild that changes nothing produces an
identical image -- and the clean-stage unit tests cannot establish it: they run
the script against a scratch tree and assert on mtimes. The acceptance test is
two builds and a layer diff, which is what this script does and what these tests
pin, with podman stubbed so the assertions cost seconds instead of two
uncached image builds.

The two things that would make a passing run meaningless are a cached second
build (which replays the first build's layers whatever the Containerfile does)
and build args that differ between the runs for reasons unrelated to residue
(VERSION carries a date and the short SHA). Both are asserted here.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-reproducible-build.sh"

PODMAN_STUB = """#!/usr/bin/env bash
# Stands in for podman. Records every build invocation and answers `image
# inspect` from a file per tag, so a run costs no image build at all.
set -eu
case "$1" in
    --version)
        echo "podman version ${PODMAN_VERSION:-5.6.0}"
        ;;
    build)
        echo "$*" >> "$BUILD_LOG"
        ;;
    image)
        tag="${@: -1}"
        case "$tag" in
            *utah-repro-a*) cat "$LAYERS_A" ;;
            *) cat "$LAYERS_B" ;;
        esac
        ;;
    *)
        echo "unexpected podman subcommand: $1" >&2
        exit 64
        ;;
esac
"""

IDENTICAL = "sha256:aaa\nsha256:bbb\nsha256:ccc\n"
CHURNED = "sha256:aaa\nsha256:bbb\nsha256:zzz\n"


class ReproducibilityCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.podman = self.tmp / "podman"
        self.podman.write_text(PODMAN_STUB)
        self.podman.chmod(0o755)
        self.build_log = self.tmp / "builds"
        self.build_log.write_text("")
        self.skopeo = self.tmp / "skopeo"
        self.skopeo.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"Digest":"sha256:verified-cache"}\'\n')
        self.skopeo.chmod(0o755)
        self.cosign = self.tmp / "cosign"
        self.cosign.write_text('#!/bin/sh\nexit "${VERIFY_STATUS:-0}"\n')
        self.cosign.chmod(0o755)

    def run_check(self, first: str, second: str, flavor: str = "main", verify_status: int = 0,
                  podman_version: str = "5.6.0"):
        layers_a = self.tmp / "a"
        layers_b = self.tmp / "b"
        layers_a.write_text(first)
        layers_b.write_text(second)
        env = dict(os.environ)
        env.update({
            "PODMAN": str(self.podman),
            "BUILD_LOG": str(self.build_log),
            "LAYERS_A": str(layers_a),
            "LAYERS_B": str(layers_b),
            "SKOPEO": str(self.skopeo),
            "COSIGN": str(self.cosign),
            "VERIFY_STATUS": str(verify_status),
            "PODMAN_VERSION": podman_version,
        })
        return subprocess.run(
            ["bash", str(SCRIPT), flavor],
            cwd=ROOT, env=env, capture_output=True, text=True)

    def builds(self) -> list[str]:
        return [line for line in self.build_log.read_text().splitlines() if line]

    def test_identical_layers_pass(self):
        result = self.run_check(IDENTICAL, IDENTICAL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reproducible", result.stdout)

    def test_a_churned_layer_fails_with_a_diff(self):
        """The failure has to name what moved, or it cannot be acted on."""
        result = self.run_check(IDENTICAL, CHURNED)
        self.assertEqual(result.returncode, 1)
        self.assertIn("NOT reproducible", result.stderr)
        self.assertIn("sha256:ccc", result.stderr)
        self.assertIn("sha256:zzz", result.stderr)

    def test_both_builds_are_uncached(self):
        """Without --no-cache the second build replays the first's layers and
        the comparison passes no matter what the build scripts leave behind."""
        self.run_check(IDENTICAL, IDENTICAL)
        builds = self.builds()
        self.assertEqual(len(builds), 2, f"expected two builds, got: {builds}")
        for invocation in builds:
            self.assertIn("--no-cache", invocation)

    def test_the_wall_clock_build_args_are_held_constant(self):
        """VERSION normally carries `date -u +%Y%m%d` and the short SHA, which
        land in a label: left to vary they would fail the comparison for a
        reason that is not build residue."""
        self.run_check(IDENTICAL, IDENTICAL)
        first, second = self.builds()
        for arg in ("VERSION=", "SHA_HEAD_SHORT=", "IMAGE_FLAVOR="):
            values = []
            for invocation in (first, second):
                token = next(t for t in invocation.split() if t.startswith(arg))
                values.append(token)
            self.assertEqual(values[0], values[1], f"{arg} differed between builds")

    def test_supported_podman_fixes_the_config_timestamp(self):
        result = self.run_check(IDENTICAL, IDENTICAL)
        self.assertEqual(result.returncode, 0, result.stderr)
        for invocation in self.builds():
            epoch = subprocess.check_output(["git", "log", "-1", "--format=%ct", "--", "Containerfile", "packages", "system_files", "scripts", "config", ".gitmodules"], cwd=ROOT, text=True).strip()
            self.assertIn(f"--source-date-epoch {epoch}", invocation)
            self.assertIn("--rewrite-timestamp", invocation)
            self.assertIn("UTAH_REWRITE_TIMESTAMPS=1", invocation)

    def test_old_podman_warns_and_continues_without_the_flag(self):
        """Podman < 5.5 does not know --source-date-epoch; the probe must
        warn and build, like the build recipes, not abort on the flag."""
        result = self.run_check(IDENTICAL, IDENTICAL, podman_version="5.4.2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("< 5.5", result.stderr)
        builds = self.builds()
        self.assertEqual(len(builds), 2)
        for invocation in builds:
            self.assertNotIn("--source-date-epoch", invocation)

    def test_the_flavor_reaches_both_builds(self):
        self.run_check(IDENTICAL, IDENTICAL, flavor="nvidia-gaming")
        for invocation in self.builds():
            self.assertIn("--build-arg IMAGE_FLAVOR=nvidia-gaming", invocation)

    def test_kernel_flavors_build_only_the_verified_immutable_cache(self):
        result = self.run_check(IDENTICAL, IDENTICAL, flavor="nvidia")
        self.assertEqual(result.returncode, 0, result.stderr)
        for invocation in self.builds():
            self.assertIn("BASE_IMAGE=ghcr.io/projectbluefin/utah-kernel-cache@sha256:verified-cache", invocation)

    def test_invalid_cache_signature_prevents_both_builds(self):
        result = self.run_check(IDENTICAL, IDENTICAL, flavor="gaming", verify_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.builds(), [])

    def test_an_unknown_flavor_is_rejected_before_building(self):
        result = self.run_check(IDENTICAL, IDENTICAL, flavor="desktop")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown flavor", result.stderr)
        self.assertEqual(self.builds(), [], "nothing should have been built")


if __name__ == "__main__":
    unittest.main()
