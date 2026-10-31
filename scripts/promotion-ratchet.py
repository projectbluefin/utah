#!/usr/bin/env python3
"""Move `:testing` forward only, and gate `:stable` on E2E for the same digests.

`post-testing-e2e.yml` used to move `:testing` only when `testing` still pointed
at the tested commit. With a merge to main about every hour and an E2E run that
takes longer than that, the branch had always moved on by the time the E2E
finished, so a fully green run promoted nothing and `:testing` froze.

Promotion no longer asks where the branch tip is. It asks whether the tested
commit is a descendant of the commit behind the image that `:testing` already
points at -- a ratchet. Contribution never has to pause: main and testing keep
moving, and a green E2E for commit X promotes X wherever the tip is.

Which commit is behind a published image
----------------------------------------
Every build bakes `org.opencontainers.image.version=testing-<yyyymmdd>-<sha>`
into the image config (Justfile `build-ghcr`) and publishes the image under the
same tag. That tag is the commit-addressed name of the image. The image's own
`org.opencontainers.image.revision` and `vcs-ref` labels are NOT usable: they
are inherited from the Hummingbird base image and identical across Utah commits.
The label travels with the digest, so it still resolves after the dated tag has
been re-pushed by a same-day rebuild; the dated tag is checked as corroboration.

Subcommands (each writes `key=value` lines to $GITHUB_OUTPUT when it is set):

    decide        may the tested digests become :testing?   action=promote|noop|refuse
    record        publish per-digest E2E success as commit statuses
    release-gate  require that evidence for what :testing points at; emit the
                  `variants` JSON that retags the commit-addressed tags to :stable

Every ambiguity -- a registry or API error, an unreadable label, an unknown
commit -- fails closed with a non-zero exit and moves nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import NamedTuple

VERSION_LABEL = "org.opencontainers.image.version"
VERSION_RE = re.compile(r"^testing-(\d{8})-([0-9a-f]{7,40})$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Commit statuses are the durable, per-commit record of E2E success. One status
# per image carries the digest it tested in its description; the bare context
# is an aggregate for humans and for the reusable release gate.
EVIDENCE_CONTEXT = "e2e/post-testing"
ACTIONS_BOT = "github-actions[bot]"
COMPARE_STATUSES = {"identical", "ahead", "behind", "diverged"}


class PromotionError(Exception):
    """Something ambiguous or broken. The caller must move nothing."""


class Registry:
    """The two skopeo reads this needs. Injectable runner, like release-trigger."""

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def _skopeo(self, *args: str):
        return self.runner(["skopeo", *args], capture_output=True, text=True, check=False)

    def digest(self, ref: str) -> str | None:
        """Manifest digest behind a tag, or None when the tag does not exist.

        Only "manifest unknown" / "name unknown" mean absent. A denied or
        unreachable registry is an error, never an empty answer: treating it as
        "nothing promoted yet" would let any outage re-baseline `:testing`.
        """
        result = self._skopeo("inspect", "--format", "{{.Digest}}", f"docker://{ref}")
        if result.returncode == 0:
            digest = result.stdout.strip()
            if not DIGEST_RE.fullmatch(digest):
                raise PromotionError(f"{ref}: registry returned no usable digest ({digest!r})")
            return digest
        stderr = (result.stderr or "").lower()
        if "manifest unknown" in stderr or "name unknown" in stderr:
            return None
        raise PromotionError(f"{ref}: cannot read manifest: {(result.stderr or '').strip()}")

    def labels(self, ref: str) -> dict:
        result = self._skopeo(
            "inspect", "--config", "--override-os", "linux", "--override-arch", "amd64",
            f"docker://{ref}",
        )
        if result.returncode != 0:
            raise PromotionError(f"{ref}: cannot read image config: {(result.stderr or '').strip()}")
        try:
            config = json.loads(result.stdout)
        except ValueError as exc:
            raise PromotionError(f"{ref}: image config is not JSON: {exc}") from exc
        labels = (config.get("config") or {}).get("Labels") or {}
        if not isinstance(labels, dict):
            raise PromotionError(f"{ref}: image config Labels is not a map")
        return labels


class GitHub:
    """The `gh api` calls this needs, scoped to one repository."""

    def __init__(self, repository: str, runner=subprocess.run):
        self.repository = repository
        self.runner = runner

    def _api(self, path: str, *extra: str) -> str:
        result = self.runner(["gh", "api", path, *extra], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise PromotionError(f"gh api {path}: {(result.stderr or '').strip()}")
        return result.stdout.strip()

    def full_sha(self, prefix: str) -> str:
        sha = self._api(f"repos/{self.repository}/commits/{prefix}", "--jq", ".sha")
        if not SHA_RE.fullmatch(sha) or not sha.startswith(prefix):
            raise PromotionError(f"commit {prefix} did not resolve to a full SHA ({sha!r})")
        return sha

    def compare(self, base: str, head: str) -> str:
        """How `head` relates to `base`: identical, ahead, behind or diverged."""
        status = self._api(f"repos/{self.repository}/compare/{base}...{head}", "--jq", ".status")
        if status not in COMPARE_STATUSES:
            raise PromotionError(f"compare {base[:7]}...{head[:7]} returned {status!r}")
        return status

    def committed_at(self, sha: str) -> str:
        """ISO-8601 committer date; orders two commits that share no ancestry."""
        when = self._api(f"repos/{self.repository}/commits/{sha}", "--jq", ".commit.committer.date")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", when):
            raise PromotionError(f"commit {sha[:7]} has no usable committer date ({when!r})")
        return when

    def statuses(self, sha: str) -> list[dict]:
        out = self._api(
            f"repos/{self.repository}/commits/{sha}/statuses",
            "--paginate",
            "--jq", ".[] | {context, state, description, creator: .creator.login}",
        )
        try:
            return [json.loads(line) for line in out.splitlines() if line.strip()]
        except ValueError as exc:
            raise PromotionError(f"statuses for {sha[:7]} are not JSON: {exc}") from exc

    def post_status(self, sha: str, context: str, description: str, target_url: str) -> None:
        self._api(
            f"repos/{self.repository}/statuses/{sha}",
            "-X", "POST",
            "--field", "state=success",
            "--field", f"context={context}",
            "--field", f"description={description}",
            "--field", f"target_url={target_url}",
        )


class Commit(NamedTuple):
    sha: str
    version: str


def commit_behind(ref: str, registry: Registry, github: GitHub) -> Commit:
    """The full commit SHA the image at `ref` was built from, via its own label."""
    version = registry.labels(ref).get(VERSION_LABEL, "")
    match = VERSION_RE.fullmatch(version)
    if not match:
        raise PromotionError(
            f"{ref}: label {VERSION_LABEL}={version!r} is not testing-<yyyymmdd>-<sha>; "
            "the commit behind this image cannot be recovered"
        )
    return Commit(sha=github.full_sha(match.group(2)), version=version)


def _image_base(ref: str) -> str:
    return ref.split("@", 1)[0]


def _bind_tested(entry: dict, tested_sha: str, registry: Registry, github: GitHub) -> None:
    """The digest must be the one built from the commit the E2E says it tested."""
    if not DIGEST_RE.fullmatch(entry.get("digest", "")) or "@" not in entry.get("ref", ""):
        raise PromotionError(f"matrix entry is not an exact digest reference: {entry!r}")
    if not SHA_RE.fullmatch(tested_sha):
        raise PromotionError(f"tested commit {tested_sha!r} is not a full SHA")
    built = commit_behind(entry["ref"], registry, github)
    if built.sha != tested_sha:
        raise PromotionError(
            f"{entry['ref']} was built from {built.sha[:7]} ({built.version}), "
            f"not the tested commit {tested_sha[:7]}"
        )


class Outcome(NamedTuple):
    image: str
    verdict: str  # new | same-digest | ahead | rebuild | behind | superseded-rebuild | based
    detail: str


def assess(
    entry: dict,
    tested_sha: str,
    registry: Registry,
    github: GitHub,
    moving_tag: str = "testing",
) -> Outcome:
    image = _image_base(entry["ref"]).rsplit("/", 1)[-1]
    base = _image_base(entry["ref"])
    promoted_digest = registry.digest(f"{base}:{moving_tag}")
    if promoted_digest is None:
        return Outcome(image, "new", f":{moving_tag} does not exist yet")
    if promoted_digest == entry["digest"]:
        return Outcome(image, "same-digest", f":{moving_tag} already points at {promoted_digest}")

    promoted = commit_behind(f"{base}@{promoted_digest}", registry, github)
    named = registry.digest(f"{base}:{promoted.version}")
    note = ""
    if named != promoted_digest:
        # A same-day rebuild of the same commit re-pushes the dated tag. The
        # label still names the commit, so this is worth saying, not refusing.
        note = (
            f" (warning: {promoted.version} now points at {named or 'nothing'}, "
            "not the promoted digest; commit taken from the image label)"
        )
    relation = github.compare(promoted.sha, tested_sha)
    where = f"promoted {promoted.sha[:7]} ({promoted.version}){note}, tested {tested_sha[:7]}"
    if relation == "ahead":
        return Outcome(image, "ahead", f"tested commit descends from {where}")
    if relation == "identical":
        # The nightly rebuild of an unchanged branch re-pushes the same commit
        # under a new digest. That image passed E2E, so it replaces the older
        # one -- unless an even newer rebuild has since taken over the dated
        # tag, which would leave :testing on an image its tag no longer names.
        tested = commit_behind(entry["ref"], registry, github)
        current = registry.digest(f"{base}:{tested.version}")
        if current != entry["digest"]:
            return Outcome(
                image, "superseded-rebuild",
                f"same commit as {where}, but {tested.version} now points at "
                f"{current or 'nothing'}, not the tested digest {entry['digest']}",
            )
        return Outcome(
            image, "rebuild",
            f"newer image of the same commit as {where} ({entry['digest']} replaces {promoted_digest})",
        )
    if relation == "behind":
        return Outcome(image, "behind", f"tested commit is older than {where}")
    # Neither commit descends from the other. Under testing -> squash -> main
    # -> force-reset this is routine: the commit behind :testing was squashed
    # away. Only the newer side may win, so a pre-squash commit tested late
    # (or an old build replayed by hand) cannot drag :testing behind its squash.
    if github.committed_at(tested_sha) <= github.committed_at(promoted.sha):
        return Outcome(image, "diverged", f"tested commit is off-line and no newer than {where}")
    return Outcome(
        image,
        "based",
        f"baseline {promoted.sha[:7]} ({promoted.version}) was rewritten out of "
        f"history; tested commit {tested_sha[:7]} supersedes it",
    )


def decide(
    matrix: dict,
    tested_sha: str,
    registry: Registry,
    github: GitHub,
    moving_tag: str = "testing",
) -> tuple[str, int, list[str]]:
    """(action, exit code, log lines) for moving the tested digests to :testing.

    Raises PromotionError on any ambiguity; the caller exits non-zero.
    """
    entries = matrix.get("include") or []
    if not entries:
        raise PromotionError("the E2E matrix names no images")
    for entry in entries:
        _bind_tested(entry, tested_sha, registry, github)
    outcomes = [assess(entry, tested_sha, registry, github, moving_tag) for entry in entries]
    lines = [f"{o.image}: {o.verdict}: {o.detail}" for o in outcomes]
    verdicts = {o.verdict for o in outcomes}

    if "diverged" in verdicts:
        lines.append(
            f"::warning title=:{moving_tag} not moved::Out-of-order completion: the tested commit "
            f"{tested_sha[:7]} is off-line and no newer than what :{moving_tag} points at. "
            "Tags left as they are; evidence retained."
        )
        return "refuse", 0, lines
    if "behind" in verdicts:
        lines.append(
            f"::warning title=:{moving_tag} not moved::Out-of-order completion: the tested commit "
            f"{tested_sha[:7]} is older than what :{moving_tag} already points at. "
            "Tags left as they are; evidence retained."
        )
        return "refuse", 0, lines
    if "superseded-rebuild" in verdicts:
        lines.append(
            f"::warning title=:{moving_tag} not moved::A newer rebuild of commit {tested_sha[:7]} "
            "has replaced the tested image under its dated tag; its own E2E decides. "
            "Tags left as they are; evidence retained."
        )
        return "refuse", 0, lines
    if "based" in verdicts:
        # The commit behind :testing's current image was rewritten out of
        # history, so the old baseline is a ghost it can no longer verify
        # against. It is already unrecoverable under the old logic; move
        # :testing to the latest E2E-green commit rather than freezing it
        # forever. This branch is only reached when the tested commit is
        # strictly newer than the ghost: the 'diverged' and 'behind' cases
        # above refuse any off-line commit no newer than the promoted one, so
        # this re-baseline never moves a flavor backward.
        lines.append(
            f"::warning title=:{moving_tag} re-baselined::The commit :testing "
            "pointed at was rewritten out of history. Moving :testing to the "
            f"latest E2E-green commit {tested_sha[:7]} instead of freezing. "
            "Evidence is retained."
        )
        return "promote", 0, lines
    if verdicts == {"same-digest"}:
        lines.append(f"::notice title=:{moving_tag} already current::Nothing to promote; the tested digests are already :{moving_tag}.")
        return "noop", 0, lines
    lines.append(
        f"::notice title=promoting to :{moving_tag}::Tested commit {tested_sha[:7]} may advance :{moving_tag}."
    )
    return "promote", 0, lines


def record(
    matrix: dict,
    tested_sha: str,
    run_url: str,
    registry: Registry,
    github: GitHub,
) -> list[str]:
    """Publish one success status per tested digest, then the aggregate.

    Idempotent: a status already carrying this context and digest is not posted
    again, so rerunning the promotion job leaves one record, not a pile.
    """
    entries = matrix.get("include") or []
    if not entries:
        raise PromotionError("the E2E matrix names no images")
    for entry in entries:
        _bind_tested(entry, tested_sha, registry, github)
    existing = github.statuses(tested_sha)
    lines = []
    for entry in entries:
        image = _image_base(entry["ref"]).rsplit("/", 1)[-1]
        context = f"{EVIDENCE_CONTEXT}/{image}"
        if _has_evidence(existing, context, entry["digest"]):
            lines.append(f"{image}: evidence for {entry['digest']} already recorded")
            continue
        github.post_status(tested_sha, context, entry["digest"], run_url)
        lines.append(f"{image}: recorded E2E success for {entry['digest']} on {tested_sha[:7]}")
    aggregate = f"LUKS + production ISO passed for {len(entries)} digest(s)"
    if not _has_evidence(existing, EVIDENCE_CONTEXT, aggregate):
        github.post_status(tested_sha, EVIDENCE_CONTEXT, aggregate, run_url)
    return lines


def _has_evidence(statuses: list[dict], context: str, description: str) -> bool:
    return any(
        s.get("context") == context
        and s.get("state") == "success"
        and s.get("description") == description
        and s.get("creator") == ACTIONS_BOT
        for s in statuses
    )


def release_gate(
    variants: list[dict],
    registry_prefix: str,
    registry: Registry,
    github: GitHub,
) -> tuple[list[dict], list[str]]:
    """Require E2E evidence for exactly the digests :testing points at.

    Returns the release `variants` with each source_tag replaced by the
    commit-addressed tag, so the retag to :stable starts from the image this
    gate verified instead of resolving the moving tag a second time.
    """
    if not variants:
        raise PromotionError("no release variants")
    promoted, lines = [], []
    for variant in variants:
        image, source_tag = variant["image"], variant["source_tag"]
        base = f"{registry_prefix}/{image}"
        digest = registry.digest(f"{base}:{source_tag}")
        if digest is None:
            raise PromotionError(f"{base}:{source_tag} does not exist; nothing to release")
        commit = commit_behind(f"{base}@{digest}", registry, github)
        context = f"{EVIDENCE_CONTEXT}/{image}"
        if not _has_evidence(github.statuses(commit.sha), context, digest):
            raise PromotionError(
                f"no green E2E evidence for {image}@{digest}: commit {commit.sha[:7]} has no "
                f"successful {context} status recorded by {ACTIONS_BOT} for that digest"
            )
        named = registry.digest(f"{base}:{commit.version}")
        if named != digest:
            raise PromotionError(
                f"{base}:{commit.version} points at {named or 'nothing'}, not the verified digest {digest}; "
                "refusing to release an image the evidence does not cover"
            )
        lines.append(f"{image}: {digest} built from {commit.sha[:7]}: E2E evidence present")
        promoted.append({**variant, "source_tag": commit.version})
    return promoted, lines


def _emit(lines: list[str]) -> None:
    for line in lines:
        print(line)


def _output(**values: str) -> None:
    for key, value in values.items():
        print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None, runner=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("decide", "record"):
        p = sub.add_parser(name)
        p.add_argument("--repository", default=os.environ.get("REPOSITORY", ""))
        p.add_argument("--tested-sha", default=os.environ.get("SOURCE_SHA", ""))
        p.add_argument("--matrix", default=os.environ.get("MATRIX", ""))
        if name == "record":
            p.add_argument("--run-url", default=os.environ.get("RUN_URL", ""))
    gate = sub.add_parser("release-gate")
    gate.add_argument("--repository", default=os.environ.get("REPOSITORY", ""))
    gate.add_argument("--registry", default=os.environ.get("REGISTRY", ""))
    gate.add_argument("--variants", default=os.environ.get("VARIANTS", ""))
    args = parser.parse_args(argv)

    registry = Registry(runner)
    github = GitHub(args.repository, runner)
    try:
        if not args.repository:
            raise PromotionError("--repository is required")
        if args.command == "release-gate":
            promoted, lines = release_gate(json.loads(args.variants), args.registry, registry, github)
            _emit(lines)
            _output(variants=json.dumps(promoted, separators=(",", ":")))
            return 0
        matrix = json.loads(args.matrix)
        if args.command == "record":
            if not args.run_url:
                raise PromotionError("--run-url is required")
            _emit(record(matrix, args.tested_sha, args.run_url, registry, github))
            return 0
        action, code, lines = decide(matrix, args.tested_sha, registry, github)
        _emit(lines)
        _output(action=action)
        return code
    except (PromotionError, ValueError, KeyError, TypeError) as exc:
        detail = str(exc) if isinstance(exc, PromotionError) else f"malformed input: {exc!r}"
        print(f"::error title=promotion fails closed::{detail}")
        if args.command == "decide":
            _output(action="refuse")
        return 1


if __name__ == "__main__":
    sys.exit(main())
