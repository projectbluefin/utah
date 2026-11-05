"""Execute the trusted workflow body against a fake gh, never the live API."""
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/update-armed-prs.yml").read_text())
JOB = WORKFLOW["jobs"]["update"]
UPDATE = next(step for step in JOB["steps"] if "run" in step)

STUB_GH = """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["GH_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["api", "graphql"]:
    if os.environ["DISCOVERY_FAIL"] == "1":
        sys.exit(1)
    print(os.environ["GH_PAGES"])
elif args[:3] == ["api", "--method", "PUT"] and args[3].endswith("/update-branch"):
    number = int(args[3].split("/")[-2])
    if number in json.loads(os.environ["GH_FAILURES"]):
        # Even a credential echoed by a failed client must not reach the log/summary.
        print(os.environ["GH_TOKEN"], file=sys.stderr)
        sys.exit(1)
    print('{"message": "Updating pull request branch.", "url": "https://example.invalid"}')
else:
    raise AssertionError(args)
"""


def pull_request(number, **changes):
    pr = {
        "number": number,
        "isDraft": False,
        "mergeStateStatus": "BEHIND",
        "headRefOid": f"{number:040x}",
        "autoMergeRequest": {"enabledAt": "2026-10-31T00:00:00Z"},
        "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
    }
    pr.update(changes)
    return pr


def page(prs):
    return {"data": {"repository": {"pullRequests": {"nodes": prs}}}}


class FreshnessWorkflowBehaviorTests(unittest.TestCase):
    def run_workflow(self, pages, failures=(), discovery_fail=False):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        gh = root / "gh"
        gh.write_text(STUB_GH)
        gh.chmod(0o755)
        calls = root / "calls.jsonl"
        summary = root / "summary.md"
        env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                   GH_TOKEN="private-test-token", REPOSITORY="projectbluefin/example",
                   GITHUB_STEP_SUMMARY=str(summary), GH_CALLS=str(calls),
                   GH_PAGES=json.dumps(pages), GH_FAILURES=json.dumps(list(failures)),
                   DISCOVERY_FAIL="1" if discovery_fail else "0")
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", UPDATE["run"]],
                                cwd=root, env=env, capture_output=True, text=True, check=False)
        recorded = [json.loads(line) for line in calls.read_text().splitlines()]
        report = summary.read_text() if summary.exists() else ""
        return result, recorded, report

    def updates(self, calls):
        return [args for args in calls if args[:3] == ["api", "--method", "PUT"]]

    def test_paginated_discovery_and_head_guarded_merge_updates(self):
        result, calls, summary = self.run_workflow([page([pull_request(1)]), page([pull_request(102)])])
        self.assertEqual(result.returncode, 0, result.stderr)
        discovery = calls[0]
        self.assertEqual(discovery[:4], ["api", "graphql", "--paginate", "--slurp"])
        query = next(arg for arg in discovery if arg.startswith("query="))
        for fragment in ('states: OPEN', 'baseRefName: "main"', 'after: $endCursor',
                         'pageInfo { hasNextPage endCursor }', 'autoMergeRequest { enabledAt }'):
            self.assertIn(fragment, query)
        self.assertIn("owner=projectbluefin", discovery)
        self.assertIn("name=example", discovery)
        self.assertEqual(self.updates(calls), [
            ["api", "--method", "PUT", f"repos/projectbluefin/example/pulls/{number}/update-branch",
             "-f", f"expected_head_sha={number:040x}"] for number in (1, 102)
        ])
        self.assertIn("#1: merge update requested", summary)
        self.assertIn("#102: merge update requested", summary)

    def test_only_armed_non_draft_behind_unheld_prs_are_updated(self):
        prs = [pull_request(1), pull_request(2, autoMergeRequest=None), pull_request(3, isDraft=True)]
        for number, state in enumerate(("DIRTY", "UNKNOWN", "BLOCKED", "CLEAN", "UNSTABLE", "HAS_HOOKS"), 4):
            prs.append(pull_request(number, mergeStateStatus=state))
        for number, label in enumerate(("hold", "do-not-merge", "do-not-merge/hold", "HOLD"), 10):
            prs.append(pull_request(number, labels={
                "nodes": [{"name": label}], "pageInfo": {"hasNextPage": False},
            }))
        result, calls, _ = self.run_workflow([page(prs)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.updates(calls)), 1)
        self.assertIn("/pulls/1/update-branch", self.updates(calls)[0][3])

    def test_per_pr_failures_continue_and_never_print_credentials(self):
        result, calls, summary = self.run_workflow(
            [page([pull_request(1), pull_request(2), pull_request(3)])], failures=[1, 2],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.updates(calls)), 3)
        for number in (1, 2):
            self.assertIn(f"#{number}: update failed", summary)
        self.assertIn("#3: merge update requested", summary)
        self.assertNotIn("private-test-token", summary + result.stdout + result.stderr)

    def test_incomplete_labels_fail_closed(self):
        pr = copy.deepcopy(pull_request(1))
        pr["labels"]["pageInfo"]["hasNextPage"] = True
        result, calls, summary = self.run_workflow([page([pr])])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.updates(calls), [])
        self.assertIn("#1: skipped", summary)

    def test_no_eligible_prs_reports_a_successful_noop(self):
        for pages in ([page([])], [page([pull_request(1, mergeStateStatus="UNKNOWN")])]):
            with self.subTest(pages=pages):
                result, calls, summary = self.run_workflow(pages)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.updates(calls), [])
                self.assertIn("No eligible armed BEHIND", summary)

    def test_discovery_errors_fail_instead_of_claiming_there_are_no_prs(self):
        result, calls, summary = self.run_workflow([], discovery_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.updates(calls), [])
        self.assertNotIn("No eligible", summary)


if __name__ == "__main__":
    unittest.main()
