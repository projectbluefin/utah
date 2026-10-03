"""A kernel build must consume exactly the verified cache, or fail before build."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JUST = shutil.which("just")


@unittest.skipUnless(JUST, "just is required to execute the build recipe")
class KernelCacheSigningTests(unittest.TestCase):
    def run_recipe(self, *, verifier: bool = True, valid: bool = True):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tools = base / "bin"
            tools.mkdir()
            for name in ("bash", "python3", "date", "git", "sha256sum", "cut", "cat", "dirname"):
                tools.joinpath(name).symlink_to(shutil.which(name))
            builds = base / "builds"
            verification = base / "verification"
            podman = tools / "podman"
            podman.write_text('#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$BUILDS"\n')
            podman.chmod(0o755)
            skopeo = tools / "skopeo"
            skopeo.write_text(
                '#!/bin/bash\n'
                'if [ "$1" = inspect ]; then\n'
                '  printf \'%s\\n\' \'{"Digest":"sha256:accepted"}\'\n'
                'else exit 1; fi\n')
            skopeo.chmod(0o755)
            if verifier:
                cosign = tools / "cosign"
                cosign.write_text(
                    '#!/bin/bash\nprintf \'%s\\n\' "$*" > "$VERIFICATION"\n'
                    f'exit {0 if valid else 1}\n')
                cosign.chmod(0o755)
            rendered = subprocess.run(
                [JUST, "--justfile", str(ROOT / "Justfile"), "--working-directory",
                 str(ROOT), "--dry-run", "build-ghcr", "utah", "testing", "gaming"],
                check=True, capture_output=True, text=True)
            env = {
                "PATH": str(tools), "HOME": str(base), "GITHUB_ACTIONS": "false",
                "BUILDS": str(builds), "VERIFICATION": str(verification),
            }
            result = subprocess.run(
                ["/bin/bash", "-c", rendered.stdout + rendered.stderr],
                cwd=ROOT, env=env, capture_output=True, text=True)
            return (result, builds.read_text() if builds.exists() else "",
                    verification.read_text() if verification.exists() else "")

    def test_missing_verifier_stops_before_build(self):
        result, builds, _ = self.run_recipe(verifier=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cosign is required", result.stderr)
        self.assertEqual(builds, "")

    def test_untrusted_cache_stops_before_build(self):
        result, builds, verification = self.run_recipe(valid=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("utah-kernel-cache@sha256:accepted", verification)
        self.assertEqual(builds, "")

    def test_build_uses_verified_digest_not_mutable_tag(self):
        result, builds, verification = self.run_recipe()
        self.assertEqual(result.returncode, 0, result.stderr)
        accepted = "ghcr.io/projectbluefin/utah-kernel-cache@sha256:accepted"
        self.assertIn(f"verify {accepted}", verification)
        self.assertIn(f"--build-arg BASE_IMAGE={accepted}", builds)
        self.assertNotIn("BASE_IMAGE=ghcr.io/projectbluefin/utah-kernel-cache:", builds)


if __name__ == "__main__":
    unittest.main()
