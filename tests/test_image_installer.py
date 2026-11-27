"""The experimental image-based installer track (docs/adr/0001, iso/raw/).

Covers the medium's boot configuration and shrink plan (iso/raw/medium.py),
the harness's per-stage results (iso/raw/raw_e2e.py), the size measurement
(iso/raw/measure_medium.py), the comparison with the ISO/fisherman luks job
and the promise that the track never gates
anything: image-installer.yml is not a required check, not in
promote-to-testing's needs, and publishes nothing. It also covers the publish
track that ships the clean medium to ghcr (publish-installer.yml) and what it
must not bake into it.
"""

import contextlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
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
measure_medium = load("measure_medium", RAW / "measure_medium.py")
kernel_verity = load("kernel_verity", ROOT / "scripts/kernel_verity.py")

ENTRY = """title Fedora Linux 44 (Utah) (ostree:0)
version 1
options root=UUID=8f3e rw boot=UUID=1b2c rhgb quiet ostree=/ostree/boot.1/default/abc/0
linux /ostree/default-abc/vmlinuz-7.0
initrd /ostree/default-abc/initramfs-7.0.img
"""


class LiveEntryTests(unittest.TestCase):
    def test_live_entry_boots_read_only_with_the_live_karg_first(self):
        live = medium.live_entry(ENTRY, ["utah.live.debug"])
        self.assertIn("title Utah Live (try or install)\n", live)
        self.assertIn("version 1001\n", live)
        options = next(line for line in live.splitlines() if line.startswith("options "))
        words = options.split()[1:]
        self.assertNotIn("rw", words)
        self.assertEqual(words[-3:], ["ro", "utah.live", "utah.live.debug"])
        self.assertIn("rhgb", words)
        self.assertIn("linux /ostree/default-abc/vmlinuz-7.0\n", live)

    def test_live_entry_never_duplicates_the_karg(self):
        twice = medium.live_entry(medium.live_entry(ENTRY))
        self.assertEqual(twice.count("utah.live"), 1)

    def test_an_entry_without_options_is_rejected(self):
        with self.assertRaises(ValueError):
            medium.live_entry("title x\nversion 1\n")


class GrubenvTests(unittest.TestCase):
    def test_grubenv_is_one_padded_block_defaulting_to_the_live_entry(self):
        current = "# GRUB Environment Block\nboot_success=1\nsaved_entry=old\n" + "#" * 900
        env = medium.grubenv(current, "utah-live")
        self.assertEqual(len(env), medium.GRUBENV_SIZE)
        self.assertTrue(env.startswith(medium.GRUBENV_HEADER))
        body = [line for line in env.split("\n") if line and not line.startswith("#")]
        self.assertEqual(body, ["boot_success=1", "saved_entry=utah-live"])


TABLE = {"partitiontable": {"label": "gpt", "sectorsize": 512, "partitions": [
    {"node": "/dev/loop0p1", "start": 2048, "size": 2048},
    {"node": "/dev/loop0p2", "start": 4096, "size": 1048576},
    {"node": "/dev/loop0p3", "start": 1052672, "size": 2097152},
    {"node": "/dev/loop0p4", "start": 3149824, "size": 47181790},
]}}


ESP_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX_FS_TYPE = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
ROOT_X86_64_TYPE = "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709"


class MediumContractTests(unittest.TestCase):
    """The medium layout and kargs an encrypted, graphical install needs."""

    def test_esp_then_a_separate_boot_then_root_last(self):
        names = [name for name, _, _ in medium.LAYOUT]
        types = {name: ptype for name, ptype, _ in medium.LAYOUT}
        sizes = {name: size for name, _, size in medium.LAYOUT}
        # build-raw-image.sh formats p2, p3 and p4 as vfat, ext4 and btrfs.
        self.assertEqual(names.index("EFI-SYSTEM"), 1)
        self.assertEqual(names.index("boot"), 2)
        self.assertEqual(names[-1], "root")
        self.assertEqual(types["EFI-SYSTEM"], ESP_TYPE)
        self.assertEqual(sizes["EFI-SYSTEM"], "512MiB")
        # GRUB reads /boot unencrypted; install.sh encrypts only the root.
        self.assertEqual(types["boot"], LINUX_FS_TYPE)
        self.assertEqual(sizes["boot"], "1GiB")
        # Root takes the rest, so the installer can grow it and shrinking can cut after it.
        self.assertEqual(types["root"], ROOT_X86_64_TYPE)
        self.assertIsNone(sizes["root"])

    def test_the_sfdisk_script_is_the_layout(self):
        script = medium.sfdisk_script().splitlines()
        self.assertEqual(script[0], "label: gpt")
        self.assertEqual(script[2], f'size=512MiB, type={ESP_TYPE}, name="EFI-SYSTEM"')
        self.assertEqual(script[-1], f'type={ROOT_X86_64_TYPE}, name="root"')

    def test_the_deployment_boots_into_the_graphical_splash(self):
        self.assertIn("rhgb", medium.INSTALL_KARGS)
        self.assertIn("quiet", medium.INSTALL_KARGS)
        self.assertNotIn("utah.live", medium.INSTALL_KARGS)

    def test_the_build_script_takes_both_from_medium_py(self):
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertIn('medium.py" layout | sfdisk', build)
        self.assertIn('medium.py" install-kargs', build)
        self.assertNotIn("label: gpt", build)
        self.assertNotIn("--karg rhgb", build)
        self.assertIn('mkfs.fat -F 32 -n EFI-SYSTEM "${LOOP}p2"', build)
        self.assertIn('mkfs.ext4 -q -L boot "${LOOP}p3"', build)
        self.assertIn('mkfs.btrfs -q -L root "${LOOP}p4"', build)


class ShrinkPlanTests(unittest.TestCase):
    def test_root_is_cut_to_the_filesystem_rounded_to_a_mebibyte(self):
        number, sectors, image = medium.shrink_plan(TABLE, 7 * 2**30 + 1)
        self.assertEqual(number, 4)
        self.assertEqual(sectors % 2048, 0)
        self.assertGreaterEqual(sectors * 512, 7 * 2**30 + 1)
        self.assertLess(sectors * 512, 7 * 2**30 + 2**20 + 1)
        self.assertEqual(image, (3149824 + sectors) * 512 + medium.GPT_TAIL_BYTES)

    def test_a_filesystem_larger_than_its_partition_is_an_error(self):
        with self.assertRaises(ValueError):
            medium.shrink_plan(TABLE, 47181790 * 512 + 2**20)

    @unittest.skipUnless(shutil.which("sfdisk"), "sfdisk not installed")
    def test_the_cut_medium_keeps_a_valid_gpt(self):
        """Run 44874576635: truncating without rewriting the GPT left no partitions."""
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp, "m.raw")
            with open(raw, "wb") as f:
                f.truncate(24 * 2**30)
            layout = ('label: gpt\nsize=1MiB, type=21686148-6449-6E6F-744E-656564454649\n'
                      'size=512MiB, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B\n'
                      'size=1GiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4\n'
                      'type=4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709, name="root"\n')
            run = lambda *a, **k: subprocess.run(["sfdisk", *a], check=True, capture_output=True, text=True, **k).stdout  # noqa: E731
            run("-q", str(raw), input=layout)
            number, sectors, size = medium.shrink_plan(json.loads(run("--json", str(raw))), 8 * 2**30)
            new = medium.shrunk_dump(run("--dump", str(raw)), number, sectors)
            with open(raw, "r+b") as f:
                f.truncate(size)
            run("-q", "--no-reread", "--wipe", "never", "--wipe-partitions", "never", str(raw), input=new)
            table = json.loads(run("--json", str(raw)))["partitiontable"]
            self.assertEqual(table["label"], "gpt")
            self.assertEqual(int(table["partitions"][-1]["size"]), sectors)
            self.assertLess(int(table["lastlba"]), size // 512)

    def test_shrunk_dump_needs_the_partition(self):
        with self.assertRaises(ValueError):
            medium.shrunk_dump("label: gpt\nx1 : start= 1, size= 2, type=a\n", 4, 8)

    def test_the_build_script_and_the_shrink_script_use_these_helpers(self):
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertIn('medium.py" live-entry', build)
        self.assertIn('medium.py" grubenv', build)
        shrink = (RAW / "shrink-raw-image.sh").read_text()
        self.assertIn('medium.py" shrink-plan', shrink)
        self.assertIn('medium.py" shrunk-dump', shrink)
        self.assertIn("--wipe-partitions never", shrink)
        self.assertNotIn("--relocate", shrink)

    def test_the_root_is_balanced_into_small_chunks_before_the_resize(self):
        shrink = (RAW / "shrink-raw-image.sh").read_text()
        self.assertIn('UTAH_RAW_SHRINK_CHUNK_MIB:-256', shrink)
        sysfs = shrink.index('allocation/${kind}/chunk_size"')
        self.assertLess(sysfs, shrink.index("btrfs balance start"))
        self.assertLess(shrink.index("btrfs balance start"), shrink.rindex("btrfs filesystem resize"))

    def test_the_root_is_trimmed_again_after_the_resize(self):
        # Chunks the resize empties below the cut keep their data in the
        # file until trimmed (run 44910214722).
        shrink = (RAW / "shrink-raw-image.sh").read_text()
        resize = shrink.rindex("btrfs filesystem resize")
        self.assertIn("fstrim -v /m", shrink[resize:shrink.index("btrfs inspect-internal dump-super")])

    def test_compression_rewrites_the_files_after_the_flatpak_bake(self):
        # ostree and flatpak fallocate their objects, and btrfs does not
        # compress writes into preallocated extents, so compress-force on the
        # install mount changed nothing; the shrink step rewrites the files.
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertNotIn("compress-force", build)
        shrink = (RAW / "shrink-raw-image.sh").read_text()
        defrag = shrink.index('btrfs filesystem defragment -r -c"${COMPRESS%%:*}" /m')
        self.assertLess(defrag, shrink.index("btrfs balance start"))
        workflow = (ROOT / ".github/workflows/image-installer.yml").read_text()
        bake = workflow.index("bash iso/raw/bake-flatpaks.sh")
        self.assertLess(bake, workflow.rindex('UTAH_RAW_BTRFS_COMPRESS="$BTRFS_COMPRESS"'))
        self.assertEqual(workflow.count('UTAH_RAW_BTRFS_COMPRESS="$BTRFS_COMPRESS"'), 2)

    def test_btrfs_compress_off_turns_compression_off_and_empty_is_the_default(self):
        # GitHub substitutes an input's default for an empty value, so runs
        # dispatched with btrfs_compress empty ran at zstd:3 (44938602008,
        # 44944477347): "off" is the explicit way out.
        workflow = yaml.safe_load(WORKFLOW.read_text())
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["btrfs_compress"]["default"], "zstd:3")
        job = workflow["jobs"]["e2e"]
        self.assertEqual(job["env"]["BTRFS_COMPRESS_INPUT"],
                         "${{ github.event_name == 'workflow_dispatch' && inputs.btrfs_compress || 'zstd:3' }}")
        self.assertNotIn("BTRFS_COMPRESS", job["env"])
        step = job["steps"][0]
        self.assertEqual(step["name"], "Resolve the btrfs compression")
        for given, want in (("off", ""), ("zstd:3", "zstd:3"), ("zstd:15", "zstd:15")):
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / "env"
                subprocess.run(["bash", "-c", step["run"]], check=True, capture_output=True,
                               env={"PATH": "/usr/bin:/bin", "BTRFS_COMPRESS_INPUT": given, "GITHUB_ENV": str(env_file)})
                self.assertEqual(env_file.read_text(), f"BTRFS_COMPRESS={want}\n", given)


class ResultsTests(unittest.TestCase):
    def test_stages_pass_fail_and_not_run(self):
        clock = iter(range(0, 1000, 10))
        results = raw_e2e.Results("gui", clock=lambda: next(clock))
        results.begin("live boot")
        results.begin("install")
        results.finish(RuntimeError("installer failed"))
        out = results.as_dict({"x": 1})
        self.assertEqual(out["result"], "fail")
        self.assertEqual(out["error"], "installer failed")
        status = {s["stage"]: s["status"] for s in out["stages"]}
        self.assertEqual(status["live boot"], "pass")
        self.assertEqual(status["install"], "fail")
        self.assertEqual(status["bootc rollback"], "not run")
        self.assertEqual([s["stage"] for s in out["stages"]], list(raw_e2e.STAGES))

    def test_all_stages_passed_is_a_pass(self):
        results = raw_e2e.Results("script")
        for stage in raw_e2e.STAGES:
            results.begin(stage)
        results.finish()
        self.assertEqual(results.as_dict()["result"], "pass")

    def test_unknown_stage_names_are_rejected(self):
        with self.assertRaises(ValueError):
            raw_e2e.Results("gui").begin("desktop")

    def test_failed_units_are_read_from_systemctl(self):
        text = "\u25cf bootc-generic-growpart.service loaded failed failed Grow root\nfoo.mount loaded failed failed x\n"
        self.assertEqual(raw_e2e.parse_failed_units(text), ["bootc-generic-growpart.service", "foo.mount"])
        self.assertEqual(raw_e2e.parse_failed_units(""), [])


class MeasureTests(unittest.TestCase):
    def test_sizes_and_pack_unpack_times_are_recorded_and_packs_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp, "medium.raw")
            with open(raw, "wb") as f:
                f.write(b"utah" * 4096)
                f.truncate(64 * 2**20)
            got = measure_medium.measure(raw, ["zstd-3"], "t")
            self.assertEqual(got["apparent_bytes"], 64 * 2**20)
            self.assertLess(got["allocated_bytes"], got["apparent_bytes"])
            self.assertLess(got["formats"]["zstd-3"]["bytes"], 2**20)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["medium.raw"])
            with self.assertRaises(ValueError):
                measure_medium.measure(raw, ["gzip"])


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


class InstalledSystemCheckTests(unittest.TestCase):
    """The installed-system checks raw_e2e.py took over from the fisherman harness (luks-e2e.sh)."""

    def test_image_name_is_the_repository_without_registry_tag_or_digest(self):
        for ref in ("ghcr.io/projectbluefin/utah@sha256:" + "a" * 64, "ghcr.io/projectbluefin/utah:testing",
                    "localhost:5000/utah:testing", "utah"):
            with self.subTest(ref=ref):
                self.assertEqual(raw_e2e.image_name(ref), "utah")

    def test_every_configured_flavor_is_found_from_its_image(self):
        flavors = json.loads((ROOT / "config/flavors.json").read_text())["flavors"]
        for flavor in flavors:
            image = subprocess.run([sys.executable, str(ROOT / "scripts/flavors.py"), "image", flavor],
                                   capture_output=True, text=True, check=True).stdout.strip()
            with self.subTest(flavor=flavor):
                self.assertEqual(raw_e2e.flavor_of(f"ghcr.io/projectbluefin/{image}@sha256:{'0' * 64}"), flavor)
        with self.assertRaises(SystemExit):
            raw_e2e.flavor_of("ghcr.io/projectbluefin/dakota:testing")

    def test_an_emergency_this_boot_is_seen_although_the_boot_carried_on(self):
        never = "InactiveExitTimestampMonotonic=0\nExecMainStartTimestamp=\n"
        self.assertFalse(raw_e2e.emergency_entered(never))
        self.assertFalse(raw_e2e.emergency_entered("InactiveExitTimestampMonotonic=0\nExecMainStartTimestamp=n/a\n"))
        self.assertTrue(raw_e2e.emergency_entered("InactiveExitTimestampMonotonic=8123456\nExecMainStartTimestamp=\n"))
        self.assertTrue(raw_e2e.emergency_entered(
            "InactiveExitTimestampMonotonic=0\nExecMainStartTimestamp=Thu 2026-11-26 10:00:01 UTC\n"))

    def test_only_a_denied_bootc_or_chcon_probe_fails_the_boot(self):
        denial = ('type=AVC msg=audit(1795247042.592:77): avc:  denied  { mac_admin } for  pid=2690 '
                  'comm="chcon" capability=33  scontext=system_u:system_r:initrc_t:s0 tclass=capability2 permissive=0')
        other = denial.replace('comm="chcon"', 'comm="logind"')
        self.assertEqual(raw_e2e.bootc_probe_denials(f"{other}\n{denial}\n"), [denial])
        self.assertEqual(raw_e2e.bootc_probe_denials(denial.replace("chcon", "bootc")), [denial.replace("chcon", "bootc")])
        self.assertEqual(raw_e2e.bootc_probe_denials(other), [])

    def test_the_flathub_remote_must_be_listed(self):
        self.assertTrue(raw_e2e.flathub_configured("fedora\nflathub\n"))
        self.assertFalse(raw_e2e.flathub_configured("flathub-beta\nfedora\n"))
        self.assertFalse(raw_e2e.flathub_configured(""))

    def test_rounded_blur_runtime_present_and_devel_absent(self):
        self.assertEqual(raw_e2e.rounded_blur_problems(0, 1), [])
        self.assertEqual(raw_e2e.rounded_blur_problems(1, 1),
                         ["installed image lacks the gnome-rounded-blur runtime package"])
        self.assertEqual(raw_e2e.rounded_blur_problems(0, 0),
                         ["build-only gnome-rounded-blur-devel leaked into the installed image"])
        self.assertEqual(raw_e2e.rounded_blur_problems(0, 2), ["could not verify gnome-rounded-blur-devel is absent"])

    def test_extension_state_is_decided_by_extension_state_sh(self):
        for info, expected in (("Name: GSConnect\n  State: ACTIVE\n", ("ACTIVE", True)),
                               ("State: ENABLED\n", ("ENABLED", True)),
                               ("State: ACTIVE\nState: ERROR\n", ("ERROR", False)),
                               ("State: OUT_OF_DATE\n", ("OUT_OF_DATE", False)),
                               ("ssh root: failed (1)\n", ("", False))):
            with self.subTest(info=info):
                self.assertEqual(raw_e2e.extension_state(info), expected)

    def test_fastfetch_is_read_back_by_fastfetch_ocr_match_sh(self):
        with tempfile.TemporaryDirectory() as d:
            transcript = Path(d) / "ocr.txt"
            transcript.write_text("root@utah:~# fastfetch && echo UTAH-E2E-FASTFETCH\n"
                                  "Utah (Version: testing-20261126)\nLinux 7.3.5-200.1.utah1.fc44.x86_64\n"
                                  "GNOME 51.1\nUTAH-E2E-FASTFETCH\n")
            self.assertTrue(raw_e2e.fastfetch_seen(transcript))
            # The sentinel in scrollback alone is not fastfetch on screen.
            transcript.write_text("UTAH-E2E-FASTFETCH\n[tester@utah ~]$\n")
            self.assertFalse(raw_e2e.fastfetch_seen(transcript))
            transcript.write_text("")
            self.assertFalse(raw_e2e.fastfetch_seen(transcript))
        self.assertTrue(raw_e2e.welcome_dialog_showing("Welcome to Bluefin\nSkip  Take Tour\n"))
        self.assertFalse(raw_e2e.welcome_dialog_showing("Linux 7.3"))

    def test_a_frame_with_only_the_top_bar_drawn_is_not_drawn_below_it(self):
        w, h = 200, 100
        top = bytes(b for i in range(w * (h // 10)) for b in (i % 251, (i * 7) % 253, (i * 13) % 255))
        frame = b"P6\n%d %d\n255\n" % (w, h) + top + bytes(3 * w * (h - h // 10))
        with tempfile.TemporaryDirectory() as d:
            ppm = Path(d) / "f.ppm"
            ppm.write_bytes(frame)
            self.assertTrue(raw_e2e.drawn(ppm))
            self.assertFalse(raw_e2e.drawn(ppm, below_top_bar=True))
            ppm.write_bytes(b"P6\n%d %d\n255\n" % (w, h) + top * 10)
            self.assertTrue(raw_e2e.drawn(ppm, below_top_bar=True))

    def run_setup_state(self, failed_unit, disabled="kanata.service"):
        """setup_state_script against e2e-services.sh, with systemctl, sudo and the contract parser stubbed."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "scripts").mkdir()
            (d / "bin").mkdir()
            shutil.copy(ROOT / "iso/scripts/e2e-services.sh", d / "e2e-services.sh")
            (d / "scripts/verify-desktop-contract.py").write_text(
                'import sys\nprint({"enabled": "gdm.service", "user_enabled": "pipewire.socket", '
                f'"disabled": {disabled!r}}}[sys.argv[2]])\n')
            (d / "bin/sudo").write_text('#!/bin/bash\nwhile [[ $1 == -* ]]; do [[ $1 == -p ]] && shift; shift; done\n'
                                        'cat >/dev/null\nexec "$@"\n')
            (d / "bin/systemctl").write_text(
                '#!/bin/bash\ncase "$*" in\n  *is-enabled*) echo enabled ;;\n  *list-units*) ;;\n'
                '  *"$FAILED_UNIT"*) printf "LoadState=loaded\\nActiveState=failed\\nResult=exit-code\\n'
                'ExecMainStartTimestamp=n/a\\n" ;;\n'
                '  *) printf "LoadState=loaded\\nActiveState=active\\nResult=success\\n'
                'ExecMainStartTimestamp=Thu 2026-11-26 10:00:00 UTC\\n" ;;\nesac\n')
            for stub in ("sudo", "systemctl"):
                (d / "bin" / stub).chmod(0o755)
            self.addCleanup(Path("/tmp/utah-e2e-verify-desktop-contract.py").unlink, missing_ok=True)
            original = raw_e2e.GUEST_SCRIPTS
            raw_e2e.GUEST_SCRIPTS = str(d)
            try:
                script = raw_e2e.setup_state_script("repeat")
            finally:
                raw_e2e.GUEST_SCRIPTS = original
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  env={"PATH": f"{d / 'bin'}:/usr/bin:/bin", "FAILED_UNIT": failed_unit})

    def test_the_contract_units_are_checked_by_e2e_services_sh_in_the_guest(self):
        ok = self.run_setup_state("none")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        for unit in ("flatpak-preinstall.service", "ublue-system-setup.service", "ublue-user-setup.service",
                     "gdm.service", "pipewire.socket", "kanata.service"):
            self.assertIn(unit, ok.stdout)
        failed = self.run_setup_state("gdm.service")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("FAIL: system unit gdm.service failed during repeat boot", failed.stderr)

    def test_kanata_is_checked_only_when_the_installed_contract_lists_it(self):
        # A :stable built before kanata (#1297) has no unit and no contract
        # entry; the publish run of that image must not fail on it.
        old = self.run_setup_state("none", disabled="")
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertNotIn("kanata.service", old.stdout)
        broken = self.run_setup_state("kanata.service")
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("FAIL: system unit kanata.service failed during repeat boot", broken.stderr)

    def test_the_reused_fisherman_checks_exist_where_the_harness_reads_them(self):
        # raw_e2e.py runs these from iso/scripts/: removing the ISO path must keep them.
        for script in ("e2e-services.sh", "verify-installed-flavor.py", "verify-countme-runtime.sh",
                       "extension-state.sh", "fastfetch-ocr-match.sh"):
            self.assertTrue((raw_e2e.SCRIPTS / script).is_file(), script)
        self.assertTrue((raw_e2e.REPO / "scripts/verify-desktop-contract.py").is_file())


class KernelVeritySelectorTests(unittest.TestCase):
    """scripts/kernel_verity.py: composefs only on a kernel with the btrfs fs-verity fix."""

    def decide(self, release, changelog="", config=""):
        return kernel_verity.decide(release, changelog, config)[0]

    def test_fixed_kernels(self):
        self.assertTrue(self.decide("7.3.3-200.1.utah1.fc44.x86_64"))  # Utah's rebuild (main, nvidia)
        self.assertTrue(self.decide("7.1.8-ogc1"))  # gaming: before large data folios
        self.assertTrue(self.decide("7.3.9-200.fc44.x86_64", "- btrfs: fix the fsverity callback where "
                                    "unexpected range is verified (Qu Wenruo)"))
        self.assertTrue(self.decide("7.3.9-200.fc44.x86_64", "- Backport 254e15591ee4"))
        self.assertTrue(self.decide("7.4.0-0.rc2.24.fc45.x86_64"))
        self.assertTrue(self.decide("7.4.1-100.fc45.x86_64"))
        self.assertTrue(self.decide("7.5.0-1.fc45.x86_64"))

    def test_unfixed_kernels(self):
        self.assertFalse(self.decide("7.3.3-200.fc44.x86_64"))  # Fedora's signed kernel today (#1379)
        self.assertFalse(self.decide("7.2.15-200.fc44.x86_64", "- btrfs: something else"))
        self.assertFalse(self.decide("7.4.0-0.rc1.20261012git1.fc45.x86_64"))
        self.assertFalse(self.decide("7.4.0-rc1"))
        self.assertFalse(self.decide("7.1.8-ogc1", config="CONFIG_BTRFS_FS=m\nCONFIG_BTRFS_EXPERIMENTAL=y\n"))
        self.assertFalse(self.decide("weird"))

    def test_auto_picks_composefs_only_on_a_fixed_kernel(self):
        fixed = kernel_verity.select("auto", "7.3.3-200.1.utah1.fc44.x86_64", True, "patched")
        self.assertEqual((fixed["backend"], fixed["requested"]), ("composefs", "auto"))
        unfixed = kernel_verity.select("auto", "7.3.3-200.fc44.x86_64", False, "unpatched")
        self.assertEqual(unfixed["backend"], "ostree")
        self.assertEqual(kernel_verity.select("ostree", "7.3.3-200.1.utah1.fc44.x86_64", True, "")["backend"], "ostree")
        self.assertEqual(kernel_verity.select("composefs", "7.1.8-ogc1", True, "")["backend"], "composefs")
        with self.assertRaises(kernel_verity.Unfixed):
            kernel_verity.select("composefs", "7.3.3-200.fc44.x86_64", False, "unpatched")

    def image_root(self, d, release, config=None, ogc=False):
        tree = Path(d) / "usr/lib/modules" / release
        tree.mkdir(parents=True)
        (tree / "vmlinuz").write_bytes(b"\0")
        if config is not None:
            target = Path(d) / "usr/lib/utah/ogc-kernel.config" if ogc else tree / "config"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(config)
        if ogc:
            (Path(d) / "usr/lib/utah").mkdir(parents=True, exist_ok=True)
            (Path(d) / "usr/lib/utah/ogc-kernel-release").write_text(release + "\n")
        return d

    def run_select(self, root, backend):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = kernel_verity.main(["select", "--backend", backend, "--root", root])
        return code, json.loads(out.getvalue()) if out.getvalue() else None

    def test_the_cli_reads_the_image_kernel_and_config(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.image_root(d, "7.1.8-ogc1", "CONFIG_BTRFS_EXPERIMENTAL=y\n", ogc=True)
            self.assertEqual(self.run_select(root, "auto")[1]["backend"], "ostree")
            self.assertEqual(self.run_select(root, "composefs"), (kernel_verity.EXIT_UNFIXED, None))
        with tempfile.TemporaryDirectory() as d:
            root = self.image_root(d, "7.3.3-200.1.utah1.fc44.x86_64", "CONFIG_BTRFS_FS=y\n")
            code, decision = self.run_select(root, "auto")
            self.assertEqual((code, decision["backend"], decision["fixed"]), (0, "composefs", True))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "usr/lib/modules/7.3.3-200.fc44.x86_64").mkdir(parents=True)
            self.image_root(d, "7.1.8-ogc1", ogc=True)
            # Only the tree with a vmlinuz is the kernel bootc deploys.
            self.assertEqual(kernel_verity.booted_kernel(Path(d)), "7.1.8-ogc1")

    def test_the_medium_build_consults_it(self):
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertIn('<"${HERE}/../../scripts/kernel_verity.py"', build)


class GnomeSetupTests(unittest.TestCase):
    """gnome-setup drives systemd-sysinstall on the live medium."""

    workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_the_live_karg_starts_the_installer(self):
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertIn("utah.live.installer=gnome-setup", build)
        for unit in ("utah-sysinstall.socket", "utah-cryptenroll.socket"):
            text = (RAW / "src/usr/lib/systemd/system" / unit).read_text()
            self.assertIn("ConditionKernelCommandLine=utah.live.installer=gnome-setup", text)

    def test_sysinstall_leaves_the_copied_boot_chain_alone(self):
        service = (RAW / "src/usr/lib/systemd/system/utah-sysinstall@.service").read_text()
        self.assertIn("--install-kernel=no --install-boot-loader=no", service)
        self.assertIn("SYSTEMD_REPART_PATH=/usr/libexec/utah-sysinstall/systemd-repart", service)

    def test_the_post_install_unit_is_the_one_gnome_setup_starts(self):
        pages = (RAW / "src/usr/share/utah/gnome-setup/pages.conf").read_text()
        self.assertIn("post-install-unit=utah-sysinstall-post@.service", pages)
        self.assertTrue((RAW / "src/usr/lib/systemd/system/utah-sysinstall-post@.service").exists())
        rules = (RAW / "src/usr/share/polkit-1/rules.d/50-utah-live-gnome-setup.rules").read_text()
        self.assertIn("utah-sysinstall-post@", rules)
        containerfile = (RAW / "Containerfile").read_text()
        self.assertIn("ln -s /run/utah-live/repart.d /out/usr/lib/gnome-setup/definitions", containerfile)
        self.assertIn("/usr/share/utah/gnome-setup/pages.conf /usr/lib/gnome-setup/pages.conf", containerfile)

    def test_without_a_tpm_the_user_picks_a_passphrase_next_to_the_recovery_key(self):
        pages = (RAW / "src/usr/share/utah/gnome-setup/pages.conf").read_text()
        flow = next(line for line in pages.splitlines() if line.startswith("live-initial-setup="))
        steps = flow.split("=", 1)[1].split(";")
        self.assertLess(steps.index("installer"), steps.index("disk-passphrase"))
        self.assertLess(steps.index("disk-passphrase"), steps.index("recovery-key"))
        inputs = self.workflow[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["tpm"]["type"], "boolean")
        self.assertTrue(inputs["tpm"]["default"])
        self.assertIn('[[ "$TPM" != true ]] || tpm=(--tpm)', WORKFLOW.read_text())

    def test_the_live_session_names_the_product_as_dakota_does(self):
        setup = (RAW / "src/usr/libexec/utah-live-setup").read_text()
        self.assertNotIn("Install Utah", setup)
        self.assertIn("\nName=Install Bluefin\n", setup)
        # The sed line itself, over a desktop file shaped like upstream's.
        line = setup[setup.index("sed -e '/^Name"):setup.index("# end of desktop entry")]
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "src.desktop", Path(d) / "dst.desktop"
            src.write_text("[Desktop Entry]\nName[de]=Einrichtung\nName=Setup\nExec=gnome-setup\n")
            line = line.replace("/usr/share/utah/gnome-setup/org.gnome.Setup.desktop", str(src))
            line = line.replace("/usr/local/share/applications/org.gnome.Setup.desktop", str(dst))
            subprocess.run(["bash", "-euc", line], check=True)
            self.assertEqual(dst.read_text(), "[Desktop Entry]\nName=Install Bluefin\n"
                             "Comment=Install Bluefin on this computer\nExec=gnome-setup\n")

    def test_installed_systems_do_not_offer_gnome_setup(self):
        containerfile = (RAW / "Containerfile").read_text()
        self.assertIn("mv /out/usr/share/applications/org.gnome.Setup.desktop", containerfile)
        self.assertIn("rm -rf /out/usr/lib/systemd/user", containerfile)

    def test_results_record_the_installer_and_encryption(self):
        results = raw_e2e.Results("gui")
        results.installer = "gnome-setup"
        out = results.as_dict()
        self.assertEqual(out["installer"], "gnome-setup")
        self.assertIn("encryption", out)

    def test_wipe_or_free_takes_free_space_without_a_confirm_dialog(self):
        # The empty target reads as DataDetected ('Existing Data Found'), so the
        # harness takes 'Install to Free Space' and starts installation directly;
        # full wipe would ask 'Erase and Install' instead (run 44968690146).
        harness = (RAW / "raw_e2e.py").read_text()
        self.assertIn('"Existing Data Found"', harness)
        self.assertIn('"Install to Free Space"', harness)
        self.assertIn('"Start Installation"', harness)
        self.assertIn('"Install"', harness)

    def test_recovery_key_read_from_label_shaped_like_a_key(self):
        # Run 44972858422's atspi-timeout-recovery-key.json: the key label's
        # accessible name IS the key, so named(nodes, 'Recovery Key') misses it.
        key = "reclhuld-icvnrelc-kjtnebnh-ucghgbnn-kffbvtuf-gbeuntuv-ukbkrgrk-kvhcerjg"
        nodes = [
            {"path": "0.1", "role": "label", "name": "Back Up Recovery Key",
             "text": "Back Up Recovery Key", "ext": [0, 0, 10, 10], "states": []},
            {"path": "0.2", "role": "label", "name": key, "text": key,
             "ext": [0, 0, 10, 10], "states": ["sensitive", "showing", "visible"]},
        ]
        self.assertIsNone(raw_e2e.named(nodes, "Recovery Key"))
        self.assertEqual(raw_e2e.Harness.recovery_key_text(nodes), key)

    def test_done_predicate_ignores_panel_before_checkbox_tick(self):
        # Run 44972858422's atspi-timeout-recovery-key.json: three nodes share
        # the name 'Done' (sensitive panel, insensitive button, sensitive
        # label). Without a role filter the predicate is already true before
        # 'I backed up my recovery key' is ticked, and act may target the panel.
        panel = {"path": "0.0.0.0.0.0.0", "role": "panel", "name": "Done",
                 "text": "", "ext": [0, 0, 1920, 977],
                 "states": ["sensitive", "showing", "visible"]}
        button = {"path": "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.1.2",
                  "role": "button", "name": "Done", "text": "",
                  "ext": [909, 699, 102, 44],
                  "states": ["focusable", "showing", "visible"]}
        label = {"path": "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.1.2.0",
                 "role": "label", "name": "Done", "text": "Done",
                 "ext": [909, 699, 38, 24],
                 "states": ["sensitive", "showing", "visible"]}
        nodes = [panel, button, label]
        self.assertTrue(raw_e2e.has_name(nodes, "Done", sensitive=True))
        self.assertFalse(raw_e2e.has_name(nodes, "Done", sensitive=True, role="button"))
        self.assertIsNone(raw_e2e.named(nodes, "Done", sensitive=True, role="button"))
        ticked = [dict(button, states=button["states"] + ["sensitive"])]
        self.assertTrue(raw_e2e.has_name(ticked, "Done", sensitive=True, role="button"))
        self.assertEqual(raw_e2e.named(ticked, "Done", sensitive=True, role="button")["role"], "button")

    def test_qr_dialog_done_is_the_dialogs_button(self):
        # Run 44978556102's atspi-timeout-qr-code-dialog-closed.json: with the QR
        # dialog open the page has a sensitive panel 'Done' (the whole window),
        # the still-insensitive main button 'Done' behind the modal, and the
        # dialog's own sensitive button 'Done'. The press must land on the last.
        nodes = [
            {"path": "0", "role": "panel", "name": "Done", "text": "",
             "ext": [0, 0, 1920, 977], "states": ["sensitive", "showing"]},
            {"path": "0.1", "role": "button", "name": "Done", "text": "",
             "ext": [909, 699, 102, 44], "states": ["focusable", "showing"]},
            {"path": "0.2", "role": "dialog", "name": "QR Code", "text": "",
             "ext": [0, 0, 1920, 977], "states": ["sensitive", "showing"]},
            {"path": "0.2.0", "role": "button", "name": "Done", "text": "",
             "ext": [808, 619, 304, 44], "states": ["focusable", "sensitive", "showing"]},
        ]
        self.assertEqual(raw_e2e.named(nodes, "Done")["role"], "panel")  # the old pick
        picked = raw_e2e.named(nodes, "Done", sensitive=True, role="button")
        self.assertEqual(picked["ext"], [808, 619, 304, 44])

    def test_setup_done_press_ignores_panels_and_insensitive_buttons(self):
        from unittest.mock import Mock
        harness = object.__new__(raw_e2e.Harness)
        harness.atspi = Mock(return_value={"how": "action:click"})
        nodes = [
            {"name": "Done", "role": "panel", "states": ["sensitive"]},
            {"name": "Done", "role": "button", "states": []},
            {"name": "Done", "role": "button", "states": ["sensitive"]},
        ]
        harness.setup_press(None, nodes, "Done")
        harness.atspi.assert_called_once_with(None, "act", raw_e2e.GS_APP, "Done", "button")

    def test_timeout_summary_lists_buttons_and_labels(self):
        nodes = [
            {"path": "0.1", "role": "button", "name": "Install",
             "text": "", "ext": [0, 0, 1, 1], "states": ["sensitive", "showing"]},
            {"path": "0.2", "role": "panel", "name": "",
             "text": "", "ext": [0, 0, 1, 1], "states": []},
        ]
        lines = raw_e2e.summarize_nodes(nodes)
        self.assertEqual(len(lines), 1)
        self.assertIn("button", lines[0])
        self.assertIn("'Install'", lines[0])
        self.assertIn("showing=True", lines[0])

    def test_disk_rows_match_despite_glib_nbsp_size(self):
        # glib's format_size separates value and unit with a non-breaking space
        # (CI run 44964323210: 'Unknown Disk 51.5\\xa0GB'); the harness matches
        # with a plain space.
        nodes = [{
            "path": "0.1", "role": "table row",
            "name": "Unknown Disk 51.5\u00a0GB", "text": "",
            "ext": [0, 0, 100, 20], "states": [],
        }]
        self.assertTrue(raw_e2e.has_text(nodes, "51.5 GB"))
        row = raw_e2e.row_containing(nodes, raw_e2e.TARGET_SIZE_TEXT)
        self.assertIsNotNone(row)
        self.assertEqual(row["role"], "table row")


if __name__ == "__main__":
    unittest.main()
