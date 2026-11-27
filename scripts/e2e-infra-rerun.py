#!/usr/bin/env python3
"""Decide whether a failed Post-Testing E2E run failed only on infrastructure.

A Post-Testing E2E run is 45 jobs and over an hour of runners, and one red leg
skips `promote-to-testing`. In run 44538634534 every LUKS, lifecycle and
production-ISO job passed; two testsuite legs failed pulling an image from
GHCR ("fetching blob: received unexpected HTTP status: 500"), so the whole
run was wasted. `rerun-e2e-infra-failures.yml` calls this on every failed
first attempt and re-runs the failed jobs once, after a delay, only when
every failed job is explained by a signature below. One unexplained failure
of a gating job leaves the run red: a re-run must never paper over a product defect.

Each failed job is judged on its first failing step only -- the step that
printed the first `##[error]` line -- so a transient error an earlier step
recovered from cannot excuse a later product failure. A job with no log at
all (HTTP 404/410: the runner died before uploading one) is infra. Any other
error reading the API fails this script, which re-runs nothing.

Every signature names the run that shows it, so the list stays auditable. Add
one only with such a run.

Usage:
  e2e-infra-rerun.py RUN_ID      # classify; writes infra/jobs to $GITHUB_OUTPUT

Environment: GH_TOKEN (actions: read), GITHUB_REPOSITORY, and optionally
GITHUB_OUTPUT and GITHUB_STEP_SUMMARY.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

API = "https://api.github.com"
# Conclusions that fail a run. Timed-out and cancelled jobs are classified like
# any other failure: none of them is excused without a matching signature.
FAILED = {"failure", "timed_out", "cancelled"}
NO_LOGS = "no-logs"


@dataclass(frozen=True)
class Signature:
    name: str
    pattern: re.Pattern
    evidence: str


SIGNATURES = (
    Signature(
        "runner-shutdown",
        re.compile(r"The runner has received a shutdown signal"
                   r"|lost communication with the server"),
        "hosted runner reclaimed mid-job (tuna-os/tunaOS run 31766322789)",
    ),
    Signature(
        "registry-5xx",
        re.compile(r"received unexpected HTTP status: 5\d\d"
                   r"|ghcr\.io\S*.*\b5\d\d \(?(?:Internal Server Error|Bad Gateway"
                   r"|Service Unavailable|Gateway Timeout)"),
        "GHCR blob 500 in two testsuite legs (utah run 44538634534)",
    ),
    Signature(
        "sigstore-5xx",
        re.compile(r"(?:rekor|fulcio|tuf-repo-cdn)\.sigstore\.dev\S*.*"
                   r"(?:\b5\d\d\b|Bad Gateway|Service Unavailable)"),
        "Rekor outage during signing (tuna-os/tunaOS run 31858324517)",
    ),
    Signature(
        "rpm-mirrors",
        re.compile(r"All mirrors were tried|Librepo error: Curl error"),
        "dnf could reach no mirror for a package or metadata download",
    ),
    Signature(
        "codeberg-dns",
        re.compile(r"Could not resolve host: codeberg\.org"),
        "codeberg.org DNS failure (fuzzy-application-search submodule)",
    ),
    Signature(
        "gnome-gitlab-5xx",
        re.compile(r"unable to access 'https://gitlab\.gnome\.org/\S*': "
                   r"The requested URL returned error: 5\d\d"),
        "GNOME GitLab 503 cloning gnome-ponytail-daemon failed 12 testsuite legs (utah run 45673646964)",
    ),
    Signature(
        "libpod-lock",
        re.compile(r"libpod_lock"),
        "podman lock exhaustion on hosted runners (tuna-os/tunaOS run 31766602231)",
    ),
)


def failing_step(log: str) -> str:
    """The log of the step that printed the first `##[error]`, through its error lines.

    A log with no `##[error]` is judged whole.
    """
    lines = log.splitlines()
    first = next((i for i, line in enumerate(lines) if "##[error]" in line), None)
    if first is None:
        return log
    start = max((i for i in range(first) if "##[group]Run " in lines[i]), default=0)
    end = first
    while end + 1 < len(lines) and "##[error]" in lines[end + 1]:
        end += 1
    return "\n".join(lines[start:end + 1])


def classify(log: str | None) -> tuple[str | None, str]:
    """(signature name or None, the line that matched) for one failed job's log."""
    if log is None:
        return NO_LOGS, "the job has no log (runner died before uploading one)"
    step = failing_step(log)
    for signature in SIGNATURES:
        match = signature.pattern.search(step)
        if match:
            start = step.rfind("\n", 0, match.start()) + 1
            end = step.find("\n", match.end())
            return signature.name, step[start:end if end != -1 else None].strip()
    return None, ""


@dataclass(frozen=True)
class Verdict:
    job_id: int
    name: str
    signature: str | None
    evidence: str


def decide(failed_jobs: list[dict], log_of) -> tuple[bool, list[Verdict]]:
    """Re-run only when there is a failed job and every one matched a signature."""
    verdicts = []
    for job in failed_jobs:
        signature, evidence = classify(log_of(job["id"]))
        verdicts.append(Verdict(job["id"], job["name"], signature, evidence))
    rerun = bool(verdicts) and all(v.signature for v in verdicts)
    return rerun, verdicts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _request(url: str, token: str | None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def failed_jobs(repo: str, run_id: str, token: str) -> list[dict]:
    jobs, page = [], 1
    while True:
        url = f"{API}/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}"
        with urllib.request.urlopen(_request(url, token), timeout=60) as response:
            batch = json.load(response)["jobs"]
        jobs += batch
        if len(batch) < 100:
            break
        page += 1
    # --failed re-runs every failed job, so none may bypass classification.
    return [job for job in jobs if job.get("conclusion") in FAILED]


def job_log(repo: str, job_id: int, token: str) -> str | None:
    """The job's log, or None when GitHub holds none (404/410). Other errors raise."""
    opener = urllib.request.build_opener(_NoRedirect)
    url = f"{API}/repos/{repo}/actions/jobs/{job_id}/logs"
    try:
        response = opener.open(_request(url, token), timeout=60)
    except urllib.error.HTTPError as error:
        with error:
            if error.code in (404, 410):
                return None
            if error.code not in (301, 302, 307, 308):
                raise
            # The log lives in blob storage behind a signed URL; never send
            # it the GitHub token.
            location = error.headers["Location"]
        with urllib.request.urlopen(_request(location, None), timeout=120) as blob:
            return blob.read().decode("utf-8", errors="replace")
    with response:
        return response.read().decode("utf-8", errors="replace")


def summary(run_url: str, rerun: bool, verdicts: list[Verdict]) -> str:
    lines = [
        "### E2E infra re-run classification",
        "",
        f"Run: {run_url}",
        "",
        "| Job | Signature | Evidence |",
        "|---|---|---|",
    ]
    for v in verdicts:
        evidence = v.evidence.replace("|", "\\|")[:300]
        lines.append(f"| `{v.name}` | {v.signature or '**none (unexplained)**'} | {evidence} |")
    if not verdicts:
        lines.append("| — | no failed jobs | |")
    lines += ["", "Decision: " + ("re-run the failed jobs once." if rerun else
                                  "leave the run red; not every failure is a known infra fault.")]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print(__doc__, file=sys.stderr)
        return 2
    run_id = argv[1]
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    jobs = failed_jobs(repo, run_id, token)
    rerun, verdicts = decide(jobs, lambda job_id: job_log(repo, job_id, token))
    text = summary(f"https://github.com/{repo}/actions/runs/{run_id}", rerun, verdicts)
    print(text)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"infra={'true' if rerun else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
