"""Image-installer track tests: Non-blocking, publish-track, offline Flatpak and composefs checks.

Split from the former single tests/test_image_installer.py; see docs/adr/0001 and iso/raw/.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "iso/raw"
WORKFLOW = ROOT / ".github/workflows/image-installer.yml"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


medium = load("raw_medium", RAW / "medium.py")
raw_e2e = load("raw_e2e", RAW / "raw_e2e.py")
kernel_verity = load("kernel_verity", ROOT / "scripts/kernel_verity.py")


class NonBlockingTrackTests(unittest.TestCase):
    workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_the_track_runs_nightly_on_dispatch_and_on_its_own_changes(self):
        on = self.workflow[True]
        self.assertIn("workflow_dispatch", on)
        self.assertTrue(on["schedule"])
        self.assertIn("iso/raw/**", on["pull_request"]["paths"])
        self.assertNotIn("push", on)
        # Called inputs carry the dispatch defaults, so a call runs the same
        # medium (TPM, Flatpaks, compression) as the nightly run.
        call, dispatch = on["workflow_call"]["inputs"], on["workflow_dispatch"]["inputs"]
        self.assertEqual(set(call), set(dispatch))
        for name in dispatch:
            self.assertEqual(call[name].get("default"), dispatch[name].get("default"), name)
        self.assertNotIn("merge_group", on)

    def test_the_track_has_no_write_permission(self):
        self.assertEqual(self.workflow["permissions"], {})
        for name, job in self.workflow["jobs"].items():
            for scope, level in (job.get("permissions") or {}).items():
                self.assertEqual(level, "read", f"{name}: {scope}")

    def test_post_testing_gates_on_it(self):
        # #1569: the gnome-setup install of every flavor is the install gate.
        e2e = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())
        self.assertIn("gnome-setup", e2e["jobs"]["promote-to-testing"]["needs"])
        for other in (ROOT / ".github/workflows").glob("*.yml"):
            if other not in (WORKFLOW, ROOT / ".github/workflows/post-testing-e2e.yml"):
                self.assertNotIn("image-installer", other.read_text(), other.name)
        lane = e2e["jobs"]["gnome-setup"]
        self.assertEqual(lane["uses"], "./.github/workflows/image-installer.yml")
        self.assertEqual(lane["with"], {"image_ref": "${{ matrix.ref }}"})
        self.assertEqual(set(lane["needs"]), {"resolve", "verify-provenance"})
        self.assertNotIn("if", lane)
        # Every need must be a real job: a dangling one makes GitHub reject the
        # whole workflow file and no Post-Testing E2E run starts at all.
        for job_name, job in e2e["jobs"].items():
            needs = job.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            self.assertLessEqual(set(needs), set(e2e["jobs"]), job_name)
        self.assertEqual(lane["strategy"]["matrix"], "${{ fromJSON(needs.resolve.outputs.matrix) }}")
        self.assertEqual(set(lane["permissions"].values()), {"read"})
        name = self.workflow["jobs"]["e2e"]["name"]
        self.assertNotIn("Check all testing builds successful", name)
        self.assertNotEqual(name, "contract")

    def test_os_installer_is_gone(self):
        # ADR-0001 decision 4: the fallback until gnome-setup passed the E2E.
        for path in [WORKFLOW, *RAW.rglob("*")]:
            if path.is_file() and "__pycache__" not in path.parts:
                self.assertNotRegex(path.read_text(), r"(?i)os[-_]installer|OSI_REF", path.name)

    def test_gnome_setup_and_systemd_are_pinned_by_commit(self):
        containerfile = (RAW / "Containerfile").read_text()
        for ref in ("GS_REF", "SYSTEMD_REF"):
            self.assertRegex(containerfile, rf"(?m)^ARG {ref}=[0-9a-f]{{40}}$")
            self.assertIn(f'test "$(git -C /src rev-parse HEAD)" = "${{{ref}}}"', containerfile)

    def test_the_medium_is_never_uploaded(self):
        upload = next(s for s in self.workflow["jobs"]["e2e"]["steps"] if "upload-artifact" in s.get("uses", ""))
        paths = upload["with"]["path"]
        self.assertNotRegex(paths, r"medium\.raw|\.qcow2|id_ed25519")


class PublishTrackTests(unittest.TestCase):
    """publish-installer.yml: ship the clean medium to ghcr, never gate on it."""

    WORKFLOW = ROOT / ".github/workflows/publish-installer.yml"
    workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_it_runs_on_dispatch_and_weekly_but_not_on_push(self):
        on = self.workflow[True]
        self.assertIn("workflow_dispatch", on)
        self.assertTrue(on["schedule"])
        self.assertEqual(on["schedule"][0]["cron"], "41 9 * * 1")
        self.assertNotIn("push", on)
        self.assertNotIn("pull_request", on)
        self.assertNotIn("merge_group", on)

    def test_permissions_are_empty_except_the_publish_job(self):
        self.assertEqual(self.workflow["permissions"], {})
        for name, job in self.workflow["jobs"].items():
            scope = job.get("permissions") or {}
            if name == "publish":
                self.assertEqual(scope, {"contents": "read", "packages": "write"})
            else:
                self.assertEqual(scope, {"contents": "read"})

    def test_nothing_gates_on_the_publish_track(self):
        e2e = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())
        needs = e2e["jobs"]["promote-to-testing"]["needs"]
        self.assertFalse([n for n in needs if "publish" in n])

    def test_the_medium_is_published_not_uploaded(self):
        text = self.WORKFLOW.read_text()
        self.assertIn("oras push --artifact-type application/vnd.projectbluefin.installer.medium", text)
        self.assertNotIn("oras push --publish", text)
        self.assertIn("ghcr.io/projectbluefin/${IMAGE}-installer", text)

    def test_oras_is_pinned_to_v1_4_0(self):
        self.assertIn("oras_1.4.0_linux_amd64.tar.gz", self.WORKFLOW.read_text())

    def test_the_published_medium_is_clean_no_debug(self):
        text = self.WORKFLOW.read_text()
        # The E2E track bakes in a root key, sshd and debug kargs; the published
        # medium must not.
        self.assertNotIn("UTAH_RAW_DEBUG_SSHD", text)
        self.assertNotIn("UTAH_RAW_SSH_PUBKEY", text)
        self.assertNotIn("utah.live.debug", text)
        for step in self.workflow["jobs"]["publish"]["steps"]:
            if "build-raw-image.sh" in step.get("run", ""):
                self.assertNotIn("UTAH_RAW_DEBUG_SSHD", step["run"])
                self.assertNotIn("UTAH_RAW_SSH_PUBKEY", step["run"])

    def test_no_other_workflow_mentions_it(self):
        for other in (ROOT / ".github/workflows").glob("*.yml"):
            if other.name == "publish-installer.yml":
                continue
            self.assertNotIn("publish-installer", other.read_text(), other.name)


class OfflineFlatpakTests(unittest.TestCase):
    """ADR-0001 decision 6: Flatpaks on the medium's /var, never in an image."""

    def test_a_removed_app_must_stay_removed_and_the_rest_stay(self):
        baked = ["org.gnome.Calculator", "org.mozilla.firefox", "com.mitchellh.ghostty"]
        self.assertEqual(raw_e2e.flatpak_problems(baked, baked), [])
        self.assertEqual(raw_e2e.flatpak_problems(baked, baked[1:], "org.gnome.Calculator"), [])
        self.assertEqual(raw_e2e.flatpak_problems(baked, baked, "org.gnome.Calculator"),
                         ["org.gnome.Calculator came back after it was removed"])
        self.assertEqual(raw_e2e.flatpak_problems(baked, baked[:1]),
                         ["missing com.mitchellh.ghostty", "missing org.mozilla.firefox"])

    def test_results_carry_the_flatpak_checks(self):
        results = raw_e2e.Results("gui")
        results.flatpak("first boot", ["a", "b"], [])
        self.assertEqual(results.as_dict()["flatpaks"], {"first boot": {"apps": 2, "problems": []}})

    def test_no_image_layer_installs_flatpaks(self):
        containerfile = (RAW / "Containerfile").read_text()
        self.assertNotRegex(containerfile, r"(?i)flatpak")
        for path in (ROOT / "Containerfile", ROOT / "Containerfile.kernel"):
            self.assertNotRegex(path.read_text(), r"flatpak (install|preinstall)")

    def test_the_medium_bakes_them_into_the_stateroot_var_with_labels(self):
        bake = (RAW / "bake-flatpaks.sh").read_text()
        self.assertIn('-v "${stateroot}/var/lib/flatpak:/var/lib/flatpak"', bake)
        self.assertIn("flatpak preinstall --system", bake)
        self.assertIn("setfiles -F -r /stateroot", bake)
        self.assertNotRegex(bake, r"podman (build|commit|push)")

    def test_the_medium_bakes_the_brewfile_set(self):
        bake = (RAW / "bake-flatpaks.sh").read_text()
        brewfile = "/usr/share/ublue-os/homebrew/system-flatpaks.Brewfile"
        self.assertIn("/scripts/verify-desktop-contract.py", bake)
        self.assertIn(brewfile, bake)
        self.assertIn("--no-related --or-update flathub", bake)
        # The OCI (TunaOS) remote needs a session bus in the container.
        self.assertIn("dbus-daemon --session --fork --print-address", bake)

    def test_the_workflow_asserts_no_image_flatpaks_and_bakes_before_shrinking(self):
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["e2e"]["steps"]
        names = [s.get("name", "") for s in steps]
        assert_step = steps[names.index("Assert the published image and the live layer carry no Flatpaks")]
        self.assertIn("/var/lib/flatpak", assert_step["run"])
        bake = steps[names.index("Bake the Flatpaks into the medium and shrink it")]["run"]
        self.assertLess(bake.index("bake-flatpaks.sh"), bake.rindex('shrink-raw-image.sh localhost/utah-raw:ci "$WORK/medium.raw"'))
        e2e = steps[names.index("End-to-end with screenshots")]["run"]
        self.assertIn("--flatpaks", e2e)

    def test_flatpaks_are_on_by_default_and_a_dispatch_can_turn_them_off(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        flag = workflow[True]["workflow_dispatch"]["inputs"]["flatpaks"]
        self.assertEqual((flag["type"], flag["default"]), ("boolean", True))
        # A bare inputs.flatpaks is null on schedule and pull_request, which
        # would turn the bake off there.
        self.assertEqual(workflow["jobs"]["e2e"]["env"]["FLATPAKS"],
                         "${{ github.event_name != 'workflow_dispatch' || inputs.flatpaks }}")

DIGEST = "ab" * 64
COMPOSEFS_ENTRY = f"""title Utah 44.20261120
version 44.20261120
sort-key bootc-utah-0
options root=UUID=8f3e rw rhgb quiet composefs={DIGEST} systemd.mount-extra=UUID=1b2c:/boot:ext4:defaults
linux /bootc_composefs-{DIGEST}/vmlinuz
initrd /bootc_composefs-{DIGEST}/initrd
"""


class ComposefsTests(unittest.TestCase):
    """The composefs backend: the medium's default on a kernel with the btrfs fs-verity fix, ostree otherwise."""

    def test_the_live_entry_keeps_fs_verity_strict(self):
        live = medium.live_entry(COMPOSEFS_ENTRY)
        words = next(line for line in live.splitlines() if line.startswith("options ")).split()[1:]
        self.assertIn(f"composefs={DIGEST}", words)
        self.assertFalse(any(w.startswith("composefs=?") for w in words))
        self.assertEqual(words[-2:], ["ro", "utah.live"])
        self.assertIn(f"linux /bootc_composefs-{DIGEST}/vmlinuz\n", live)
        self.assertEqual(medium.live_entry(live), live)

    def test_an_installed_boot_must_enforce_verity_on_every_object(self):
        good = {"cmdline": f"root=UUID=1 rw composefs={DIGEST}", "root_source": f"composefs:{DIGEST}",
                "objects": 10, "verity_objects": 10, "read_errors": 0, "corrupted_reports": 0}
        self.assertEqual(raw_e2e.composefs_problems(good), [])
        v1 = {**good, "cmdline": f"rw composefs.digest=v1-sha512-12:{DIGEST}"}
        self.assertEqual(raw_e2e.composefs_problems(v1), [])
        self.assertEqual(raw_e2e.composefs_problems({**good, "cmdline": "rw ostree=/x"}),
                         ["no composefs karg on the kernel command line"])
        bad = {**good, "cmdline": f"composefs=?{DIGEST}", "root_source": "/dev/vda4", "verity_objects": 7,
               "read_errors": 1, "corrupted_reports": 2}
        problems = raw_e2e.composefs_problems(bad)
        self.assertEqual(len(problems), 5)
        self.assertIn("3 of 10 composefs objects have no fs-verity", problems)
        results = raw_e2e.Results("gui")
        results.composefs_boot("first boot", good, [])
        self.assertNotIn("cmdline", results.as_dict()["composefs"]["first boot"])

    def test_the_medium_is_built_with_the_selected_backend(self):
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertIn('REQUESTED_BACKEND="${UTAH_RAW_BACKEND:-auto}"', build)
        self.assertIn('- select --backend "${REQUESTED_BACKEND}"', build)
        self.assertIn('"${EVIDENCE}/backend.json"', build)
        self.assertIn("install_args+=(--composefs-backend)", build)
        self.assertIn('"${mnt}"/root/state/deploy/*', build)
        self.assertIn("composefs-verity.json", build)
        bake = (RAW / "bake-flatpaks.sh").read_text()
        self.assertIn('"${mnt}/state/os/default"', bake)
        workflow = yaml.safe_load(WORKFLOW.read_text())
        backend = workflow[True]["workflow_dispatch"]["inputs"]["backend"]
        self.assertEqual((backend["options"], backend["default"]), (["auto", "composefs", "ostree"], "auto"))
        self.assertIn("scripts/kernel_verity.py", workflow[True]["pull_request"]["paths"])
        env = workflow["jobs"]["e2e"]["env"]
        self.assertEqual(env["BACKEND"], "${{ inputs.backend || 'auto' }}")
        steps = {s.get("name", ""): s.get("run", "") for s in workflow["jobs"]["e2e"]["steps"]}
        built = steps["Build the raw medium (bootc install to-filesystem, once)"]
        self.assertIn('UTAH_RAW_BACKEND="$BACKEND"', built)
        self.assertIn('MEDIUM_BACKEND=$(jq -r .backend "$WORK/raw-evidence/backend.json")', built)
        e2e = steps["End-to-end with screenshots"]
        self.assertIn('--backend "$MEDIUM_BACKEND"', e2e)
        self.assertIn('--backend-decision "$WORK/raw-evidence/backend.json"', e2e)

    def test_the_backend_decision_reaches_results(self):
        decision = {"requested": "auto", "backend": "ostree", "kernel": "7.3.5-200.fc44.x86_64", "fixed": False,
                    "reason": "7.3.5-200.fc44.x86_64 is 7.2 or 7.3 without 254e15591ee4 in its changelog"}
        results = raw_e2e.Results("gui")
        results.backend = {"backend": "ostree", **decision}
        self.assertEqual(results.as_dict()["backend"]["kernel"], "7.3.5-200.fc44.x86_64")

    def test_the_live_initramfs_is_the_booted_kernels(self):
        # Gaming images carry a second module tree that sorts above the OGC
        # kernel's; rebuilding that one left the booted initrd without the
        # live overlay (run 45729186820: "serial never showed UTAH_LIVE_READY").
        containerfile = (RAW / "Containerfile").read_text()
        self.assertNotIn("ls /usr/lib/modules | sort -V | tail -1", containerfile)
        self.assertIn("-name vmlinuz", containerfile)
        self.assertIn('kver="${kernel_dirs##*/}"', containerfile)

    def test_the_live_overlay_sits_under_both_root_setups(self):
        units = RAW / "src/usr/lib/systemd/system"
        live_root = (units / "utah-live-root.service").read_text()
        self.assertIn("Before=ostree-prepare-root.service bootc-root-setup.service initrd-root-fs.target", live_root)
        # The composefs repository is bound from the medium over the overlay:
        # erofs cannot mount its image from an overlayfs file.
        self.assertIn("mount --bind /sysroot/composefs /run/utah-live/composefs", live_root)
        self.assertIn("mount --bind /run/utah-live/composefs /sysroot/composefs", live_root)
        for dropin in ("ostree-prepare-root.service.d", "bootc-root-setup.service.d"):
            text = (units / dropin / "10-utah-live.conf").read_text()
            self.assertIn("Wants=utah-live-root.service", text)
            self.assertIn("After=utah-live-root.service", text)
        dracut = (RAW / "src/usr/lib/dracut/dracut.conf.d/60-utah-live.conf").read_text()
        containerfile = (RAW / "Containerfile").read_text()
        for path in ("/usr/lib/systemd/system/bootc-root-setup.service.d/10-utah-live.conf",
                     "/usr/lib/systemd/system/ostree-prepare-root.service.d/10-utah-live.conf"):
            self.assertIn(path, dracut)
            self.assertIn(path.lstrip("/"), containerfile)

    def test_the_checker_reads_every_object_only_on_request(self):
        checker = (RAW / "composefs_check.py").read_text()
        self.assertIn('OBJECTS = "/sysroot/composefs/objects"', checker)
        self.assertIn('read = "--read" in argv[1:]', checker)
        self.assertIn("FILE CORRUPTED", checker)
        harness = (RAW / "raw_e2e.py").read_text()
        self.assertIn('self.check_composefs(phase, read=phase == "first boot")', harness)

    def test_the_live_session_leaves_the_medium_boot_unmounted(self):
        # boot.mount is skipped live; on composefs the gpt-auto automount
        # would then fail every mount namespace with ENODEV (dbus-broker).
        units = RAW / "src/usr/lib/systemd/system"
        for unit in ("boot.mount.d", "boot.automount.d"):
            self.assertIn("ConditionKernelCommandLine=!utah.live", (units / unit / "50-utah-live.conf").read_text())

    def test_the_live_session_does_not_download_flatpaks(self):
        # The live session writes to RAM; the installed system preinstalls.
        units = RAW / "src/usr/lib/systemd/system"
        self.assertIn("ConditionKernelCommandLine=!utah.live",
                      (units / "flatpak-preinstall.service.d/50-utah-live.conf").read_text())

    def test_the_live_session_skips_the_booted_image_snapshot(self):
        # Its bootc status remounts /sysroot read-write, which the live RAM
        # overlay refuses, so the unit failed every live boot (run 45918946265).
        units = RAW / "src/usr/lib/systemd/system"
        self.assertIn("ConditionKernelCommandLine=!utah.live",
                      (units / "ublue-booted-image.service.d/50-utah-live.conf").read_text())

    def test_build_injected_etc_placeholders_are_removed(self):
        # podman RUN leaves empty 0700 resolv.conf/hostname; ostree-ext drops
        # them, bootc's composefs backend does not.
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertIn('"${deployments[0]}/etc/resolv.conf" "${deployments[0]}/etc/hostname"', build)
        self.assertIn('! -s "$f"', build)
        harness = (RAW / "raw_e2e.py").read_text()
        self.assertIn("resolv.conf: Permission denied", harness)

    def test_boot_is_labeled_and_its_denials_fail_the_e2e(self):
        # bootc labels the /boot root only on ostree; the medium labels all of
        # /boot after its last write there, and the E2E fails on any denial (#1368).
        build = (RAW / "build-raw-image.sh").read_text()
        label = build.index("setfiles -F -r /target")
        self.assertGreater(label, build.index('medium.py" grubenv'))
        self.assertGreater(label, build.index('medium.py" live-entry'))
        self.assertIn('-v "${bootdir}:/target/boot"', build)
        self.assertIn("^/boot: .*:boot_t:", build)
        harness = (RAW / "raw_e2e.py").read_text()
        self.assertIn('path=\\"/boot[/\\"]', harness)
