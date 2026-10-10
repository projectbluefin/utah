"""Fail-closed coverage for the e2e input trust gate.

scripts/resolve-e2e-inputs.py selects immutable images from one trusted
build run; every refusal branch must keep refusing (projectbluefin/utah#636).
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "resolve-e2e-inputs.py"

spec = importlib.util.spec_from_file_location("resolve_e2e_inputs", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

REPO = "projectbluefin/utah"
DIGEST = "sha256:" + "a" * 64


def good_run(**overrides):
    run = {
        "conclusion": "success",
        "head_branch": "testing",
        "event": "push",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "path": ".github/workflows/build.yml",
        "head_sha": "b" * 40,
    }
    run.update(overrides)
    return run


def artifact_dir(tmp, lines):
    d = Path(tmp) / "artifacts"
    d.mkdir(exist_ok=True)
    (d / "digests.txt").write_text("\n".join(lines) + "\n")
    return str(d)


class ResolveE2EInputsTests(unittest.TestCase):
    def test_happy_path_returns_pinned_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = gate.resolve(
                good_run(), ["utah"],
                artifact_dir(tmp, [f"utah|amd64|{DIGEST}"]), REPO)
        self.assertEqual(out, {"include": [{
            "image": "utah", "digest": DIGEST,
            "ref": f"ghcr.io/projectbluefin/utah@{DIGEST}",
        }]})

    def test_happy_path_tolerates_duplicate_digests_across_legs(self):
        # Mirrors the reusable-build layout: one image-digest-testing-* dir
        # per leg, each repeating the same digest in both line forms.
        nvidia = "sha256:" + "d" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "digests"
            for leg, name, digest in (("utah", "utah", DIGEST),
                                      ("utah-nvidia", "utah-nvidia", nvidia)):
                d = root / f"image-digest-testing-{leg}"
                d.mkdir(parents=True)
                (d / "digest.txt").write_text(f"{name}={digest}\n")
                (d / "platforms.txt").write_text(f"{name}|amd64|{digest}\n")
            (root / "image-digest-testing-utah" / "all.txt").write_text(
                f"utah={DIGEST}\nutah|amd64|{DIGEST}\n")
            out = gate.resolve(
                good_run(), ["utah", "utah-nvidia"], str(root), REPO)
        self.assertEqual(out, {"include": [
            {"image": "utah", "digest": DIGEST,
             "ref": f"ghcr.io/projectbluefin/utah@{DIGEST}"},
            {"image": "utah-nvidia", "digest": nvidia,
             "ref": f"ghcr.io/projectbluefin/utah-nvidia@{nvidia}"},
        ]})

    def test_rejects_unsuccessful_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "not a successful trusted"):
                gate.resolve(
                    good_run(conclusion="failure"), ["utah"],
                    artifact_dir(tmp, [f"utah|amd64|{DIGEST}"]), REPO)

    def test_rejects_wrong_branch_and_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = artifact_dir(tmp, [f"utah|amd64|{DIGEST}"])
            for bad in ({"head_branch": "main"}, {"event": "pull_request"}):
                with self.assertRaisesRegex(ValueError, "not a successful trusted"):
                    gate.resolve(good_run(**bad), ["utah"], d, REPO)

    def test_rejects_fork_and_foreign_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = artifact_dir(tmp, [f"utah|amd64|{DIGEST}"])
            for bad in ({"repository": {"full_name": "evil/fork"}},
                        {"head_repository": {"full_name": "evil/fork"}},
                        {"path": ".github/workflows/other.yml"},
                        {"head_sha": "not-a-sha"}):
                with self.assertRaisesRegex(ValueError, "not a successful trusted"):
                    gate.resolve(good_run(**bad), ["utah"], d, REPO)

    def test_rejects_unexpected_arch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unexpected architecture"):
                gate.resolve(
                    good_run(), ["utah"],
                    artifact_dir(tmp, [f"utah|arm64|{DIGEST}"]), REPO)

    def test_rejects_unknown_image_and_bad_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unexpected image name or digest"):
                gate.resolve(
                    good_run(), ["utah"],
                    artifact_dir(tmp, [f"other|amd64|{DIGEST}"]), REPO)
            with self.assertRaisesRegex(ValueError, "unexpected image name or digest"):
                gate.resolve(
                    good_run(), ["utah"],
                    artifact_dir(tmp, ["utah=not-a-digest"]), REPO)

    def test_rejects_conflicting_digests(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = "sha256:" + "c" * 64
            with self.assertRaisesRegex(ValueError, "conflicting image digests"):
                gate.resolve(
                    good_run(), ["utah"], artifact_dir(tmp, [
                        f"utah|amd64|{DIGEST}",
                        f"utah|amd64|{other}",
                    ]), REPO)

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_f = Path(tmp) / "run.json"
            run_f.write_text(json.dumps(good_run()))
            art = Path(tmp) / "artifacts"
            art.mkdir()
            images = json.loads(subprocess.check_output(
                [sys.executable, "scripts/flavors.py", "images"],
                text=True, cwd=ROOT))
            expected = [item["image"] for item in images]
            (art / "digests.txt").write_text("".join(
                f"{name}|amd64|{DIGEST}\n" for name in expected))
            proc = subprocess.run(
                [sys.executable, "scripts/resolve-e2e-inputs.py",
                 str(run_f), str(art), REPO],
                text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(
            {item["image"] for item in out["include"]}, set(expected))

    def test_rejects_incomplete_flavor_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "do not cover"):
                gate.resolve(
                    good_run(), ["utah", "utah-nvidia"],
                    artifact_dir(tmp, [f"utah|amd64|{DIGEST}"]), REPO)


class ResolveE2EInputsEdgeTests(unittest.TestCase):
    """Accepted forms and refusals the cases above do not reach.

    Fixtures mirror projectbluefin/actions' reusable-build.yml uploads: one
    image-digest-testing-* directory per leg, each with a .txt holding the
    legacy name=digest line and/or the name|platform|digest line.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "digests"
        self.root.mkdir()
        self.nvidia = "sha256:" + "d" * 64

    def write_leg(self, image, lines):
        leg = self.root / f"image-digest-testing-{image}"
        leg.mkdir(exist_ok=True)
        (leg / f"{image}-x86_64.txt").write_text("\n".join(lines) + "\n")

    def write_both(self):
        self.write_leg("utah", [f"utah={DIGEST}", f"utah|amd64|{DIGEST}"])
        self.write_leg("utah-nvidia", [f"utah-nvidia={self.nvidia}",
                                       f"utah-nvidia|amd64|{self.nvidia}"])

    def resolve(self, run=None, expected=("utah", "utah-nvidia"), repository=REPO):
        return gate.resolve(run or good_run(), list(expected), str(self.root),
                            repository)

    def assert_refused(self, message, **kwargs):
        with self.assertRaisesRegex(ValueError, message):
            self.resolve(**kwargs)

    def test_matrix_follows_configured_order(self):
        self.write_both()
        out = self.resolve(expected=("utah-nvidia", "utah"))
        self.assertEqual([i["image"] for i in out["include"]],
                         ["utah-nvidia", "utah"])

    def test_dispatch_and_schedule_events_are_accepted(self):
        self.write_both()
        for event in ("workflow_dispatch", "schedule"):
            with self.subTest(event=event):
                self.assertEqual(
                    len(self.resolve(run=good_run(event=event))["include"]), 2)

    def test_registry_owner_is_lowercased(self):
        self.write_both()
        mixed = {"full_name": "ProjectBluefin/utah"}
        out = self.resolve(
            run=good_run(repository=mixed, head_repository=mixed),
            repository="ProjectBluefin/utah")
        self.assertTrue(all(i["ref"].startswith("ghcr.io/projectbluefin/")
                            for i in out["include"]))

    def test_legacy_only_and_pipe_only_lines_each_suffice(self):
        self.write_leg("utah", [f"utah={DIGEST}"])
        self.write_leg("utah-nvidia", [f"utah-nvidia|amd64|{self.nvidia}"])
        self.assertEqual([i["digest"] for i in self.resolve()["include"]],
                         [DIGEST, self.nvidia])

    def test_blank_lines_and_non_txt_files_are_ignored(self):
        self.write_leg("utah", ["", f"utah={DIGEST}", ""])
        self.write_leg("utah-nvidia", [f"utah-nvidia={self.nvidia}"])
        (self.root / "notes.json").write_text("utah=not-a-digest\n")
        self.assertEqual(len(self.resolve()["include"]), 2)

    def test_untrusted_run_fields_are_refused(self):
        self.write_both()
        cases = {
            "conclusion missing": ("conclusion", None),
            "pull_request_target event": ("event", "pull_request_target"),
            "workflow_run event": ("event", "workflow_run"),
            "short sha": ("head_sha", "b" * 7),
            "uppercase sha": ("head_sha", "B" * 40),
            "missing sha": ("head_sha", None),
            "no repository": ("repository", None),
            "no head repository": ("head_repository", None),
        }
        for label, (key, value) in cases.items():
            with self.subTest(label):
                run = good_run()
                if value is None:
                    del run[key]
                else:
                    run[key] = value
                self.assert_refused("not a successful trusted", run=run)

    def test_self_consistent_run_from_another_repository_is_refused(self):
        self.write_both()
        other = {"full_name": "someone/utah"}
        self.assert_refused(
            "not a successful trusted",
            run=good_run(repository=other, head_repository=other))

    def test_malformed_digests_are_refused(self):
        bad = {
            "short": "sha256:" + "a" * 63,
            "uppercase": "sha256:" + "A" * 64,
            "other algorithm": "sha512:" + "a" * 64,
            "trailing space": DIGEST + " ",
        }
        for label, value in bad.items():
            with self.subTest(label):
                self.write_leg("utah", [f"utah={value}"])
                self.write_leg("utah-nvidia", [f"utah-nvidia={self.nvidia}"])
                self.assert_refused("unexpected image name or digest")

    def test_empty_artifact_download_is_refused(self):
        self.assert_refused("do not cover")

    def test_malformed_lines_fail_closed(self):
        for line in ("utah", f"utah|amd64|{DIGEST}|extra", f"utah={DIGEST}=x"):
            with self.subTest(line=line):
                self.write_leg("utah", [line])
                self.write_leg("utah-nvidia", [f"utah-nvidia={self.nvidia}"])
                with self.assertRaises(ValueError):
                    self.resolve()

    def test_cli_untrusted_run_exits_non_zero_with_no_matrix(self):
        self.write_both()
        run_f = self.root.parent / "run.json"
        run_f.write_text(json.dumps(good_run(event="pull_request")))
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), str(run_f), str(self.root), REPO],
            text=True, capture_output=True, cwd=ROOT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("not a successful trusted", proc.stderr)


if __name__ == "__main__":
    unittest.main()
