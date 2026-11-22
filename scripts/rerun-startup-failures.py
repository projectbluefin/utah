#!/usr/bin/env python3
"""Re-dispatch the workflows that move `:testing` when GitHub never started them.

A startup failure is a run GitHub refused to begin: it is created already
completed, with zero jobs and conclusion `startup_failure` (`failure` on older
runs). A deleted `gh-readonly-queue/*` ref or a workflow file that does not
parse causes one. `rerun-e2e-infra-failures.yml` cannot recover it: GitHub
sends no `workflow_run` event for a run that never started, and there is no
failed job to classify. So `:testing` silently stops moving until
`testing-freshness.yml` raises its alarm 36 hours later.

Recovery therefore has to be polled, not evented. `rerun-startup-failures.yml`
runs this every three hours. Ported from tuna-os/tunaOS
`scripts/rerun-startup-failures.py`:

- A workflow is re-dispatched only while its newest run on the ref below is a
  startup failure. The dispatch then becomes the newest run, so the next sweep
  leaves it alone, and a run someone already restarted by hand is never
  duplicated. Nothing is stored between sweeps.
- A run with any job is a real failure and stays red.
- Only the most upstream lost workflow is dispatched. Each workflow in the
  chain starts the next one, so dispatching a downstream one as well would
  run the same build and E2E twice. The lost downstream run is reported as
  covered; the chain replaces it with a newer run.

Each workflow is dispatched the way the chain itself dispatches it:
sync-main-to-testing on `main` with no inputs; build on `testing` with the
current `testing` SHA, as sync-main-to-testing does; post-testing-e2e on `main`
with the build run it should have validated. The API does not return a run's
inputs, so that build run is reconstructed: the newest `testing` build created
before the lost E2E run whose `dispatch-iso` job (the job that starts E2E)
succeeded, whatever the build's overall conclusion. candidate-common is not
swept: its `common_sha` input comes from projectbluefin/common and cannot be
recovered from a lost run.

Usage:
  rerun-startup-failures.py [--dry-run]

Environment: GH_TOKEN (actions: write), GITHUB_REPOSITORY.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Callable, NamedTuple

# `startup_failure` is what the API reports now; `failure` with zero jobs is
# the older shape tunaOS recorded. The zero-job check decides either way.
STARTUP_CONCLUSIONS = {"startup_failure", "failure"}
BUILD_WORKFLOW = "build.yml"
E2E_DISPATCH_JOB = "dispatch-iso"


class GhError(RuntimeError):
    pass


class Unrecoverable(RuntimeError):
    """The lost run's dispatch inputs cannot be reconstructed."""


def gh_text(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def gh(*args: str):
    out = gh_text(*args)
    return json.loads(out) if out.strip() else None


def source_build(build_runs: list[dict], jobs_of: Callable[[int], list[dict]]) -> int | None:
    """The first of `build_runs` (newest first) whose dispatch-iso job started E2E.

    A successful build that skipped dispatch-iso (a contract_only dispatch)
    published no digests for E2E to resolve, so it does not count.
    """
    for run in build_runs:
        if any(job.get("name") == E2E_DISPATCH_JOB and job.get("conclusion") == "success"
               for job in jobs_of(run["id"])):
            return run["id"]
    return None


class GitHub:
    def __init__(self, repo: str):
        self.repo = repo

    def runs(self, workflow: str, ref: str) -> list[dict]:
        return gh("api", f"repos/{self.repo}/actions/workflows/{workflow}/runs?branch={ref}&per_page=5",
                  "--jq", "[.workflow_runs[] | {id, status, conclusion, created_at, html_url}]")

    def job_count(self, run_id: int) -> int:
        return int(gh_text("api", f"repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=1",
                           "--jq", ".total_count").strip())

    def branch_sha(self, branch: str) -> str:
        return gh_text("api", f"repos/{self.repo}/commits/{branch}", "--jq", ".sha").strip()

    def e2e_source_build(self, before: str) -> int | None:
        # No status=success filter: review_vm runs beside dispatch-iso, so a build
        # whose dispatch-iso started E2E can still conclude `failure` overall.
        runs = gh("api", f"repos/{self.repo}/actions/workflows/{BUILD_WORKFLOW}/runs"
                         f"?branch=testing&created=%3C{before}&per_page=10",
                  "--jq", "[.workflow_runs[] | {id}]")
        return source_build(runs, lambda run_id: gh(
            "api", f"repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=100")["jobs"])

    def dispatch(self, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        cmd = ["workflow", "run", workflow, "--repo", self.repo, "--ref", ref]
        for key, value in inputs.items():
            cmd += ["-f", f"{key}={value}"]
        gh_text(*cmd)


def e2e_inputs(api, lost: dict) -> dict[str, str]:
    build = api.e2e_source_build(lost["created_at"])
    if build is None:
        raise Unrecoverable(f"no successful testing build before {lost['created_at']} "
                            f"ran {E2E_DISPATCH_JOB}; dispatch post-testing-e2e.yml by hand")
    return {"build_run_id": str(build)}


class Target(NamedTuple):
    workflow: str
    ref: str  # runs are listed on, and the dispatch goes to, this ref
    inputs: Callable[[object, dict], dict[str, str]]


# In chain order: sync starts the testing build, the build starts E2E, E2E moves :testing.
TARGETS = [
    Target("sync-main-to-testing.yml", "main", lambda api, lost: {}),
    Target(BUILD_WORKFLOW, "testing", lambda api, lost: {"sha": api.branch_sha("testing")}),
    Target("post-testing-e2e.yml", "main", e2e_inputs),
]


def is_startup_failure(run: dict, job_count: int) -> bool:
    """Failed without producing a single job. A real failure always leaves a job behind."""
    return (run.get("status") == "completed" and run.get("conclusion") in STARTUP_CONCLUSIONS
            and job_count == 0)


class Report(NamedTuple):
    dispatched: list[str]
    covered: list[str]  # lost, but an upstream dispatch this sweep starts it
    failed: list[str]


def sweep(api, targets: list[Target], dry_run: bool = False, log=print) -> Report:
    report = Report([], [], [])
    for target in targets:
        wf = target.workflow
        # One unreadable workflow or failed dispatch must not stop the rest.
        try:
            runs = api.runs(wf, target.ref)
            if not runs:
                log(f"{wf}: no runs on {target.ref}")
                continue
            # Only the newest run matters: anything newer than a startup
            # failure (a re-dispatch, a manual run, the next trigger) answers it.
            newest = max(runs, key=lambda run: run["created_at"])
            if newest.get("status") != "completed" or newest.get("conclusion") not in STARTUP_CONCLUSIONS:
                log(f"{wf}: newest run {newest['id']} is {newest.get('conclusion') or newest.get('status')}; no action")
                continue
            jobs = api.job_count(newest["id"])
            if not is_startup_failure(newest, jobs):
                log(f"{wf}: newest run {newest['id']} failed with {jobs} job(s); a real failure, left red")
                continue
            # Targets are in chain order: an upstream dispatch starts this one.
            if report.dispatched:
                log(f"{wf}: run {newest['id']} never started; {report.dispatched[-1]} was "
                    f"re-dispatched this sweep and starts it downstream")
                report.covered.append(wf)
                continue
            inputs = target.inputs(api, newest)
            log(f"{wf}: run {newest['id']} never started and is still the newest; "
                f"re-dispatching on {target.ref} with {inputs or 'no inputs'}")
            if not dry_run:
                api.dispatch(wf, target.ref, inputs)
            report.dispatched.append(wf)
        except (GhError, Unrecoverable) as exc:
            log(f"::error title=startup failure not recovered::{wf}: {exc}")
            report.failed.append(wf)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be dispatched without dispatching it")
    args = parser.parse_args(argv)
    api = GitHub(os.environ.get("GITHUB_REPOSITORY", "projectbluefin/utah"))
    report = sweep(api, TARGETS, args.dry_run)
    print(f"\nre-dispatched {len(report.dispatched)}; covered downstream {len(report.covered)}; "
          f"failed {len(report.failed)}")
    for label, names in (("dispatched", report.dispatched), ("covered downstream", report.covered),
                         ("FAILED", report.failed)):
        if names:
            print(f"  {label}: {', '.join(names)}")
    # Non-zero only after every workflow had its turn, so a sweep that could
    # not recover something never reads as green.
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
