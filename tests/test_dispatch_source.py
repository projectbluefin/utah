"""Exercise the dispatch guard with stale and divergent source histories."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DispatchSourceTests(unittest.TestCase):
    def run_guard(self, status="ahead", target="head", api_exit=0):
        source = (ROOT / ".github/workflows/build.yml").read_text()
        guard = source.split("      - name: Assert dispatched SHA", 1)[1]
        guard = guard.split("        run: |\n", 1)[1].split("      - name: Install just", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / "gh"
            gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$CALL_LOG"\nprintf "%s\\n" "$COMPARE_STATUS"\nexit "$API_EXIT"\n')
            gh.chmod(0o755)
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                   "REPO": "projectbluefin/utah", "GITHUB_SHA": "head",
                   "TARGET_SHA": target, "SOURCE_SHA": "source", "GH_TOKEN": "test",
                   "COMPARE_STATUS": status, "API_EXIT": str(api_exit),
                   "CALL_LOG": str(root / "call")}
            result = subprocess.run(["bash", "-c", textwrap.dedent(guard)],
                                    capture_output=True, text=True, env=env)
            call = (root / "call").read_text() if (root / "call").exists() else ""
        return result, call

    def test_compares_source_to_head_rather_than_head_to_itself(self):
        result, call = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("compare/source...head", call)
        self.assertNotIn("compare/head...head", call)

    def test_stale_or_divergent_source_and_api_failure_are_rejected(self):
        for status, api_exit in [("behind", 0), ("diverged", 0), ("ahead", 1)]:
            with self.subTest(status=status, api_exit=api_exit):
                result, _ = self.run_guard(status=status, api_exit=api_exit)
                self.assertNotEqual(result.returncode, 0)

    def test_changed_target_is_rejected_before_comparing_history(self):
        result, call = self.run_guard(target="stale")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(call, "")
