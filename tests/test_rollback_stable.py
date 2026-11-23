"""Rollback preflight never writes and binds signatures and evidence to digests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rollback", ROOT / "scripts/rollback-stable.py")
rollback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollback)
ratchet = rollback.ratchet
DIGEST = "sha256:" + "a" * 64
OLD = "sha256:" + "b" * 64
SHA = "c" * 40
TARGET = "testing-20261122-ccccccc"


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.registry = Mock()
        self.registry.digest.side_effect = lambda ref: OLD if ref.endswith(":stable") else DIGEST
        self.registry.labels.return_value = {ratchet.VERSION_LABEL: TARGET}
        self.github = Mock()
        self.github.full_sha.return_value = SHA
        self.github.statuses.return_value = [
            {"context": f"{context}/{image}", "state": "success", "description": DIGEST,
             "creator": ratchet.ACTIONS_BOT}
            for context in (ratchet.EVIDENCE_CONTEXT, ratchet.TESTSUITE_CONTEXT)
            for image in ("first", "second")
        ]
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))

    def prepare(self, target=TARGET):
        return rollback.prepare(target, [{"image": "first"}, {"image": "second"}],
                                self.registry, self.github, self.runner)

    def test_preview_every_flavor_and_verify_only_digest(self):
        for target in (TARGET, DIGEST):
            with self.subTest(target=target):
                plan = self.prepare(target)
                self.assertEqual([p["image"] for p in plan], ["first", "second"])
                self.assertTrue(all(p["from"] == OLD and p["to"] == DIGEST for p in plan))
                for call in self.runner.call_args_list:
                    self.assertEqual(call.args[0][:2], ["cosign", "verify"])
                    self.assertTrue(call.args[0][-1].endswith("@" + DIGEST))
                for call in self.registry.labels.call_args_list:
                    self.assertTrue(call.args[0].endswith("@" + DIGEST))

    def test_signature_failure_refuses(self):
        self.runner.return_value.returncode = 1
        with self.assertRaisesRegex(ratchet.PromotionError, "signature"):
            self.prepare()
        self.github.statuses.assert_not_called()

    def test_evidence_fails_closed(self):
        for field, value in (("state", "failure"), ("description", OLD), ("creator", "someone")):
            with self.subTest(field=field):
                statuses = self.github.statuses.return_value
                saved = statuses[0][field]
                statuses[0][field] = value
                with self.assertRaisesRegex(ratchet.PromotionError, "no green E2E"):
                    self.prepare()
                statuses[0][field] = saved
        self.github.statuses.return_value = self.github.statuses.return_value[:2]
        with self.assertRaises(ratchet.PromotionError):
            self.prepare()

    def test_missing_target_and_bad_input(self):
        for target in ("stable", "testing; echo unsafe", "sha256:bad", ""):
            with self.assertRaises(ratchet.PromotionError):
                self.prepare(target)
        self.registry.digest.return_value = None
        self.registry.digest.side_effect = None
        with self.assertRaisesRegex(ratchet.PromotionError, "does not exist"):
            self.prepare()

    def test_live_copy_uses_pinned_digests_and_checks_destinations(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/rollback-stable.yml").read_text())
        body = next(step["run"] for step in workflow["jobs"]["rollback"]["steps"]
                    if step.get("name") == "Copy verified digests and check stable")
        plan = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skopeo = root / "skopeo"
            skopeo.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['FAKE_REGISTRY'])
state = json.loads((root / 'state').read_text())
with (root / 'calls').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'inspect':
    print(state[sys.argv[-1]])
else:
    assert sys.argv[1:4] == ['copy', '--preserve-digests', '--all']
    state[sys.argv[-1]] = sys.argv[-2].split('@')[1]
    (root / 'state').write_text(json.dumps(state))
''')
            skopeo.chmod(0o755)
            state = {f"docker://ghcr.io/projectbluefin/{p['image']}:stable": OLD for p in plan}
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                   "FAKE_REGISTRY": directory, "PLAN": json.dumps(plan)}
            (root / "state").write_text(json.dumps(state))
            result = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
            self.assertEqual([call[0] for call in calls], ["inspect"] * 2 + ["copy"] * 2 + ["inspect"] * 2)
            self.assertTrue(all(value == DIGEST for value in json.loads((root / 'state').read_text()).values()))
            # A stable tag changed after preflight: no copy is allowed.
            (root / "calls").write_text("")
            result = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('"copy"', (root / "calls").read_text())

    def test_workflow_safety_contract(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/rollback-stable.yml").read_text())
        release = yaml.safe_load((ROOT / ".github/workflows/execute-release.yml").read_text())
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(list(trigger), ["workflow_dispatch"])
        self.assertIs(trigger["workflow_dispatch"]["inputs"]["dry_run"]["default"], True)
        self.assertEqual(workflow["concurrency"], release["concurrency"])
        self.assertFalse(any(value == "write" for value in workflow["jobs"]["verify"]["permissions"].values()))
        apply = workflow["jobs"]["rollback"]
        self.assertIn("!inputs.dry_run", apply["if"])
        self.assertIn("refs/heads/main", apply["if"])
        body = "\n".join(step.get("run", "") for step in apply["steps"])
        self.assertIn("--preserve-digests --all", body)
        self.assertIn('${image}@${digest}', body)
        self.assertIn('--prerelease --latest=false', body)
        self.assertLess(body.index('"$now" = "$before"'), body.index('skopeo copy'))
        self.assertLess(body.index('skopeo copy'), body.index('"$now" = "$digest"'))


if __name__ == "__main__":
    unittest.main()
