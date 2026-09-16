"""CI must test the complete exact-digest set before publication."""
import importlib.util
import json
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
                         "FUSE_FS", "FS_VERITY"]:
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
