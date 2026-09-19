"""CI must test the complete exact-digest set before publication."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "digests.txt"
        self.digest = "sha256:" + "a" * 64
        self.path.write_text(f"utah={self.digest}\nutah|amd64|{self.digest}\n")
        self.run = dict(conclusion="success", head_branch="testing", event="workflow_dispatch",
                        repository={"full_name": "projectbluefin/utah"},
                        head_repository={"full_name": "projectbluefin/utah"},
                        path=".github/workflows/build.yml", head_sha="b" * 40)

    def resolve(self, expected=None):
        return load("resolve-e2e-inputs").resolve(
            self.run, expected or ["utah"], self.tmp.name, "projectbluefin/utah")

    def test_dual_artifact_formats_agree(self):
        self.assertEqual(self.resolve()["include"][0]["ref"],
                         f"ghcr.io/projectbluefin/utah@{self.digest}")

    def test_rejects_untrusted_or_failed_builds(self):
        for field, value in [("event", "pull_request"), ("conclusion", "failure"),
                             ("head_branch", "main"), ("head_sha", "bad"),
                             ("path", "other.yml"),
                             ("head_repository", {"full_name": "attacker/utah"})]:
            with self.subTest(field=field):
                old = self.run[field]
                self.run[field] = value
                with self.assertRaises(ValueError):
                    self.resolve()
                self.run[field] = old

    def test_missing_flavor_is_fatal(self):
        with self.assertRaises(ValueError):
            self.resolve(["utah", "second-image"])

    def test_rejects_conflicts_tags_unknown_names_and_architectures(self):
        for line in ["utah=latest", "intruder=" + self.digest,
                     "utah|arm64|" + self.digest, "utah=sha256:" + "c" * 64]:
            with self.subTest(line=line):
                self.path.write_text(f"utah={self.digest}\n{line}\n")
                with self.assertRaises(ValueError):
                    self.resolve()


class EvidenceTests(unittest.TestCase):
    def test_ogc_config_gate_rejects_each_missing_live_boot_feature(self):
        script = (ROOT / "scripts/install-ogc-kernel.sh").read_text()
        # Execute only the pure config gate, never the package/kernel installer.
        gate = "required_config=" + script.split("required_config=", 1)[1].split(
            '\nif [ -f "${CACHE_DIR}/ogc.tar" ]; then', 1)[0]
        names = gate.split("(", 1)[1].split(")", 1)[0].split()
        for required in ["OVERLAY_FS", "SQUASHFS", "SQUASHFS_ZSTD", "EROFS_FS",
                         "BLK_DEV_LOOP", "DM_SNAPSHOT", "DM_CRYPT", "CRYPTO_XTS",
                         "FUSE_FS", "FS_VERITY", "SYSFB_SIMPLEFB", "DRM_SIMPLEDRM"]:
            self.assertIn(required, names)
            self.assertRegex(script, rf"--(?:enable|module) {required}(?:\s|$)")
        self.assertEqual(script.count("verify_config /usr/lib/utah/ogc-kernel.config"), 2)
        self.assertIn("make olddefconfig", script.split("verify_config .config")[0])
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            for missing in [None, *names]:
                with self.subTest(missing=missing):
                    config.write_text("".join(f"CONFIG_{name}=y\n"
                                              for name in names if name != missing))
                    result = subprocess.run(
                        ["bash", "-eu", "-c", gate + '\nverify_config "$1"',
                         "config-test", str(config)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0 if missing is None else 1)
                    if missing:
                        self.assertIn(f"CONFIG_{missing}", result.stderr)

    def test_offline_payload_preserves_manifest_digest(self):
        script = (ROOT / "iso/scripts/build-iso.sh").read_text()
        self.assertNotIn("oci-archive:", script)
        self.assertEqual(script.count("--preserve-digests"), 2)
        self.assertIn('"dir:${PAYLOAD_EXPORT}"', script)
        self.assertIn('dir:/payload "containers-storage:$1"', script)

    def test_iso_budget_guard_fails_closed_above_ceiling(self):
        # The budget guard (#128) is the whole point of the size drift this PR
        # closes. Extract the real block and run it with du stubbed so we can
        # drive both the byte count (-b) and the human size (-sh) without a
        # real ISO on disk -- the test exercises the logic, not a copy of it.
        script = (ROOT / "iso/scripts/build-iso.sh").read_text()
        # The guard must receive ISO_MAX_GB the way the real script delivers it:
        # as a positional arg into the <<'ASSEMBLY' heredoc, not from the outer
        # shell's environment. Assert that plumbing exists so a regression back
        # to an unexported, unpassed variable (which dies under set -u inside
        # the assembly) is caught here before it breaks every ISO build.
        self.assertRegex(script, r"podman unshare bash -s -- .*\$\{ISO_MAX_GB\}")
        self.assertIn('ISO_MAX_GB="$8"', script)
        start = script.index("iso_max_bytes=$(( ISO_MAX_GB")
        end = script.index("\nfi\n", start) + len("\nfi\n")
        guard = script[start:end]
        run = (
            "du() { if [ \"$1\" = \"-b\" ]; then echo \"$DU_BYTES\"; "
            "else echo \"$DU_HUMAN\"; fi; };\n"
            "OUTPUT_ISO=/tmp/utah-fakeiso\n"
            + guard
        )
        under = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "ISO_MAX_GB": "8", "DU_BYTES": str(7 * 1024 ** 3), "DU_HUMAN": "7.0G"}
        result = subprocess.run(["bash", "-eu", "-c", run], capture_output=True, text=True, env=under)
        self.assertEqual(result.returncode, 0, result.stderr)
        over = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "ISO_MAX_GB": "8", "DU_BYTES": str(8 * 1024 ** 3 + 512 * 1024 ** 2), "DU_HUMAN": "8.5G"}
        result = subprocess.run(["bash", "-eu", "-c", run], capture_output=True, text=True, env=over)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("exceeds 8 GB budget", result.stderr)

    def test_build_explicitly_dispatches_iso_after_both_image_jobs(self):
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        job = build["jobs"]["dispatch-iso"]
        self.assertEqual(set(job["needs"]), {"build_main", "build_kernel"})
        self.assertIn("refs/heads/testing", job["if"])
        self.assertIn("needs.build_main.result == 'success'", job["if"])
        self.assertIn("needs.build_kernel.result == 'success'", job["if"])
        self.assertIn("build_run_id=$GITHUB_RUN_ID", job["steps"][0]["run"])
        workflow = (ROOT / ".github/workflows/post-testing-e2e.yml").read_text()
        self.assertNotIn("workflow_run:", workflow)
        self.assertIn('timeout 300 gh run watch "$BUILD_RUN"', workflow)

    def test_readme_update_is_idempotent_and_preserves_other_text(self):
        update = load("update-e2e-readme").update
        proof = {"source_sha": "a" * 40, "e2e_run": "123"}
        text = "# Utah\n\nKeep this paragraph.\n"
        result = update(text, proof)
        self.assertEqual(update(result, proof), result)
        self.assertIn("Keep this paragraph.", result)
        self.assertIn("actions/runs/123", result)

    def test_publication_needs_all_luks_jobs_and_debug_images_are_not_uploaded(self):
        import yaml
        jobs = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())["jobs"]
        for name in ["promote-to-testing", "documentation"]:
            self.assertIn("luks", jobs[name]["needs"])
            self.assertNotIn("if", jobs[name])
        steps = jobs["luks"]["steps"]
        self.assertFalse(jobs["luks"]["strategy"]["fail-fast"])
        test = next(step for step in steps if "Run existing LUKS" in step.get("name", ""))
        self.assertEqual(test["env"]["UTAH_E2E_REQUIRE_FASTFETCH"], "1")
        uploads = [s for s in steps if "upload-artifact@" in s.get("uses", "")]
        for step in uploads:
            self.assertNotIn("output/", step["with"]["path"])
            self.assertNotIn("qcow2", step["with"]["path"])
        self.assertTrue(any(s.get("if") == "always()" for s in uploads))

    def test_harness_restricts_both_guests_and_requires_real_screenshots(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertEqual(script.count("restrict=on,hostfwd=tcp:127.0.0.1:"), 2)
        self.assertIn("systemd.wants=sshd.service", script)
        self.assertIn("fastfetch output was not visible", script)
        self.assertIn("missing required screenshot", script)


class FastfetchOcrGateTests(unittest.TestCase):
    """The gate runs against tesseract output, which drops and mangles glyphs."""

    GATE = ROOT / "iso/scripts/fastfetch-ocr-match.sh"

    # Verbatim from _temp/utah-luks-e2e/fastfetch-ocr.txt in the
    # iso-diagnostics-utah artifact of run 35374557822, whose screenshot showed
    # fastfetch but which the previous gate rejected.
    REAL_TRANSCRIPT = """TAH-E2E-FASTFETCH
[utahtest@utah-luks-test ~]$

utah: testing-20260918-2216657 &
Utah (Version: testing-20260918-2216657)
Linux 7.1.8-100.fc43.x86_64

2 mins

KVM/QEMU Standard PC (Q35 + ICH9, 2009) (pc-q35-10.2)
GNOME 51.beta
Mutter (Wayland)
"""

    def matches(self, transcript):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fastfetch-ocr.txt"
            path.write_text(transcript)
            return subprocess.run(["bash", str(self.GATE), str(path)]).returncode == 0

    def test_accepts_the_transcript_that_previously_failed_a_good_screenshot(self):
        self.assertTrue(self.matches(self.REAL_TRANSCRIPT))

    def test_accepts_a_clean_transcript_with_readable_field_labels(self):
        self.assertTrue(self.matches("UTAH-E2E-FASTFETCH\nKernel: 7.1.8-100.fc43.x86_64\n"))

    def test_rejects_a_desktop_with_no_terminal_on_it(self):
        self.assertFalse(self.matches("Activities\nSep 18  19:04\nutahtest\n"))

    def test_rejects_the_sentinel_without_any_fastfetch_body(self):
        self.assertFalse(self.matches("UTAH-E2E-FASTFETCH\n[utahtest@utah-luks-test ~]$\n"))

    def test_rejects_fastfetch_body_without_the_sentinel(self):
        self.assertFalse(self.matches("Kernel: 7.1.8-100.fc43.x86_64\nGNOME 51.beta\n"))

    def test_rejects_an_empty_or_missing_transcript(self):
        self.assertFalse(self.matches(""))
        self.assertFalse(
            subprocess.run(["bash", str(self.GATE), "/nonexistent/ocr.txt"]).returncode == 0)

    def test_harness_delegates_the_decision_and_prints_the_transcript_on_failure(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertIn("fastfetch-ocr-match.sh", script)
        self.assertNotIn("grep -qi 'UTAH.E2E.FASTFETCH'", script)
        self.assertIn("last OCR transcript", script)


class ForkPullRequestKernelCacheTests(unittest.TestCase):
    """A fork PR cannot publish the kernel cache, and must not go red for it.

    #154, #157 and #158 were all red on the same thing: the kernel compiled and
    the push then failed with "denied: installation not allowed to Write
    organization package", twenty minutes in, on a permission no contributor can
    be granted from a fork. Three PRs blocked by CI plumbing rather than by
    anything in their diffs.
    """

    WORKFLOW = ROOT / ".github/workflows/build.yml"

    def setUp(self):
        import yaml
        self.text = self.WORKFLOW.read_text()
        self.jobs = yaml.safe_load(self.text)["jobs"]

    def test_the_push_is_conditional_but_the_build_is_not(self):
        step = next(s for s in self.jobs["kernel_cache"]["steps"]
                    if s.get("id") == "cache")
        run = step["run"]
        # The compile is what validates a kernel change, so it always runs.
        self.assertIn("podman build --tag", run)
        build = run.index("podman build --tag")
        guard = run.index('if [ "${CAN_PUBLISH}" != "true" ]')
        push = run.index("podman push")
        self.assertLess(build, guard, "the build must not be behind the guard")
        self.assertLess(guard, push, "the push must be behind the guard")

    def test_can_publish_is_true_for_everything_that_is_not_a_fork_pr(self):
        step = next(s for s in self.jobs["kernel_cache"]["steps"]
                    if s.get("id") == "cache")
        env = step["env"]["CAN_PUBLISH"]
        # An empty head repository is a push, a schedule or a dispatch.
        self.assertIn("github.event.pull_request.head.repo.full_name == ''", env)
        self.assertIn("== github.repository", env)

    def test_the_flavored_builds_are_skipped_rather_than_left_to_fail(self):
        # Without this they would try to pull an image that was never pushed.
        self.assertIn("needs.kernel_cache.outputs.available == 'true'",
                      self.jobs["build_kernel"]["if"])
        self.assertEqual(self.jobs["kernel_cache"]["outputs"]["available"],
                         "${{ steps.cache.outputs.available }}")

    def test_a_cache_hit_still_reports_the_image_as_available(self):
        step = next(s for s in self.jobs["kernel_cache"]["steps"]
                    if s.get("id") == "cache")
        hit = step["run"].index("Cache hit")
        self.assertIn("available=true", step["run"][hit:hit + 200])
