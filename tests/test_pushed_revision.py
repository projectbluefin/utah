"""The image on ghcr.io must label the commit that built it (#1468).

scripts/verify-pushed-revision.py reads org.opencontainers.image.revision back
from the registry, by the digests the build pushed, and fails on any mismatch.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-pushed-revision.py"

spec = importlib.util.spec_from_file_location("verify_pushed_revision", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

SHA = "c" * 40
OTHER = "d" * 40
MAIN = "sha256:" + "a" * 64
KERNEL = "sha256:" + "b" * 64
MAIN_DIGESTS = json.dumps({"utah": {"amd64": MAIN}})
KERNEL_DIGESTS = json.dumps({"utah-nvidia": {"amd64": KERNEL}})


class PushedRefsTests(unittest.TestCase):
    def test_both_build_calls_resolve_to_immutable_refs(self):
        refs = gate.pushed_refs([MAIN_DIGESTS, KERNEL_DIGESTS], "projectbluefin")
        self.assertEqual(refs, [f"ghcr.io/projectbluefin/utah-nvidia@{KERNEL}",
                                f"ghcr.io/projectbluefin/utah@{MAIN}"])

    def test_skipped_call_is_ignored(self):
        refs = gate.pushed_refs([MAIN_DIGESTS, ""], "projectbluefin")
        self.assertEqual(refs, [f"ghcr.io/projectbluefin/utah@{MAIN}"])

    def test_nothing_pushed_is_refused(self):
        for maps in (["", ""], ["{}", ""], ['{"utah": {}}']):
            with self.assertRaises(ValueError, msg=maps):
                gate.pushed_refs(maps, "projectbluefin")

    def test_tag_or_malformed_digest_is_refused(self):
        for digest in ("testing", "sha256:abc", "sha512:" + "a" * 128):
            with self.assertRaises(ValueError, msg=digest):
                gate.pushed_refs([json.dumps({"utah": {"amd64": digest}})], "projectbluefin")


class MismatchTests(unittest.TestCase):
    def test_matching_labels_pass(self):
        refs = gate.pushed_refs([MAIN_DIGESTS, KERNEL_DIGESTS], "projectbluefin")
        seen = []

        def labels(ref):
            seen.append(ref)
            return {gate.LABEL: SHA}

        self.assertEqual(gate.mismatches(refs, SHA, labels), [])
        self.assertEqual(seen, refs)

    def test_one_wrong_or_missing_label_fails_naming_the_image(self):
        refs = gate.pushed_refs([MAIN_DIGESTS, KERNEL_DIGESTS], "projectbluefin")
        for label in ({gate.LABEL: OTHER}, {}):
            problems = gate.mismatches(
                refs, SHA, lambda ref, label=label: label if KERNEL in ref else {gate.LABEL: SHA})
            self.assertEqual(len(problems), 1, label)
            self.assertIn(f"utah-nvidia@{KERNEL}", problems[0])
            self.assertIn(SHA, problems[0])


class CommandLineTests(unittest.TestCase):
    """Runs the script against a fake skopeo to pin the registry query."""

    def run_script(self, revision, *args):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            log = tools / "calls"
            skopeo = tools / "skopeo"
            skopeo.write_text(
                "#!/bin/sh\n"
                f'echo "$*" >> "{log}"\n'
                f"printf '%s\\n' '{json.dumps({'Labels': {gate.LABEL: revision}})}'\n")
            skopeo.chmod(0o755)
            env = dict(os.environ, PATH=f"{tools}{os.pathsep}{os.environ['PATH']}")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), *args],
                env=env, capture_output=True, text=True)
            calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    def test_reads_each_pushed_digest_from_the_registry(self):
        result, calls = self.run_script(SHA, SHA, "ProjectBluefin", MAIN_DIGESTS, "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [f"inspect --no-tags docker://ghcr.io/projectbluefin/utah@{MAIN}"])

    def test_mismatch_fails_the_job(self):
        result, _ = self.run_script(OTHER, SHA, "projectbluefin", MAIN_DIGESTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pushed image revision mismatch", result.stderr)

    def test_short_expected_sha_is_refused_before_any_query(self):
        result, calls = self.run_script(SHA, SHA[:7], "projectbluefin", MAIN_DIGESTS)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
