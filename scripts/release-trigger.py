#!/usr/bin/env python3
"""Decide whether this run of execute-release.yml should cut a :stable release.

`execute-release.yml` retags :testing as :stable, and it has to know whether the
push it just saw was the promotion. It decided that by matching the head commit
message against `^chore: promote testing to main`.

That works, and it is silent when it does not. The promotion branch carries a
commit with exactly that subject, so a squash of the single-commit promotion PR
inherits it -- but a *merge commit* produces "Merge pull request #181 from
projectbluefin/auto/promote-testing-to-main", which does not match, and an
edited squash subject need not match either. The PR's own footer recommends
`gh pr merge --merge --admin`, which is precisely the case that fails.

When it fails, nothing says so. `check-trigger` succeeds, `execute` is skipped,
the workflow reported green, main advances, and no :stable tag appears. That is
also exactly what an ordinary merge to main looks like, so the failure is
indistinguishable from the normal case at a glance.

So the message is no longer the only signal. A commit that arrived from the
promotion branch is a promotion whatever its subject says, and a push that is
not a promotion says so out loud instead of leaving a bare skipped job.

The daily schedule follows the owner's policy instead: "if the :stable tag is
over a week old and :testing passes, we promote it". A scheduled run releases
only when :stable is more than STALE_AFTER old (or missing) and :testing points
at a digest :stable does not. Whether :testing passes is not decided here: the
e2e-evidence gate and the release gate that follow judge every release alike.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess

PROMOTION_BRANCH = "auto/promote-testing-to-main"
PROMOTION_PREFIX = "chore: promote testing to main"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseCheckError(Exception):
    """A registry or API read failed. The scheduled check fails; nothing moves."""


def registry_digest(ref: str, *, runner=subprocess.run) -> str | None:
    """Manifest digest behind a tag, or None when the tag does not exist.

    Only "manifest unknown" / "name unknown" mean absent; any other failure is
    an error, never "no :stable yet", which would release on a registry outage.
    --no-tags: without it skopeo lists every tag in the repository, which takes
    over half a minute per call for these images.
    """
    result = runner(
        ["skopeo", "inspect", "--no-tags", "--format", "{{.Digest}}", f"docker://{ref}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        digest = result.stdout.strip()
        if not DIGEST_RE.fullmatch(digest):
            raise ReleaseCheckError(f"{ref}: registry returned no usable digest ({digest!r})")
        return digest
    stderr = (result.stderr or "").lower()
    if "manifest unknown" in stderr or "name unknown" in stderr:
        return None
    raise ReleaseCheckError(f"{ref}: cannot read manifest: {(result.stderr or '').strip()}")


# "Over a week old" is measured from the last time :stable actually moved: the
# completion of the reusable release's retag step in an execute-release.yml run.
#
# Not the image config `created`: that is SOURCE_DATE_EPOCH, the date of the
# last build-input commit (scripts/podman-epoch-args.sh), and the retag is a
# `skopeo copy --preserve-digests`, so the released image cannot carry its
# release time at all (the pitfall projectbluefin/utah#1132 fixed for :testing).
#
# Not the `org.opencontainers.image.version` label (`testing-YYYYMMDD-<sha>`)
# either. It dates the build that was later released, not the release: a build
# released five days after it ran reads five days old on its release day, so
# the next scheduled release would follow two days later instead of a week. It
# is also per-flavor and day-granular: the four flavors of one commit carried
# 20261106 and 20261107 because their builds straddled midnight.
#
# The run is the release itself. execute-release.yml is the only writer of
# :stable, and the retag step copies every flavor and fails if any one fails,
# so a successful step means all flavors moved by its completion; a partial
# failure does not count, which keeps the oldest flavor's age in force. The
# run's conclusion is not used: a release can retag and then fail a later step.
STALE_AFTER = dt.timedelta(days=7)
RELEASE_WORKFLOW = "execute-release.yml"
# The caller job id ("execute") and the job and step names of
# projectbluefin/actions reusable-execute-release.yml at the pinned commit.
# tests/test_release_trigger.py fails when execute-release.yml moves off this
# pin: re-check both names against the new commit, then update it here.
RETAG_NAMES_VERIFIED_AT = "6453818f6279a2f2c1282919aad0009a88c01819"
RETAG_JOB = "execute / Promote release"
RETAG_STEP = "Promote digests to target tags"
# Runs are scanned back to (STALE_AFTER + RUN_SLACK): a release created before
# the window that retagged inside it still counts. The release gate plus a run
# queued behind another in the utah-stable-release group fit inside a day.
RUN_SLACK = dt.timedelta(days=1)
# Every push to main starts a run (about a hundred a week), so the retag steps
# are read in batches through GraphQL, not one REST jobs call per run. `ALL`
# includes earlier attempts: a rerun's latest attempt can hide one that
# retagged. GraphQL resolves at most 100 node ids per query.
RETAG_QUERY = """
query($ids: [ID!]!, $job: String!) {
  nodes(ids: $ids) {
    ... on WorkflowRun {
      databaseId
      checkSuite {
        checkRuns(first: 20, filterBy: {checkName: $job, checkType: ALL}) {
          nodes { steps(first: 50) { nodes { name conclusion completedAt } } }
        }
      }
    }
  }
}
"""
GRAPHQL_BATCH = 100


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _gh(args: list[str], runner, stdin: str | None = None) -> str:
    result = runner(["gh", "api", *args], input=stdin, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ReleaseCheckError(f"gh api {' '.join(args[:2])}: {(result.stderr or '').strip()}")
    return result.stdout


def retagged_at(check_runs: list[dict]) -> dt.datetime | None:
    """When a run retagged :stable: its retag step's completion, if it succeeded.

    `check_runs` are the run's RETAG_JOB check runs, one per attempt.
    """
    times = [
        _parse_time(step["completedAt"])
        for check_run in check_runs
        for step in (check_run.get("steps") or {}).get("nodes") or []
        if step.get("name") == RETAG_STEP and step.get("conclusion") == "SUCCESS"
    ]
    return max(times, default=None)


def last_stable_release(
    repository: str, since: dt.datetime, *, runner=subprocess.run, exclude: str = ""
) -> tuple[dt.datetime | None, dict | None]:
    """The latest retag of :stable by an execute-release run created since `since`.

    Returns (when, run), or (None, None) when no run in the window retagged.
    """
    out = _gh([
        "--paginate",
        f"repos/{repository}/actions/workflows/{RELEASE_WORKFLOW}/runs?per_page=100"
        f"&created=%3E%3D{since:%Y-%m-%dT%H:%M:%SZ}",
        "--jq", ".workflow_runs[] | {id, node_id, html_url}",
    ], runner)
    try:
        runs = [json.loads(line) for line in out.splitlines() if line.strip()]
    except ValueError as exc:
        raise ReleaseCheckError(f"execute-release runs are not JSON lines: {exc}") from exc
    runs = [run for run in runs if str(run["id"]) != exclude]

    latest: tuple[dt.datetime | None, dict | None] = (None, None)
    for start in range(0, len(runs), GRAPHQL_BATCH):
        batch = {run["id"]: run for run in runs[start:start + GRAPHQL_BATCH]}
        body = json.dumps({"query": RETAG_QUERY, "variables": {
            "ids": [run["node_id"] for run in batch.values()], "job": RETAG_JOB}})
        try:
            nodes = json.loads(_gh(["graphql", "--input", "-"], runner, body))["data"]["nodes"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ReleaseCheckError(f"retag lookup returned no usable data: {exc!r}") from exc
        found = {node["databaseId"]: node for node in nodes if node}
        if set(found) != set(batch):
            raise ReleaseCheckError(
                f"retag lookup did not resolve runs {sorted(set(batch) - set(found))}")
        for run_id, node in found.items():
            when = retagged_at(((node.get("checkSuite") or {}).get("checkRuns") or {}).get("nodes") or [])
            if when and (latest[0] is None or when > latest[0]):
                latest = (when, batch[run_id])
    return latest


def _days(delta: dt.timedelta) -> str:
    return f"{delta.total_seconds() / 86400:.1f} days"


def scheduled_release(
    variants: list[dict],
    registry_prefix: str,
    digest,
    last_release,
    now: dt.datetime,
) -> tuple[bool, list[str]]:
    """Whether the daily schedule releases: :stable over a week old, :testing new.

    Every flavor is checked. A flavor without :stable (a first release, or a
    flavor added since the last one) makes :stable stale. `digest` reads a tag
    (see registry_digest). `last_release` is called only when the registry alone
    cannot decide, and returns (when, run) for the last retag (see
    last_stable_release); one retag moves every flavor, so it dates them all.
    """
    if not variants:
        raise ReleaseCheckError("no release variants")
    missing, changed = [], []
    for variant in variants:
        base = f"{registry_prefix}/{variant['image']}"
        stable = digest(f"{base}:{variant['target_tag']}")
        if stable is None:
            missing.append(variant["image"])
        elif digest(f"{base}:{variant['source_tag']}") != stable:
            changed.append(variant["image"])

    gates = "Releasing, subject to the E2E evidence and release gates."
    if missing:
        return True, [
            "::notice title=stable release::Scheduled release: "
            f"{', '.join(missing)} has no :stable tag yet, so :stable counts as stale. {gates}"
        ]
    if not changed:
        return False, [
            "::notice title=no stable release::Nothing new: :testing and :stable point at "
            f"the same digest for every flavor ({', '.join(v['image'] for v in variants)}). "
            "No :stable tag was moved."
        ]

    week = f"{STALE_AFTER.days} days"
    new = f":testing has new digests for {', '.join(changed)}"
    when, run = last_release()
    if when is None:
        return True, [
            "::notice title=stable release::Scheduled release: no execute-release run retagged "
            f":stable in the last {_days(STALE_AFTER + RUN_SLACK)}, so it is over {week} old, "
            f"and {new}. {gates}"
        ]
    age = now - when
    if age > STALE_AFTER:
        return True, [
            "::notice title=stable release::Scheduled release: :stable last moved "
            f"{when:%Y-%m-%d %H:%M} UTC ({_days(age)} ago, in {run['html_url']}), over {week}, "
            f"and {new}. {gates}"
        ]
    return False, [
        f"::notice title=no stable release:::stable is {_days(age)} old, not over {week}: it last "
        f"moved {when:%Y-%m-%d %H:%M} UTC in {run['html_url']}. {new}; the daily schedule releases "
        f"them once :stable is over {week} old ({when + STALE_AFTER:%Y-%m-%d %H:%M} UTC). "
        "No :stable tag was moved. To release sooner, run this workflow manually."
    ]


def pull_request_head_refs(
    sha: str, repository: str, *, runner=subprocess.run
) -> list[str]:
    """Head refs of *this repository's* pull requests that contain this commit.

    head.ref is attacker-chosen on a fork pull request, and GitHub associates
    the merge or squash commit on main with the pull request it came from. A
    fork branch named auto/promote-testing-to-main would otherwise make an
    innocuous merge look exactly like the promotion and cut a :stable release,
    so a head ref only counts when it lives in this repository.

    Injectable runner so the decision can be tested without a network or a
    token: the GitHub CLI is the only thing here that needs either.
    """
    result = runner(
        [
            "gh", "api",
            f"repos/{repository}/commits/{sha}/pulls",
            # Both fields, filtered below, so the trust boundary is in Python
            # where it is covered by tests rather than inside a jq string.
            "--jq", '.[] | [(.head.repo.full_name // ""), .head.ref] | @tsv',
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    refs = []
    for line in result.stdout.splitlines():
        full_name, _, ref = line.partition("\t")
        ref = ref.strip()
        # A deleted fork reports a null repo, which is not this repository.
        if ref and full_name.strip() == repository:
            refs.append(ref)
    return refs


def decide(
    event: str,
    message: str,
    head_refs: list[str],
    *,
    branch: str = PROMOTION_BRANCH,
    prefix: str = PROMOTION_PREFIX,
) -> tuple[bool, list[str]]:
    """Whether this push or dispatch is a promotion, and what to say about it.

    A scheduled run has no commit to judge; scheduled_release decides it.
    head_refs must already be restricted to branches in this repository (see
    pull_request_head_refs); a fork can name its branch anything.

    Returns (is_promotion, annotations) where each annotation is a GitHub
    workflow command. The annotations are the point: every outcome explains
    itself, because the previous version's skip was invisible.
    """
    if event == "schedule":
        raise ValueError("a scheduled run is decided by scheduled_release, not by a commit")

    if event == "workflow_dispatch":
        return True, ["::notice title=stable release::Triggered manually."]

    subject = message.splitlines()[0] if message else ""

    if subject.startswith(prefix):
        return True, [
            "::notice title=stable release::Promotion recognised from the commit subject."
        ]

    if branch in head_refs:
        return True, [
            "::warning title=promotion subject did not match::"
            f"This commit came from {branch}, so it is a promotion, but its "
            f"subject does not start with '{prefix}'. It was recognised by "
            "branch instead. A merge commit or an edited squash subject causes "
            "this; squashing the single-commit promotion PR preserves the "
            "subject."
        ]

    return False, [
        "::notice title=no stable release::"
        f"This push is not a promotion: its subject does not start with "
        f"'{prefix}' and it did not come from {branch}. No :stable tag was "
        "moved. This is the expected outcome for an ordinary merge to main. To "
        "cut a release, merge the promotion pull request or run this workflow "
        "manually."
    ]


def main(
    argv: list[str] | None = None, *, runner=subprocess.run, now: dt.datetime | None = None
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default=os.environ.get("EVENT", ""))
    parser.add_argument("--message", default=os.environ.get("MESSAGE", ""))
    parser.add_argument("--sha", default=os.environ.get("SHA", ""))
    parser.add_argument("--repository", default=os.environ.get("REPOSITORY", ""))
    # Read by scheduled runs only: the flavors to check, where their tags live,
    # and this run's id (it cannot have retagged yet, so its jobs are not read).
    parser.add_argument("--registry", default=os.environ.get("REGISTRY", ""))
    parser.add_argument("--variants", default=os.environ.get("VARIANTS", ""))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    args = parser.parse_args(argv)

    if args.event == "schedule":
        now = now or dt.datetime.now(dt.timezone.utc)
        try:
            if not (args.repository and args.registry):
                raise ReleaseCheckError("a scheduled run needs --repository and --registry")
            is_promotion, annotations = scheduled_release(
                json.loads(args.variants),
                args.registry,
                lambda ref: registry_digest(ref, runner=runner),
                lambda: last_stable_release(
                    args.repository, now - STALE_AFTER - RUN_SLACK,
                    runner=runner, exclude=args.run_id,
                ),
                now,
            )
        except (ReleaseCheckError, ValueError, KeyError, TypeError) as exc:
            detail = str(exc) if isinstance(exc, ReleaseCheckError) else f"malformed input: {exc!r}"
            # An unreadable registry or API is neither stale nor fresh: fail
            # the run so it is seen, and release nothing.
            print(f"::error title=scheduled release check failed::{detail}")
            return 1
    else:
        head_refs: list[str] = []
        # Only worth a lookup when the cheap signals have not already decided it.
        if args.event != "workflow_dispatch" and not args.message.startswith(PROMOTION_PREFIX):
            if args.sha and args.repository:
                head_refs = pull_request_head_refs(args.sha, args.repository, runner=runner)
        is_promotion, annotations = decide(args.event, args.message, head_refs)

    for annotation in annotations:
        print(annotation)
    print(f"is-promotion={str(is_promotion).lower()}")

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"is-promotion={str(is_promotion).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
