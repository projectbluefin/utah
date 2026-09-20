#!/usr/bin/env python3
"""Decide whether a push to main is a testing-to-main promotion.

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
the workflow reports green, main advances, and no :stable tag appears. That is
also exactly what an ordinary merge to main looks like, so the failure is
indistinguishable from the normal case at a glance.

So the message is no longer the only signal. A commit that arrived from the
promotion branch is a promotion whatever its subject says, and a push that is
not a promotion says so out loud instead of leaving a bare skipped job.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

PROMOTION_BRANCH = "auto/promote-testing-to-main"
PROMOTION_PREFIX = "chore: promote testing to main"


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
    """Whether this push is a promotion, and what to say about it.

    head_refs must already be restricted to branches in this repository (see
    pull_request_head_refs); a fork can name its branch anything.

    Returns (is_promotion, annotations) where each annotation is a GitHub
    workflow command. The annotations are the point: every outcome explains
    itself, because the previous version's skip was invisible.
    """
    if event == "workflow_dispatch":
        return True, ["::notice title=stable release::Triggered manually."]

    subject = message.splitlines()[0] if message else ""

    if subject.startswith(prefix):
        return True, [
            f"::notice title=stable release::Promotion recognised from the commit subject."
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default=os.environ.get("EVENT", ""))
    parser.add_argument("--message", default=os.environ.get("MESSAGE", ""))
    parser.add_argument("--sha", default=os.environ.get("SHA", ""))
    parser.add_argument("--repository", default=os.environ.get("REPOSITORY", ""))
    args = parser.parse_args()

    head_refs: list[str] = []
    # Only worth a lookup when the cheap signals have not already decided it.
    if args.event != "workflow_dispatch" and not args.message.startswith(
        PROMOTION_PREFIX
    ):
        if args.sha and args.repository:
            head_refs = pull_request_head_refs(args.sha, args.repository)

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
