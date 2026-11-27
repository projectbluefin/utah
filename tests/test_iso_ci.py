"""CI must test the complete exact-digest set before publication."""
import importlib.util
import json
import os
import re
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
                         "BTRFS_FS", "BLK_DEV_LOOP", "DM_SNAPSHOT", "DM_CRYPT",
                         "CRYPTO_XTS", "FUSE_FS", "FS_VERITY", "SYSFB_SIMPLEFB",
                         "DRM_SIMPLEDRM", "VIDEO_DEV", "DRM_AMDGPU", "DRM_XE"]:
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

    def test_release_gate_runs_the_testsuite_commit_post_testing_validated(self):
        # A gate on a different testsuite commit can pass or fail on tests the
        # promoted image never ran (#773). Bump both refs together.
        import yaml
        e2e = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())
        release = yaml.safe_load((ROOT / ".github/workflows/execute-release.yml").read_text())
        validated = e2e["jobs"]["testsuite"]["with"]["test_ref"]
        gated = release["jobs"]["execute"]["with"]["gate_test_ref"]
        self.assertRegex(validated, r"^[0-9a-f]{40}$")
        self.assertEqual(gated, validated)

    def test_testsuite_checkout_matches_the_pinned_workflow_commit(self):
        # Workflow logic and checked-out tests must come from one testsuite
        # revision (#946); Renovate moves the uses: digest and test refs together.
        import yaml
        e2e = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())
        job = e2e["jobs"]["testsuite"]
        workflow_sha = job["uses"].rpartition("@")[2]
        self.assertEqual(job["with"]["test_ref"], workflow_sha)
        manager = next(m for m in json.loads((ROOT / "renovate.json").read_text())["customManagers"]
                       if m.get("depNameTemplate") == "projectbluefin/testsuite")
        for name in ["post-testing-e2e", "execute-release", "candidate-common"]:
            text = (ROOT / f".github/workflows/{name}.yml").read_text()
            pattern = re.sub(r"\(\?<(\w+)>", "(?P<\\1>", manager["matchStrings"][0])
            self.assertEqual(re.search(pattern, text)["currentDigest"], workflow_sha, name)
            self.assertTrue(any(re.search(p.strip("/"), f".github/workflows/{name}.yml")
                                for p in manager["managerFilePatterns"]), name)
        # The candidate workflow's testsuite job holds packages: write, so it
        # calls the same reviewed testsuite commit, never an unmerged one (#1214).
        candidate = yaml.safe_load((ROOT / ".github/workflows/candidate-common.yml").read_text())
        job = candidate["jobs"]["testsuite"]
        self.assertEqual(job["uses"].rpartition("@")[2], workflow_sha)
        self.assertEqual(job["with"]["test_ref"], workflow_sha)

    def test_build_explicitly_dispatches_iso_after_both_image_jobs(self):
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        job = build["jobs"]["dispatch-iso"]
        self.assertEqual(set(job["needs"]), {"build_main", "build_kernel", "verify_revision"})
        self.assertIn("refs/heads/testing", job["if"])
        self.assertIn("needs.build_main.result == 'success'", job["if"])
        self.assertIn("needs.build_kernel.result == 'success'", job["if"])
        # A pushed image labelled with another commit never reaches E2E (#1468).
        self.assertIn("needs.verify_revision.result == 'success'", job["if"])
        self.assertIn("build_run_id=$GITHUB_RUN_ID", job["steps"][0]["run"])
        workflow = (ROOT / ".github/workflows/post-testing-e2e.yml").read_text()
        self.assertNotIn("workflow_run:", workflow)
        self.assertIn('timeout 300 gh run watch "$BUILD_RUN"', workflow)

    def test_readme_update_is_idempotent_and_preserves_other_text(self):
        update = load("update-e2e-readme").update
        proof = {"source_sha": "a" * 40, "e2e_run": "123"}
        record_commit = "b" * 40
        text = "# Utah\n\nKeep this paragraph.\n"
        result = update(text, proof, record_commit)
        self.assertEqual(update(result, proof, record_commit), result)
        self.assertIn("Keep this paragraph.", result)
        self.assertIn("actions/runs/123", result)
        # The evidence links pin to the record's own commit, not the mutable
        # `verification` branch, so a frozen block stays self-consistent.
        self.assertIn(f"raw/{record_commit}/screenshots", result)
        self.assertIn(f"tree/{record_commit}", result)
        # The record commit has no README.md, so the screenshot must not link to a blob path (404, #1637).
        self.assertNotIn(f"blob/{record_commit}/README.md", result)
        # The record now lives on the `verification` branch, so the block must
        # not embed any local docs/verification/ path.
        self.assertNotIn("docs/verification/", result)
        self.assertIn("independent desktop tests that do not gate this record", result)
        self.assertNotIn("failure", result)
        self.assertIn("every flavor installed, booted, switched and rolled back", result)
        self.assertIn("screenshots/14-first-boot-desktop.png", result)

    def test_the_iso_record_pr_is_opened_as_mergeraptor(self):
        # A PR opened with the workflow token gets no pull_request checks and
        # an untrusted author, so it waits for a hand merge. The App token
        # opens it instead; the workflow token keeps only the record push.
        import yaml
        job = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())["jobs"]["documentation"]
        self.assertNotIn("pull-requests", job["permissions"])
        steps = {step.get("name"): step for step in job["steps"]}
        self.assertIn("actions/create-github-app-token@", steps["Get repository-scoped mergeraptor token"]["uses"])
        self.assertEqual(
            steps["Propose updated verification link"]["with"]["token"],
            "${{ steps.app-token.outputs.token }}",
        )
        guard = "github.repository_owner == 'projectbluefin' && github.ref == 'refs/heads/main'"
        for name in ["Get repository-scoped mergeraptor token", "Propose updated verification link"]:
            self.assertEqual(steps[name].get("if"), guard, name)


class ReviewAndCandidateInstallTests(unittest.TestCase):
    """The review VM and the Common candidate install-test with the gnome-setup medium (#1569)."""

    def steps(self, workflow, job):
        import yaml
        jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
        return "\n".join(step.get("run", "") for step in jobs[job]["steps"])

    def test_both_build_the_medium_from_the_local_candidate_and_drive_it(self):
        for workflow, job in (("build.yml", "review_vm"), ("candidate-common.yml", "candidate")):
            with self.subTest(workflow):
                run = self.steps(workflow, job)
                self.assertIn("-f iso/raw/Containerfile iso/raw", run)
                self.assertIn("bash iso/raw/build-raw-image.sh", run)
                self.assertIn("python3 iso/raw/raw_e2e.py", run)
                self.assertIn("--tpm", run)
                for gone in ("luks-e2e.sh", "build-iso.sh", "tacklebox"):
                    self.assertNotIn(gone, run)

    def test_no_test_media_is_uploaded(self):
        import yaml
        for workflow in ("build.yml", "candidate-common.yml"):
            jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
            for job in jobs.values():
                for step in job.get("steps", []):
                    if "upload-artifact@" in step.get("uses", ""):
                        with self.subTest(workflow=workflow, step=step.get("name")):
                            self.assertNotRegex(step["with"]["path"], r"medium\.raw|\.qcow2|id_ed25519|utah-live\.iso")

    def test_every_vm_job_grants_kvm_by_mode(self):
        # raw_e2e.py takes KVM only when /dev/kvm is writable, else TCG. A setfacl
        # grant was gone by the installed VM (run 45950837602), which booted under
        # TCG and missed /boot's device timeout; chmod lasts across the VMs.
        for workflow, job in (("build.yml", "review_vm"), ("candidate-common.yml", "candidate"),
                              ("image-installer.yml", "e2e")):
            with self.subTest(workflow):
                run = self.steps(workflow, job)
                self.assertIn("sudo chmod a+rw /dev/kvm", run)
                self.assertNotIn("sudo setfacl", run)

    def test_isolated_common_build_names_the_cgroupfs_manager(self):
        # The Common source build runs under env -i: no user D-Bus session, so
        # crun's systemd cgroup request for each RUN step went to the system
        # bus and was refused (run 45926387896). The build must name cgroupfs.
        run = self.steps("candidate-common.yml", "source")
        self.assertIn("podman --cgroup-manager=cgroupfs build --file", run)
        self.assertEqual(run.count(" build --file"), 1)


class E2eServicesTests(unittest.TestCase):
    """iso/scripts/e2e-services.sh, the installed-contract service gate raw_e2e.py runs."""

    def test_setup_unit_checks_reject_non_success_results(self):
        # systemd's Result= is success/exit-code/signal/timeout/...; it is never
        # "failed" (that is an ActiveState). A setup unit that exited non-zero
        # must fail the gate on both the system and user helper.
        script = (ROOT / "iso/scripts/e2e-services.sh").read_text()
        start = script.index("check_system_unit_state() {")
        end = script.index("\nverify_setup_state() {", start)
        block = script[start:end]

        def run(call, active, result):
            harness = (
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                "ssh_target() {\n"
                "  case \"$1\" in\n"
                "    *is-enabled*) echo enabled ;;\n"
                "    *) printf 'LoadState=loaded\\nActiveState=%s\\nResult=%s\\n"
                "ExecMainStartTimestamp=Tue 2026-10-13 10:00:00 UTC\\n' \"$ACTIVE\" \"$RESULT\" ;;\n"
                "  esac\n"
                "}\n"
                + block + "\n" + call + "\n"
            )
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "TEST_PASSWORD": "pw", "ACTIVE": active, "RESULT": result}
            return subprocess.run(["bash", "-eu", "-c", harness],
                                   capture_output=True, text=True, env=env)

        for call in ("check_system_unit_state ublue-system-setup.service true true first",
                     "check_user_unit_state ublue-user-setup.service true second"):
            ok = run(call, "inactive", "success")
            self.assertEqual(ok.returncode, 0, ok.stderr)
            for active, result in (("inactive", "exit-code"), ("failed", "exit-code"),
                                   ("failed", "success"), ("inactive", "timeout")):
                bad = run(call, active, result)
                self.assertNotEqual(bad.returncode, 0, (call, active, result))
                self.assertIn(f"Result={result}", bad.stderr)

    def test_setup_state_checks_every_unit_in_the_installed_contract(self):
        # #19: the runtime check follows the installed image's own contract,
        # not a list copied into the script, and a failed contract unit fails
        # the gate. brew-preinstall.service needs the network, so only its
        # enablement counts.
        script = (ROOT / "iso/scripts/e2e-services.sh").read_text()
        block = script[script.index("check_system_unit_state() {"):]

        def run(failed_unit):
            harness = (
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                "scp_target() { :; }\n"
                # Real ssh reads stdin; so does the stub, so a loop feeding
                # units on stdin would stop after the first one.
                "ssh_target() {\n"
                "  cat >/dev/null\n"
                "  case \"$1\" in\n"
                "    *\"--services 'enabled'\"*) printf 'gdm.service\\nuupd.timer\\n' ;;\n"
                "    *\"--services 'user_enabled'\"*) "
                "printf 'brew-preinstall.service\\npipewire.socket\\n' ;;\n"
                "    *is-enabled*) echo enabled ;;\n"
                "    *\"'$FAILED_UNIT'\"*) printf 'LoadState=loaded\\nActiveState=failed\\n"
                "Result=exit-code\\nExecMainStartTimestamp=n/a\\n' ;;\n"
                "    *) printf 'LoadState=loaded\\nActiveState=active\\nResult=success\\n"
                "ExecMainStartTimestamp=Tue 2026-10-13 10:00:00 UTC\\n' ;;\n"
                "  esac\n"
                "}\n"
                + block + "\nverify_setup_state first\n"
            )
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ROOT": str(ROOT),
                   "TEST_USER": "u", "TEST_PASSWORD": "pw", "FAILED_UNIT": failed_unit}
            return subprocess.run(["bash", "-eu", "-c", harness],
                                  capture_output=True, text=True, env=env,
                                  # The stub drains stdin; never wait on a terminal.
                                  stdin=subprocess.DEVNULL)

        ok = run("none")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        # Every unit is reported, not only the first of each list.
        for unit in ("gdm.service", "uupd.timer", "pipewire.socket",
                     "brew-preinstall.service"):
            self.assertIn(f"  {unit}: ", ok.stdout)
        offline = run("brew-preinstall.service")
        self.assertEqual(offline.returncode, 0, offline.stderr)
        for unit in ("gdm.service", "pipewire.socket"):
            bad = run(unit)
            self.assertNotEqual(bad.returncode, 0, unit)
            self.assertIn(f"{unit} failed during first boot", bad.stderr)

    def test_setup_state_checks_template_units_by_enablement_and_instances(self):
        # kmsconvt@.service (#1237) is a template: `systemctl show` reports
        # nothing for it, which read as "unavailable" and failed the lifecycle
        # candidate (run 44895186825). It must be enabled, and no instance may
        # have failed.
        script = (ROOT / "iso/scripts/e2e-services.sh").read_text()
        block = script[script.index("check_system_unit_state() {"):]

        def run(enabled, failed_instance, scope):
            system, user = (("kmsconvt@.service", "pipewire.socket") if scope == "system"
                            else ("gdm.service", "kmsconvt@.service"))
            harness = (
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                "scp_target() { :; }\n"
                "ssh_target() {\n"
                "  cat >/dev/null\n"
                "  case \"$1\" in\n"
                f"    *\"--services 'enabled'\"*) echo {system} ;;\n"
                f"    *\"--services 'user_enabled'\"*) echo {user} ;;\n"
                "    *\"is-enabled 'kmsconvt@.service'\"*) echo \"$ENABLED\" ;;\n"
                "    *is-enabled*) echo enabled ;;\n"
                "    *\"list-units --state=failed\"*\"'kmsconvt@*.service'\"*) "
                "[ -z \"$FAILED_INSTANCE\" ] || echo \"$FAILED_INSTANCE loaded failed failed\" ;;\n"
                # Like systemd: a template has no runtime state to show.
                "    *kmsconvt@.service*) ;;\n"
                "    *) printf 'LoadState=loaded\\nActiveState=active\\nResult=success\\n"
                "ExecMainStartTimestamp=Sat 2026-11-21 10:00:00 UTC\\n' ;;\n"
                "  esac\n"
                "}\n"
                + block + "\nverify_setup_state first\n"
            )
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ROOT": str(ROOT),
                   "TEST_USER": "u", "TEST_PASSWORD": "pw", "ENABLED": enabled,
                   "FAILED_INSTANCE": failed_instance}
            return subprocess.run(["bash", "-eu", "-c", harness],
                                  capture_output=True, text=True, env=env,
                                  stdin=subprocess.DEVNULL)

        for scope in ("system", "user"):
            with self.subTest(scope=scope):
                ok = run("enabled", "", scope)
                self.assertEqual(ok.returncode, 0, ok.stderr)
                self.assertIn("  kmsconvt@.service: enabled; no failed instance", ok.stdout)
                disabled = run("disabled", "", scope)
                self.assertNotEqual(disabled.returncode, 0)
                self.assertIn(f"required {scope} unit kmsconvt@.service is disabled",
                              disabled.stderr)
                failed = run("enabled", "kmsconvt@tty2.service", scope)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("kmsconvt@.service has a failed instance during first boot: "
                              "kmsconvt@tty2.service", failed.stderr)

    def test_setup_state_judges_resultless_units_by_active_state(self):
        # A target has no Result= property, so `systemctl show` omits it and
        # nfs-client.target failed the lifecycle candidate as
        # "ActiveState=active Result=unknown" (run 44926686288). A target is
        # judged by ActiveState alone; a service still needs Result=success.
        script = (ROOT / "iso/scripts/e2e-services.sh").read_text()
        start = script.index("check_system_unit_state() {")
        end = script.index("\nverify_setup_state() {", start)
        block = script[start:end]

        def run(call, active, result):
            harness = (
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                "ssh_target() {\n"
                "  case \"$1\" in\n"
                "    *is-enabled*) echo enabled ;;\n"
                "    *) printf 'LoadState=loaded\\nActiveState=%s\\n' \"$ACTIVE\"\n"
                "       [ -z \"$RESULT\" ] || printf 'Result=%s\\n' \"$RESULT\"\n"
                "       printf 'ExecMainStartTimestamp=\\n' ;;\n"
                "  esac\n"
                "}\n"
                + block + "\n" + call + "\n"
            )
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "TEST_PASSWORD": "pw", "ACTIVE": active, "RESULT": result}
            return subprocess.run(["bash", "-eu", "-c", harness],
                                  capture_output=True, text=True, env=env)

        for call in ("check_system_unit_state nfs-client.target true false upgraded",
                     "check_user_unit_state default.target false upgraded"):
            with self.subTest(call=call):
                ok = run(call, "active", "")
                self.assertEqual(ok.returncode, 0, ok.stderr)
                self.assertIn(".target: active", ok.stdout)
                bad = run(call, "failed", "")
                self.assertNotEqual(bad.returncode, 0)
                self.assertIn("failed during upgraded boot (ActiveState=failed", bad.stderr)
        missing = run("check_system_unit_state gdm.service true false upgraded", "active", "")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("gdm.service failed during upgraded boot", missing.stderr)
