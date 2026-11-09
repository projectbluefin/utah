"""`:testing` is a ratchet over commits, and `:stable` needs E2E for its own digests.

The old promotion job moved `:testing` only if `testing` still pointed at the
tested commit. A merge lands on main about every hour and an E2E run takes
longer, so that was never true: run 40133928234 was green end to end and
promoted nothing. These tests drive scripts/promotion-ratchet.py against a
fake registry and a fake GitHub that implement the real command lines
(`skopeo inspect`, `gh api .../compare`, ...) from independent state -- a commit
graph and tag table -- so a wrong flag, a wrong direction of `compare`, or a
decision that quietly depends on the branch tip fails here instead of in a
40-minute-per-attempt workflow.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/promotion-ratchet.py"

spec = importlib.util.spec_from_file_location("promotion_ratchet", SCRIPT)
ratchet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ratchet)

REPO = "projectbluefin/utah"
PREFIX = "ghcr.io/projectbluefin"


def sha(name: str) -> str:
    """A stable 40-hex commit id whose first 7 characters identify `name`."""
    seed = name.encode().hex()
    return (seed + "0" * 40)[:7] + "a" * 33


def version(name: str, date: str = "20261024") -> str:
    return f"testing-{date}-{sha(name)[:7]}"


def digest(name: str) -> str:
    return "sha256:" + (name.encode().hex() + "0" * 64)[:64]


class World:
    """A registry plus a GitHub repository, behind subprocess-shaped calls."""

    # Committer dates order commits that share no ancestry: under testing ->
    # squash -> main a squashed-away baseline is 'diverged', not 'behind', so
    # the ratchet must compare dates to decide which side of the divergence wins.
    _DATES = {
        "base": "2026-10-24T00:00:00Z",
        "older": "2026-10-24T01:00:00Z",
        "promoted": "2026-10-24T02:00:00Z",
        "tested": "2026-10-24T03:00:00Z",
        "tip": "2026-10-24T04:00:00Z",
        # fork branches off base, so it is older than everything above base but
        # its line never reached main -- a diverged, older off-line commit.
        "fork": "2026-10-24T00:30:00Z",
    }

    def __init__(self):
        self.parents: dict[str, list[str]] = {}
        self.dates: dict[str, str] = {}
        self.tags: dict[str, str] = {}  # "ghcr.io/o/image:tag" -> digest
        self.labels: dict[str, dict] = {}  # digest -> labels
        self.statuses: dict[str, list[dict]] = {}
        self.calls: list[list[str]] = []
        self.skopeo_error: str | None = None  # stderr for tag lookups (`inspect --format`)
        self.api_error: str | None = None  # substring of an api path that fails
        self.posted: list[dict] = []

    # -- graph ---------------------------------------------------------
    def commit(self, name: str, *parents: str) -> str:
        key = sha(name)
        self.parents[key] = [sha(p) for p in parents]
        self.dates[key] = self._DATES[name]
        return key

    def ancestors(self, tip: str) -> set[str]:
        seen, queue = {tip}, deque([tip])
        while queue:
            for parent in self.parents.get(queue.popleft(), []):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        return seen

    def relation(self, base: str, head: str) -> str:
        if base == head:
            return "identical"
        if base in self.ancestors(head):
            return "ahead"
        if head in self.ancestors(base):
            return "behind"
        return "diverged"

    # -- registry ------------------------------------------------------
    def publish(self, image: str, commit: str, *, date="20261024", dated_tag=True, label=True) -> str:
        """Publish an image built from `commit`; returns its digest."""
        d = digest(f"{image}-{commit}-{date}")
        labels = {"org.opencontainers.image.revision": "b" * 40}  # the *base* image's, as in real images
        if label:
            labels[ratchet.VERSION_LABEL] = version(commit, date)
        self.labels[d] = labels
        if dated_tag:
            self.tags[f"{PREFIX}/{image}:{version(commit, date)}"] = d
        return d

    def promote(self, image: str, d: str, tag="testing"):
        self.tags[f"{PREFIX}/{image}:{tag}"] = d

    def entry(self, image: str, d: str) -> dict:
        return {"image": image, "digest": d, "ref": f"{PREFIX}/{image}@{d}"}

    # -- the subprocess seam ------------------------------------------
    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        out, err, rc = self._dispatch(list(argv))
        return subprocess.CompletedProcess(argv, rc, out, err)

    def _dispatch(self, argv):
        tool, rest = argv[0], argv[1:]
        if tool == "skopeo":
            return self._skopeo(rest)
        if tool == "gh":
            return self._gh(rest)
        raise AssertionError(f"unexpected command {argv}")

    def _skopeo(self, args):
        assert args[0] == "inspect", args
        if self.skopeo_error and args[1] == "--format":
            return "", self.skopeo_error, 1
        ref = args[-1].removeprefix("docker://")
        if args[1] == "--format":
            assert args[2] == "{{.Digest}}", args
            if "@" in ref:
                return ref.split("@")[1] + "\n", "", 0
            if ref not in self.tags:
                return "", f'reading manifest {ref.rsplit(":", 1)[1]} in {ref}: manifest unknown', 1
            return self.tags[ref] + "\n", "", 0
        assert args[1] == "--config", args
        assert "@" in ref, f"labels must be read from an exact digest, not a mutable tag: {ref}"
        d = ref.split("@")[1]
        if d not in self.labels:
            return "", "manifest unknown", 1
        return json.dumps({"config": {"Labels": self.labels[d]}}), "", 0

    def _gh(self, args):
        assert args[0] == "api", args
        path = args[1]
        if self.api_error and self.api_error in path:
            return "", f"HTTP 502 for {path}", 1
        prefix = f"repos/{REPO}/"
        assert path.startswith(prefix), path
        path = path[len(prefix):]
        if path.startswith("compare/"):
            base, head = path.removeprefix("compare/").split("...")
            for end in (base, head):
                if end not in self.parents:
                    return "", f"No commit found for SHA: {end}", 1
            return self.relation(base, head) + "\n", "", 0
        if path.startswith("statuses/"):
            assert "POST" in args, args
            fields = dict(a.split("=", 1) for i, a in enumerate(args) if i and args[i - 1] == "--field")
            entry = {
                "context": fields["context"], "state": fields["state"],
                "description": fields["description"], "creator": ratchet.ACTIONS_BOT,
                "target_url": fields["target_url"],
            }
            target = path.removeprefix("statuses/")
            self.statuses.setdefault(target, []).insert(0, entry)
            self.posted.append({"sha": target, **entry})
            return "{}", "", 0
        if path.endswith("/statuses"):
            target = path.removeprefix("commits/").removesuffix("/statuses")
            rows = self.statuses.get(target, [])
            return "\n".join(json.dumps({k: r[k] for k in ("context", "state", "description", "creator")})
                             for r in rows), "", 0
        if path.startswith("commits/"):
            assert path != "commits/testing", "the decision must never depend on the branch tip"
            prefix_sha = path.removeprefix("commits/")
            hits = [c for c in self.parents if c.startswith(prefix_sha)]
            if len(hits) != 1:
                return "", f"No commit found for SHA: {prefix_sha}", 1
            full = hits[0]
            commit = {"sha": full, "commit": {"committer": {"date": self.dates.get(full)}}}
            # Simulate `gh api ... --jq <filter>`: the real command extracts one
            # field from this response, so return the extracted value, not JSON.
            # full_sha reads .sha; committed_at reads .commit.committer.date.
            if "--jq" not in args:
                raise AssertionError(f"commits/ call missing --jq: {args}")
            jq = args[args.index("--jq") + 1]
            if jq == ".sha":
                return commit["sha"] + "\n", "", 0
            if jq == ".commit.committer.date":
                return commit["commit"]["committer"]["date"] + "\n", "", 0
            raise AssertionError(f"unexpected jq filter {jq!r}")
        raise AssertionError(f"unexpected api path {path}")


def world_with_history() -> World:
    """main:  base - older - promoted - tested - tip      side branch:  base - fork"""
    w = World()
    w.commit("base")
    w.commit("older", "base")
    w.commit("promoted", "older")
    w.commit("tested", "promoted")
    w.commit("tip", "tested")
    w.commit("fork", "base")
    return w


FLAVORS = ["utah", "utah-nvidia"]


def publish_promoted(w: World, commit: str) -> dict:
    digests = {}
    for image in FLAVORS:
        digests[image] = w.publish(image, commit)
        w.promote(image, digests[image])
    return digests


def tested_matrix(w: World, commit: str) -> dict:
    return {"include": [w.entry(i, w.publish(i, commit, date="20261025")) for i in FLAVORS]}


def decide(w: World, commit: str, matrix=None):
    matrix = matrix or tested_matrix(w, commit)
    return ratchet.decide(matrix, sha(commit), ratchet.Registry(w), ratchet.GitHub(REPO, w))


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.w = world_with_history()
        publish_promoted(self.w, "promoted")

    def test_a_descendant_of_the_promoted_commit_promotes(self):
        action, code, lines = decide(self.w, "tested")
        self.assertEqual((action, code), ("promote", 0))
        self.assertIn("descends from promoted", "\n".join(lines))

    def test_promotes_while_the_branch_tip_is_further_ahead(self):
        """The bug: tested != tip used to mean 'superseded'. Tip is irrelevant now."""
        self.assertIn(sha("tip"), self.w.parents)  # main moved on past the tested commit
        action, code, _ = decide(self.w, "tested")
        self.assertEqual((action, code), ("promote", 0))
        for call in self.w.calls:
            self.assertNotIn("commits/testing", " ".join(call))

    def test_an_older_commit_is_refused_loudly_without_failing_the_run(self):
        action, code, lines = decide(self.w, "older")
        self.assertEqual((action, code), ("refuse", 0))  # out-of-order is expected, not a red run
        joined = "\n".join(lines)
        self.assertIn("::warning", joined)
        self.assertIn("older than", joined)

    def test_a_ghost_baseline_is_rebaselined_to_the_verified_main_head(self):
        """A :testing commit whose line was rewritten out of main (a ghost) must
        not freeze the ratchet: move :testing to the current verified main HEAD."""
        w = world_with_history()
        # :testing points at a commit (fork) whose line is not reachable from
        # the current main HEAD "tip" -- structurally a ghost rewritten out of
        # history, exactly like 33ed66a in projectbluefin/utah#1033.
        for image in FLAVORS:
            w.promote(image, w.publish(image, "fork"))
        action, code, lines = decide(w, "tip")
        self.assertEqual((action, code), ("promote", 0))
        joined = "\n".join(lines)
        self.assertIn("supersedes it", joined)
        self.assertNotIn("::error", joined)

    def test_a_diverged_older_commit_refuses_and_never_moves_back(self):
        """The mirror of the ghost case: a diverged commit tested late (or an old
        build replayed by hand) must not drag :testing backward onto older
        content. Only the newer side of a divergence may win."""
        w = world_with_history()
        # :testing points at the newer main HEAD "tip"; the tested commit "fork"
        # is off main's line AND older than it, so it cannot promote.
        for image in FLAVORS:
            w.promote(image, w.publish(image, "tip"))
        action, code, lines = decide(w, "fork")
        self.assertEqual((action, code), ("refuse", 0))
        joined = "\n".join(lines)
        self.assertIn("Out-of-order", joined)
        self.assertNotIn("::error", joined)

    def test_a_behind_flavor_refuses_even_when_another_is_a_ghost(self):
        """The out-of-order ('behind') case refuses over a ghost re-baseline, so
        a stale tested commit can never move any flavor backward."""
        w = world_with_history()
        # utah-nvidia's :testing points at a ghost (fork, off main's line).
        w.promote("utah-nvidia", w.publish("utah-nvidia", "fork"))
        # utah's :testing points at a commit newer than the tested_sha.
        w.promote("utah", w.publish("utah", "tip"))
        action, code, lines = decide(w, "promoted")
        self.assertEqual((action, code), ("refuse", 0))
        self.assertIn("Out-of-order", "\n".join(lines))

    def test_the_same_digests_are_an_idempotent_noop(self):
        matrix = {"include": [self.w.entry(i, self.w.tags[f"{PREFIX}/{i}:testing"]) for i in FLAVORS]}
        action, code, _ = decide(self.w, "promoted", matrix)
        self.assertEqual((action, code), ("noop", 0))

    def test_a_nightly_rebuild_of_the_promoted_commit_promotes_the_newer_image(self):
        action, code, lines = decide(self.w, "promoted")  # tested_matrix publishes new digests
        self.assertEqual((action, code), ("promote", 0))
        self.assertIn("rebuild", "\n".join(lines))
        self.assertNotIn("::error", "\n".join(lines))

    def test_a_same_day_rebuild_of_the_promoted_commit_promotes_the_newer_image(self):
        matrix = {"include": [self.w.entry(i, self._rebuild_same_day(i)) for i in FLAVORS]}
        action, code, _ = decide(self.w, "promoted", matrix)
        self.assertEqual((action, code), ("promote", 0))

    def _rebuild_same_day(self, image: str) -> str:
        d = digest(f"{image}-promoted-sameday")
        self.w.labels[d] = {ratchet.VERSION_LABEL: version("promoted")}
        self.w.tags[f"{PREFIX}/{image}:{version('promoted')}"] = d
        return d

    def test_a_rebuild_superseded_by_a_newer_rebuild_is_refused_without_failing(self):
        matrix = tested_matrix(self.w, "promoted")
        for image in FLAVORS:  # a later rebuild of the same commit took the dated tag
            self.w.tags[f"{PREFIX}/{image}:{version('promoted', '20261025')}"] = digest(f"{image}-newest")
        action, code, lines = decide(self.w, "promoted", matrix)
        self.assertEqual((action, code), ("refuse", 0))
        joined = "\n".join(lines)
        self.assertIn("superseded-rebuild", joined)
        self.assertIn("::warning", joined)

    def test_no_promoted_image_at_all_promotes(self):
        w = world_with_history()
        action, code, lines = decide(w, "tested")
        self.assertEqual((action, code), ("promote", 0))
        self.assertIn("does not exist yet", "\n".join(lines))

    def test_an_unreadable_promoted_commit_fails_closed(self):
        w = world_with_history()
        for image in FLAVORS:
            d = w.publish(image, "promoted", label=False)
            w.promote(image, d)
        with self.assertRaisesRegex(ratchet.PromotionError, "cannot be recovered"):
            decide(w, "tested")

    def test_a_promoted_commit_unknown_to_github_fails_closed(self):
        w = world_with_history()
        for image in FLAVORS:
            w.promote(image, w.publish(image, "ghost"))  # label names a commit GitHub has never seen
        with self.assertRaisesRegex(ratchet.PromotionError, "No commit found"):
            decide(w, "tested")

    def test_a_github_api_failure_fails_closed(self):
        self.w.api_error = "/compare/"
        with self.assertRaisesRegex(ratchet.PromotionError, "HTTP 502"):
            decide(self.w, "tested")

    def test_a_registry_outage_is_not_mistaken_for_an_empty_registry(self):
        """Only 'manifest unknown' means first-ever promotion. 'denied' must not re-baseline."""
        matrix = tested_matrix(self.w, "tested")
        self.w.skopeo_error = "requesting bearer token: denied: permission_denied"
        with self.assertRaisesRegex(ratchet.PromotionError, "denied"):
            decide(self.w, "tested", matrix)

    def test_an_image_that_was_not_built_from_the_tested_commit_is_refused(self):
        matrix = tested_matrix(self.w, "older")  # digests built from another commit
        with self.assertRaisesRegex(ratchet.PromotionError, "not the tested commit"):
            decide(self.w, "tested", matrix)

    def test_a_partial_promotion_is_completed_by_rerunning_the_same_commit(self):
        """Tag copies are sequential: a crash leaves utah on the new commit, nvidia on the old."""
        matrix = tested_matrix(self.w, "tested")
        self.w.promote("utah", matrix["include"][0]["digest"])
        action, code, _ = decide(self.w, "tested", matrix)
        self.assertEqual((action, code), ("promote", 0))

    def test_one_stale_flavor_blocks_the_whole_promotion(self):
        matrix = tested_matrix(self.w, "promoted")
        self.w.promote("utah", self.w.publish("utah", "tested"))  # utah is already past the tested commit
        self.w.promote("utah-nvidia", self.w.publish("utah-nvidia", "older"))  # nvidia is behind it
        action, code, _ = decide(self.w, "promoted", matrix)
        self.assertEqual((action, code), ("refuse", 0))

    def test_a_rebuilt_dated_tag_still_resolves_through_the_image_label(self):
        """A same-day rebuild re-pushes testing-<date>-<sha>; the promoted commit must still resolve."""
        self.w.tags[f"{PREFIX}/utah:{version('promoted')}"] = digest("rebuilt")
        action, code, lines = decide(self.w, "tested")
        self.assertEqual((action, code), ("promote", 0))
        self.assertIn("warning", "\n".join(lines))


class RecordAndGateTests(unittest.TestCase):
    def setUp(self):
        self.w = world_with_history()
        self.registry = ratchet.Registry(self.w)
        self.github = ratchet.GitHub(REPO, self.w)
        self.matrix = tested_matrix(self.w, "tested")

    def record(self, matrix=None, commit="tested"):
        return ratchet.record(matrix or self.matrix, sha(commit), "https://example/run/1",
                              self.registry, self.github)

    def gate(self):
        variants = [{"image": i, "source_tag": "testing", "target_tag": "stable"} for i in FLAVORS]
        return ratchet.release_gate(variants, PREFIX, self.registry, self.github)

    def promote_all(self):
        for entry in self.matrix["include"]:
            self.w.promote(entry["image"], entry["digest"])

    def test_recording_publishes_each_digest_then_an_aggregate(self):
        self.record()
        by_context = {p["context"]: p for p in self.w.posted}
        for entry in self.matrix["include"]:
            posted = by_context[f"e2e/post-testing/{entry['image']}"]
            self.assertEqual(posted["description"], entry["digest"])
            self.assertEqual(posted["state"], "success")
            self.assertEqual(posted["sha"], sha("tested"))
        self.assertIn("e2e/post-testing", by_context)

    def test_rerunning_the_record_step_does_not_pile_up_statuses(self):
        self.record()
        before = len(self.w.posted)
        self.record()
        self.assertEqual(len(self.w.posted), before)

    def test_nothing_is_recorded_for_digests_built_from_another_commit(self):
        with self.assertRaises(ratchet.PromotionError):
            self.record(commit="older")
        self.assertEqual(self.w.posted, [])

    def test_release_passes_for_the_promoted_digests_and_retags_from_the_commit_tag(self):
        self.record()
        self.promote_all()
        variants, _ = self.gate()
        self.assertEqual([v["source_tag"] for v in variants], [version("tested", "20261025")] * 2)
        self.assertEqual({v["target_tag"] for v in variants}, {"stable"})
        # What the release will copy is exactly what the evidence covers.
        for v in variants:
            self.assertEqual(self.w.tags[f"{PREFIX}/{v['image']}:{v['source_tag']}"],
                             self.w.tags[f"{PREFIX}/{v['image']}:testing"])

    def test_release_is_blocked_without_evidence_for_the_promoted_digests(self):
        self.promote_all()  # promoted by hand or by an old run; no E2E record exists
        with self.assertRaisesRegex(ratchet.PromotionError, "no green E2E evidence"):
            self.gate()

    def test_release_is_keyed_to_the_promoted_commit_not_the_branch_tip(self):
        """E2E evidence on the tip must not bless an older promoted image."""
        tip = tested_matrix(self.w, "tip")
        self.record(tip, commit="tip")
        self.promote_all()  # :testing is still the `tested` commit
        with self.assertRaisesRegex(ratchet.PromotionError, "no green E2E evidence"):
            self.gate()

    def test_evidence_for_a_different_digest_of_the_same_commit_does_not_count(self):
        """'Last status wins' per commit would pass this; per-digest evidence must not."""
        self.promote_all()
        other = {"include": [self.w.entry(i, self.w.publish(i, "tested", date="20261026")) for i in FLAVORS]}
        self.record(other)  # a later green E2E, for a rebuild
        with self.assertRaisesRegex(ratchet.PromotionError, "no green E2E evidence"):
            self.gate()

    def test_a_status_not_written_by_actions_is_not_evidence(self):
        self.record()
        self.promote_all()
        for rows in self.w.statuses.values():
            for row in rows:
                row["creator"] = "someone-with-write-access"
        with self.assertRaisesRegex(ratchet.PromotionError, "no green E2E evidence"):
            self.gate()

    def test_release_refuses_when_the_commit_tag_no_longer_names_the_verified_image(self):
        self.record()
        self.promote_all()
        self.w.tags[f"{PREFIX}/utah:{version('tested', '20261025')}"] = digest("rebuilt")
        with self.assertRaisesRegex(ratchet.PromotionError, "not the verified digest"):
            self.gate()

    def test_release_fails_when_a_flavor_has_nothing_to_release(self):
        self.record()
        self.w.promote("utah", self.matrix["include"][0]["digest"])  # utah-nvidia has no :testing
        with self.assertRaisesRegex(ratchet.PromotionError, "does not exist"):
            self.gate()


class CommandLineTests(unittest.TestCase):
    """The workflow calls main(); its exit code and outputs are the contract."""

    def run_main(self, w, *argv):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "output"
            old = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(out)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = ratchet.main(list(argv), runner=w)
            finally:
                if old is None:
                    del os.environ["GITHUB_OUTPUT"]
                else:
                    os.environ["GITHUB_OUTPUT"] = old
            return code, out.read_text() if out.exists() else ""

    def test_decide_exit_codes_and_action_output(self):
        w = world_with_history()
        publish_promoted(w, "promoted")
        matrix = json.dumps(tested_matrix(w, "tested"))
        code, output = self.run_main(w, "decide", "--repository", REPO, "--tested-sha", sha("tested"), "--matrix", matrix)
        self.assertEqual((code, output.strip()), (0, "action=promote"))

        stale = json.dumps(tested_matrix(w, "older"))
        code, output = self.run_main(w, "decide", "--repository", REPO, "--tested-sha", sha("older"), "--matrix", stale)
        self.assertEqual((code, output.strip()), (0, "action=refuse"))

        w.api_error = "/compare/"
        code, output = self.run_main(w, "decide", "--repository", REPO, "--tested-sha", sha("tested"), "--matrix", matrix)
        self.assertEqual((code, output.strip()), (1, "action=refuse"))

    def test_malformed_matrix_fails_closed(self):
        code, output = self.run_main(world_with_history(), "decide", "--repository", REPO,
                                     "--tested-sha", sha("tested"), "--matrix", "{not json")
        self.assertEqual((code, output.strip()), (1, "action=refuse"))

    def test_release_gate_emits_variants_for_the_release_workflow(self):
        w = world_with_history()
        matrix = tested_matrix(w, "tested")
        ratchet.record(matrix, sha("tested"), "https://example/run/1", ratchet.Registry(w), ratchet.GitHub(REPO, w))
        for entry in matrix["include"]:
            w.promote(entry["image"], entry["digest"])
        variants = json.dumps([{"image": i, "source_tag": "testing", "target_tag": "stable"} for i in FLAVORS])
        code, output = self.run_main(w, "release-gate", "--repository", REPO, "--registry", PREFIX,
                                     "--variants", variants)
        self.assertEqual(code, 0)
        emitted = json.loads(output.splitlines()[-1].removeprefix("variants="))
        self.assertEqual([v["source_tag"] for v in emitted], [version("tested", "20261025")] * 2)

        w.statuses.clear()
        code, _ = self.run_main(w, "release-gate", "--repository", REPO, "--registry", PREFIX,
                                "--variants", variants)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
