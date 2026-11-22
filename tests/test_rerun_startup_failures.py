"""rerun-startup-failures.py re-dispatches only runs GitHub never started.

A false positive re-dispatches over a real failure (hiding it) or duplicates a
run someone already restarted; a false negative leaves :testing frozen until
the freshness alarm. The tests drive the sweep against a fake GitHub with the
run shapes the API returns, and pin the names the dispatches depend on to the
workflows that define them.
"""
import importlib.util
import re
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rerun_startup_failures", ROOT / "scripts/rerun-startup-failures.py")
sweeper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweeper)

SYNC, BUILD, E2E = (t.workflow for t in sweeper.TARGETS)


def run(run_id, conclusion="startup_failure", created="2026-11-22T12:00:00Z", status="completed"):
    return {"id": run_id, "status": status, "conclusion": conclusion, "created_at": created}


class FakeGitHub:
    def __init__(self, runs, jobs=None, source_build=111):
        self._runs = runs
        self._jobs = jobs or {}
        self._source_build = source_build
        self.dispatched = []

    def runs(self, workflow, ref):
        return self._runs.get(workflow, [])

    def job_count(self, run_id):
        return self._jobs.get(run_id, 0)

    def branch_sha(self, branch):
        return "f" * 40

    def e2e_source_build(self, before):
        return self._source_build

    def dispatch(self, workflow, ref, inputs):
        self.dispatched.append((workflow, ref, inputs))


def sweep(api):
    return sweeper.sweep(api, sweeper.TARGETS, log=lambda _line: None)


class SweepTests(unittest.TestCase):
    def test_a_startup_failure_is_redispatched_the_way_the_chain_dispatches_it(self):
        for wf, ref, inputs in ((SYNC, "main", {}), (BUILD, "testing", {"sha": "f" * 40}),
                                (E2E, "main", {"build_run_id": "111"})):
            with self.subTest(wf):
                api = FakeGitHub({wf: [run(2), run(1, "success", "2026-11-22T10:00:00Z")]})
                report = sweep(api)
                self.assertEqual(api.dispatched, [(wf, ref, inputs)])
                self.assertEqual(report.dispatched, [wf])

    def test_the_older_failure_shape_counts_when_it_has_no_jobs(self):
        api = FakeGitHub({SYNC: [run(1, "failure")]})
        sweep(api)
        self.assertEqual(api.dispatched, [(SYNC, "main", {})])

    def test_a_run_with_failed_jobs_stays_red(self):
        api = FakeGitHub({BUILD: [run(2, "failure")]}, jobs={2: 7})
        report = sweep(api)
        self.assertEqual(api.dispatched, [])
        self.assertEqual(report.failed, [])

    def test_a_startup_failure_with_a_newer_run_is_left_alone(self):
        # The newer run is the previous sweep's dispatch, or a manual re-run.
        later = "2026-11-22T13:00:00Z"
        for newer in (run(2, None, later, "in_progress"), run(2, "success", later), run(2, "failure", later)):
            with self.subTest(newer["conclusion"] or newer["status"]):
                api = FakeGitHub({BUILD: [newer, run(1)]}, jobs={2: 3})
                sweep(api)
                self.assertEqual(api.dispatched, [])

    def test_the_newest_run_is_chosen_by_creation_time_not_list_order(self):
        api = FakeGitHub({BUILD: [run(1), run(2, "success", "2026-11-22T13:00:00Z")]})
        sweep(api)
        self.assertEqual(api.dispatched, [])

    def test_only_the_most_upstream_lost_workflow_is_dispatched(self):
        # Sync starts build and build starts E2E: dispatching those too would run them twice.
        api = FakeGitHub({SYNC: [run(1)], BUILD: [run(2)], E2E: [run(3)]})
        report = sweep(api)
        self.assertEqual(api.dispatched, [(SYNC, "main", {})])
        self.assertEqual(report.covered, [BUILD, E2E])

    def test_an_e2e_run_with_no_source_build_fails_the_sweep(self):
        api = FakeGitHub({E2E: [run(3)]}, source_build=None)
        report = sweep(api)
        self.assertEqual(report.failed, [E2E])
        self.assertEqual(api.dispatched, [])

    def test_an_api_error_on_one_workflow_does_not_stop_the_sweep(self):
        class Broken(FakeGitHub):
            def runs(self, workflow, ref):
                if workflow == SYNC:
                    raise sweeper.GhError("HTTP 502")
                return super().runs(workflow, ref)

        api = Broken({BUILD: [run(2)]})
        report = sweep(api)
        self.assertEqual(report.failed, [SYNC])
        self.assertEqual([d[0] for d in api.dispatched], [BUILD])


class SourceBuildTests(unittest.TestCase):
    def test_skips_a_build_that_did_not_start_e2e(self):
        # Run 9 was a contract_only dispatch: green, but dispatch-iso was skipped.
        jobs = {9: [{"name": "dispatch-iso", "conclusion": "skipped"}],
                8: [{"name": "contract", "conclusion": "success"},
                    {"name": "dispatch-iso", "conclusion": "success"}]}
        self.assertEqual(sweeper.source_build([{"id": 9}, {"id": 8}], jobs.__getitem__), 8)

    def test_none_when_no_build_started_e2e(self):
        self.assertIsNone(sweeper.source_build([{"id": 9}], lambda _id: []))

    def test_a_failed_build_whose_dispatch_iso_succeeded_is_the_source(self):
        # Build 9 failed only in review_vm, which runs beside dispatch-iso; it still
        # started the lost E2E, so the older green build 8 must not be chosen.
        builds = [{"id": 9, "conclusion": "failure"}, {"id": 8, "conclusion": "success"}]
        jobs = {9: [{"name": "review_vm", "conclusion": "failure"},
                    {"name": "dispatch-iso", "conclusion": "success"}],
                8: [{"name": "dispatch-iso", "conclusion": "success"}]}

        def fake_gh(*args):
            url = args[1]
            if url.endswith("/jobs?per_page=100"):
                return {"jobs": jobs[int(url.split("/")[-2])]}
            listed = builds if "status=success" not in url else [
                b for b in builds if b["conclusion"] == "success"]
            return [{"id": b["id"]} for b in listed]

        with mock.patch.object(sweeper, "gh", fake_gh):
            self.assertEqual(sweeper.GitHub("o/r").e2e_source_build("2026-01-01T00:00:00Z"), 9)


class DispatchContractTests(unittest.TestCase):
    """The refs, inputs and job name the sweep uses must match the workflows.

    A renamed input makes every re-dispatch fail with HTTP 422; a renamed job
    makes every lost E2E run unrecoverable.
    """

    def workflow(self, name):
        return (ROOT / ".github/workflows" / name).read_text()

    def test_targets_dispatch_on_the_refs_the_chain_uses(self):
        self.assertIn("gh workflow run build.yml --repo \"$REPOSITORY\" --ref testing", self.workflow(SYNC))
        self.assertIn("gh workflow run post-testing-e2e.yml --repo \"$REPOSITORY\" --ref main",
                      self.workflow(BUILD))
        refs = {t.workflow: t.ref for t in sweeper.TARGETS}
        self.assertEqual(refs, {SYNC: "main", BUILD: "testing", E2E: "main"})

    def test_inputs_and_dispatch_job_exist(self):
        build = self.workflow(BUILD)
        self.assertRegex(build, re.compile(r"^      sha:$", re.M))
        self.assertIn(f"\n  {sweeper.E2E_DISPATCH_JOB}:\n", build)
        self.assertRegex(self.workflow(E2E), re.compile(r"^      build_run_id:$", re.M))
        self.assertIn("workflow_dispatch:", self.workflow(SYNC))


if __name__ == "__main__":
    unittest.main()
