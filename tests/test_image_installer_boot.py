"""Image-installer track tests: Live entry, grubenv, GRUB menu, medium contract, shrink plan, results and measure checks.

Split from the former single tests/test_image_installer.py; see docs/adr/0001 and iso/raw/.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


# tesseract's transcripts of an early boot frame: shim's fallback as Post-Testing
# run 45918946265's 00-grub-menu.png showed it, and GRUB's menu on the medium
# as image-installer run 45935827310's debug/grub-probe.png showed it.
SHIM_FALLBACK_OCR = "Boot Option Restoration\n\nPress any key to stop system reset\n\nBooting in 2 seconds\n"
GRUB_MENU_OCR = ("GRUB version 2.12\n\nUtah Live (try or install)\nBluefin Wersion: test ing-20261127-2de3690)\n"
                 "UEFI Firmware Settings\n\nUse the 4 and ¥ keys to select uhich entry is highlighted.\n"
                 "Press enter to boot the selected 0S, \u201ce\u2019 to edit the conmands before booting or "
                 "\u201cc\u2019 for a command-line. ESC to return previous menu.\n")


class GrubMenuTests(unittest.TestCase):
    """Stage 1 proves firmware -> shim -> GRUB on the medium, not shim's fallback."""

    def test_grub_menu_frame_reads_as_grub(self):
        self.assertEqual(raw_e2e.boot_frame(GRUB_MENU_OCR), "grub")
        # tesseract may break the title across lines or change its case.
        self.assertEqual(raw_e2e.boot_frame(GRUB_MENU_OCR.replace("Utah Live", "UTAH\nLIVE")), "grub")

    def test_grub_menu_two_colour_frame_is_read_back(self):
        # Grey text on black: run 45935827310 skipped every GRUB frame as undrawn at 3 colours.
        w, h = 200, 100
        text = bytes(b for i in range(w * h) for b in ((0x98,) * 3 if i % 7 == 0 else (0,) * 3))
        with tempfile.TemporaryDirectory() as d:
            ppm = Path(d) / "grub.ppm"
            ppm.write_bytes(b"P6\n%d %d\n255\n" % (w, h) + text)
            self.assertTrue(raw_e2e.drawn(ppm, min_colors=raw_e2e.GRUB_MIN_COLORS))
            ppm.write_bytes(b"P6\n%d %d\n255\n" % (w, h) + bytes(3 * w * h))
            self.assertFalse(raw_e2e.drawn(ppm, min_colors=raw_e2e.GRUB_MIN_COLORS))

    def test_grub_shim_fallback_frame_reads_as_fallback(self):
        self.assertEqual(raw_e2e.boot_frame(SHIM_FALLBACK_OCR), "fallback")
        self.assertEqual(raw_e2e.boot_frame("Press any key to stop\nsystem reset\n"), "fallback")

    def test_grub_absent_from_firmware_or_kernel_frames(self):
        for ocr in ("", "TianoCore\n", GRUB_MENU_OCR.replace("Utah Live (try or install)\n", ""),
                    "[  OK  ] Started utah-live-root.service - Utah live root overlay.\n"):
            with self.subTest(ocr=ocr):
                self.assertEqual(raw_e2e.boot_frame(ocr), "other")

    def test_without_kvm_ci_fails_and_a_local_run_falls_back_to_tcg(self):
        # A vanished KVM grant made the candidate's installed VM run under TCG and
        # time out into emergency mode (run 45950837602); CI must say so up front.
        self.assertEqual(raw_e2e.accel_args(True, ci=True), ["-accel", "kvm", "-cpu", "host"])
        with self.assertRaisesRegex(raw_e2e.Fail, "/dev/kvm is not writable"):
            raw_e2e.accel_args(False, ci=True)
        with mock.patch.object(raw_e2e, "log") as log:
            self.assertEqual(raw_e2e.accel_args(False, ci=False), ["-accel", "tcg"])
        log.assert_called_once()

    def test_grub_live_text_is_the_live_entry_title(self):
        self.assertTrue(medium.LIVE_TITLE.lower().startswith(raw_e2e.GRUB_LIVE_TEXT))

    def test_grub_countdown_is_held_by_a_key_the_menu_ignores(self):
        # GRUB's menu maps these keys to moves, edits or a boot (normal/menu.c).
        self.assertNotIn(raw_e2e.GRUB_HOLD_KEY, ("ret", "esc", "e", "c", "v", "up", "down", "home", "end",
                                                 "pgup", "pgdn", "right"))

    def test_grub_build_drops_shim_fallback_from_the_medium_esp(self):
        build = (RAW / "build-raw-image.sh").read_text()
        delete = build.index("-iname fbx64.efi -print -delete")
        listing = build.index('tee "${EVIDENCE}/esp-files.txt"')
        self.assertLess(delete, listing)
        self.assertIn("grep -qix '/EFI/BOOT/fbx64.efi' \"${EVIDENCE}/esp-files.txt\"", build)
        self.assertIn('cp "${bootupd_state}" "${EVIDENCE}/bootupd-state.json"', build)

    def test_grub_build_drops_shim_fallback_from_bootupd_state(self):
        build = (RAW / "build-raw-image.sh").read_text()
        script = build.split("<<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
        entry = {"size": 1, "sha512": "sha512:00"}
        state = {"installed": {"EFI": {"meta": {"version": "grub2-efi-x64-1:2.12-66.fc44,shim-x64-16.1-7"},
                                       "filetree": {"children": {"BOOT/BOOTX64.EFI": entry,
                                                                 "BOOT/fbx64.efi": entry,
                                                                 "fedora/shimx64.efi": entry}}}}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bootupd-state.json"
            path.write_text(json.dumps(state))
            subprocess.run([sys.executable, "-", str(path)], input=script, text=True, check=True)
            children = json.loads(path.read_text())["installed"]["EFI"]["filetree"]["children"]
        self.assertEqual(sorted(children), ["BOOT/BOOTX64.EFI", "fedora/shimx64.efi"])


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
