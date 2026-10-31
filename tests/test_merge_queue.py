"""The required checks report in the merge queue, and the queue cannot publish.

The `main` ruleset requires `contract` and `build_main / Check all testing
builds successful`. A merge queue waits for both on its own `merge_group`
commit and drops the entry when one never reports. The second name normally
comes from build.yml's call to the pinned projectbluefin/actions reusable
build, which pushes, signs, attests and opens issues for every event other
than pull_request -- so on a merge group build.yml runs only `contract`, and
merge-queue.yml reports the same name from a local build that publishes
nothing. These tests hold all of that.
"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
BUILD = yaml.safe_load((WORKFLOWS / "build.yml").read_text())
QUEUE = yaml.safe_load((WORKFLOWS / "merge-queue.yml").read_text())
GUARD = "github.event_name != 'merge_group'"
CHECK = "build_main / Check all testing builds successful"
# Anything that writes outside the runner: registry pushes and signatures,
# attestations, the layer cache, issues, pull requests, dispatches.
WRITES = re.compile(
    r"podman push|docker push|skopeo copy|cosign sign|attest|--cache-to|"
    r"REGISTRY_CACHE_WRITE: ?\"?1|gh (issue|pr|workflow|api|release)|git push"
)


def triggers(workflow):
    return workflow.get("on", workflow.get(True))


def needs(job):
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else list(value)


def runs_on_merge_group(jobs, name):
    """Whether a job can run on a merge_group event, from its `if` and needs."""
    job = jobs[name]
    condition = str(job.get("if", "")).removeprefix("${{ ").strip()
    if condition.startswith(GUARD + " &&") or condition == GUARD:
        return False
    if "github.event_name == 'workflow_dispatch'" in condition and "||" not in condition:
        return False
    if "always()" not in condition and "cancelled()" not in condition:
        return all(runs_on_merge_group(jobs, need) for need in needs(job))
    return not any(
        f"needs.{need}.result == 'success'" in condition and not runs_on_merge_group(jobs, need)
        for need in needs(job)
    )


def write_scopes(permissions):
    if permissions is None or permissions == "write-all":
        return {"<inherited or write-all>"}
    if permissions in ({}, "read-all"):
        return set()
    return {scope for scope, level in permissions.items() if level != "read"}


class RequiredChecksReportInTheQueueTests(unittest.TestCase):
    def test_build_runs_contract_on_merge_group(self):
        self.assertIn("merge_group", triggers(BUILD))
        self.assertIsNone(triggers(BUILD)["merge_group"], "no types or filters: every group")
        self.assertNotIn("name", BUILD["jobs"]["contract"], "the check is named after the job id")
        self.assertTrue(runs_on_merge_group(BUILD["jobs"], "contract"))

    def test_the_queue_check_has_exactly_the_reusable_builds_name(self):
        # The reusable aggregate is "Check all <stream_name> builds successful",
        # reported under the calling job's id.
        stream = BUILD["jobs"]["build_main"]["with"]["stream_name"]
        self.assertEqual(f"build_main / Check all {stream} builds successful", CHECK)
        self.assertEqual(QUEUE["jobs"]["check"]["name"], CHECK)

    def test_exactly_one_merge_group_job_reports_the_build_check_and_no_pull_request_job_does(self):
        on_queue, on_pr = [], []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            workflow = yaml.safe_load(path.read_text())
            for job_id, job in (workflow.get("jobs") or {}).items():
                if job.get("name") == CHECK:
                    if "merge_group" in triggers(workflow):
                        on_queue.append((path.name, job_id))
                    # A skipped job still reports its name; on a pull request
                    # it would sit next to the real check.
                    if "pull_request" in triggers(workflow):
                        on_pr.append((path.name, job_id))
        self.assertEqual(on_queue, [("merge-queue.yml", "check")])
        self.assertEqual(on_pr, [])

    def test_the_queue_check_fails_unless_every_image_built(self):
        check = QUEUE["jobs"]["check"]
        self.assertEqual(check["if"], "always()")
        self.assertEqual(set(check["needs"]), set(QUEUE["jobs"]) - {"check"})
        script = check["steps"][-1]["run"]
        for results, expected in (
            ({"flavors": "success", "image": "success"}, 0),
            ({"flavors": "success", "image": "failure"}, 1),
            ({"flavors": "failure", "image": "skipped"}, 1),
            ({"flavors": "success", "image": "cancelled"}, 1),
        ):
            with self.subTest(results=results):
                jobs = {name: {"result": result} for name, result in results.items()}
                done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                      env={**os.environ, "JOBS": json.dumps(jobs)})
                self.assertEqual(done.returncode, expected, done.stdout + done.stderr)


class TheQueueCannotPublishTests(unittest.TestCase):
    def test_build_runs_nothing_but_contract_on_merge_group(self):
        jobs = BUILD["jobs"]
        self.assertEqual({name for name in jobs if runs_on_merge_group(jobs, name)}, {"contract"})
        self.assertEqual(write_scopes(jobs["contract"]["permissions"]), set())
        for name in ("kernel_cache", "build_main", "build_kernel"):
            with self.subTest(job=name):
                self.assertIn(GUARD, str(jobs[name]["if"]))

    def test_merge_queue_runs_only_on_merge_group_with_read_only_tokens(self):
        self.assertEqual(triggers(QUEUE), {"merge_group": None})
        self.assertEqual(QUEUE["permissions"], {})
        for name, job in QUEUE["jobs"].items():
            with self.subTest(job=name):
                self.assertEqual(write_scopes(job["permissions"]), set())
                self.assertNotIn("uses", job, "no reusable workflow: they publish on non-PR events")
        self.assertEqual(QUEUE["jobs"]["image"]["permissions"], {"contents": "read", "packages": "read"})

    def test_merge_queue_has_no_step_that_writes_anywhere(self):
        text = (WORKFLOWS / "merge-queue.yml").read_text()
        steps = [step for job in QUEUE["jobs"].values() for step in job["steps"]]
        for step in steps:
            with self.subTest(step=step.get("name", step.get("uses"))):
                self.assertIsNone(WRITES.search(step.get("run", "")))
                uses = step.get("uses", "")
                if uses:
                    self.assertRegex(uses, r"^(actions/checkout|extractions/setup-just)@[0-9a-f]{40}$")
                    self.assertNotEqual(step.get("with", {}).get("persist-credentials"), True)
        build = next(s for s in QUEUE["jobs"]["image"]["steps"] if "build-ghcr" in s.get("run", ""))
        self.assertEqual(build["env"]["REGISTRY_CACHE_WRITE"], "0")
        self.assertEqual(build["env"]["ENABLE_SSHD"], "0")
        self.assertNotIn("secrets.", text)

    def test_the_local_recipe_writes_the_layer_cache_only_when_asked(self):
        # The queue relies on REGISTRY_CACHE_WRITE=0; the read-only token is
        # the second line of defence if this recipe ever changed.
        recipe = (ROOT / "Justfile").read_text().split("\nbuild-ghcr ", 1)[1].split("\n\n# ", 1)[0]
        self.assertIn('if [ "${REGISTRY_CACHE_WRITE:-0}" = "1" ]; then', recipe)
        self.assertEqual(recipe.count("layer_cache_args+=(--cache-to"), 1)
        self.assertRegex(recipe, r'if \[ "\$\{REGISTRY_CACHE_WRITE:-0\}" = "1" \]; then\n\s*'
                                 r'layer_cache_args\+=\(--cache-to')
        self.assertNotRegex(recipe, r"podman push|cosign sign|skopeo copy")


if __name__ == "__main__":
    unittest.main()
