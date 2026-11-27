"""Image-installer track tests: the hybrid ISO (#1404).

The raw medium as an El Torito hybrid with shim -> systemd-boot on its ESP
(iso/raw/build-hybrid-iso.sh), its CD-ROM/USB E2E legs (iso/raw/raw_e2e.py
--iso/--boot-as) and the installed ESP's restore (utah-sysinstall-post).
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

ENTRY = """title Fedora Linux 44 (Utah) (ostree:0)
version 1
options root=UUID=8f3e rw boot=UUID=1b2c rhgb quiet ostree=/ostree/boot.1/default/abc/0
linux /ostree/default-abc/vmlinuz-7.0
initrd /ostree/default-abc/initramfs-7.0.img
"""


def eltorito_image(rba, platform=0xEF, boot=0x88):
    """The first 64 KiB of an image as systemd-repart --el-torito writes them:
    primary descriptor (block 16), El Torito boot record (17) naming the
    catalog at block 19, terminator (18), validation and initial entry (19)."""
    head = bytearray(medium.ISO9660_HEAD)
    block = 2048
    head[16 * block:16 * block + 7] = b"\x01CD001\x01"
    rec = 17 * block
    head[rec:rec + 7] = b"\x00CD001\x01"
    head[rec + 7:rec + 30] = b"EL TORITO SPECIFICATION"
    head[rec + 0x47:rec + 0x4B] = (19).to_bytes(4, "little")
    head[18 * block:18 * block + 7] = b"\xffCD001\x01"
    cat = 19 * block
    head[cat] = 1
    head[cat + 1] = platform
    head[cat + 30:cat + 32] = b"\x55\xaa"
    head[cat + 32] = boot
    head[cat + 40:cat + 44] = rba.to_bytes(4, "little")
    return bytes(head)


# tesseract's transcripts: systemd-boot's menu on the hybrid ISO, GRUB's menu
# on the raw medium (PR #1619), and a kernel console line naming a live unit.
SDBOOT_MENU_OCR = "Utah Live (try or install)\nReboot Into Firmware Interface\n\nBoot in 4s.\n"
GRUB_MENU_OCR_RAW = ("GNU GRUB version 2.12\n\nUtah Live (try or install)\nUEFI Firmware Settings\n\n"
                     "Use the * and v keys to select which entry is highlighted.\n")
LIVE_UNIT_OCR = "[  OK  ] Finished utah-live-root.service - Utah live: tmpfs overlay over the medium root.\n"
# What a CD-ROM boot of the hybrid ISO reported in a local VM: the loop device
# attached in the initrd names its backing device relative to the initrd's /dev.
CDROM_FACTS = {"loader": "systemd-boot 261.2-1.1.hum1", "loader_part_uuid": "82D819E3-81A7-4A05-A582-97E7ECCBE210",
               "root_part": "/dev/loop0p4", "root_disk": "/dev/loop0", "root_tran": "",
               "loop_backing": "/sr0", "loop_devices": "/dev/loop1:/ostree/deploy/default/deploy/b5fb.0/.ostree.cfs /dev/loop0:/sr0"}
USB_FACTS = {"loader": "systemd-boot 261.2-1.1.hum1", "loader_part_uuid": "80a68949-9ded-4b69-8867-c263b5e6e972",
             "root_part": "/dev/sda4", "root_disk": "/dev/sda", "root_tran": "usb",
             "loop_backing": "", "loop_devices": ""}


class HybridIsoTests(unittest.TestCase):
    """#1404: the raw medium as an El Torito hybrid ISO, systemd-boot on its ESP (Secure Boot off)."""

    workflow = yaml.safe_load(WORKFLOW.read_text())
    build = (RAW / "build-hybrid-iso.sh").read_text()

    def test_the_esp_entry_is_the_live_entry_booting_copies_on_the_esp(self):
        live = medium.live_entry(ENTRY, ["utah.live.installer=gnome-setup"])
        entry, files = medium.iso_entry(live)
        self.assertEqual(files, [("/utah-live/vmlinuz-7.0", "/ostree/default-abc/vmlinuz-7.0"),
                                 ("/utah-live/initramfs-7.0.img", "/ostree/default-abc/initramfs-7.0.img")])
        self.assertIn("linux /utah-live/vmlinuz-7.0\n", entry)
        self.assertIn("initrd /utah-live/initramfs-7.0.img\n", entry)
        options = lambda text: next(l for l in text.splitlines() if l.startswith("options "))  # noqa: E731
        self.assertEqual(options(entry), options(live))
        self.assertIn(f"title {medium.LIVE_TITLE}\n", entry)

    def test_an_entry_without_the_live_karg_or_with_clashing_files_is_rejected(self):
        with self.assertRaises(ValueError):
            medium.iso_entry(ENTRY)
        live = medium.live_entry(ENTRY + "initrd /other/initramfs-7.0.img\n")
        with self.assertRaises(ValueError):
            medium.iso_entry(live)

    def test_loader_conf_defaults_to_the_live_entry_with_a_visible_menu(self):
        conf = dict(line.split(" ", 1) for line in medium.iso_loader_conf().splitlines())
        self.assertEqual(conf["default"], "utah-live.conf")
        self.assertGreater(int(conf["timeout"]), 0)
        # The harness reads "Reboot Into Firmware Interface" off the menu.
        self.assertEqual(conf["auto-firmware"], "yes")
        self.assertIn('LIVE_ENTRY=utah-live\n', self.build)

    def test_the_el_torito_catalog_is_read_back(self):
        self.assertEqual(medium.eltorito_boot_rba(eltorito_image(1024)), 1024)
        for bad in (eltorito_image(1024, platform=0), eltorito_image(1024, boot=0), bytes(medium.ISO9660_HEAD)):
            with self.subTest(bad=bad[19 * 2048:19 * 2048 + 2]), self.assertRaises(ValueError):
                medium.eltorito_boot_rba(bad)

    def test_the_build_uses_the_mediums_own_repart_and_systemd_boot(self):
        self.assertIn("# Host tools:", self.build)
        self.assertNotRegex(self.build, r"\b(podman|xorriso|mtools|mcopy)\b")
        self.assertIn("--el-torito=yes", self.build)
        self.assertIn('chroot "$d" systemd-repart', self.build)
        self.assertIn("usr/lib/systemd/boot/efi/systemd-bootx64.efi", self.build)
        self.assertIn('medium.py" iso-entry', self.build)
        self.assertIn('medium.py" eltorito-rba', self.build)
        # Nothing but the ESP and the El Torito area may change.
        self.assertIn('diff <(table "${RAW}") <(table "${partial}")', self.build)
        self.assertIn('partition ${n} differs from the raw medium', self.build)
        self.assertIn('"${EVIDENCE}/iso.json"', self.build)
        self.assertIn('"iso_bytes": %s, "build_seconds": %s', self.build)

    def test_the_installed_esp_gets_shim_back(self):
        post = (RAW / "src/usr/libexec/utah-sysinstall-post").read_text()
        self.assertIn(f"ISO_ESP_DIR={medium.ISO_ESP_DIR}\n", post)
        self.assertIn('mv -f "${iso_dir}/BOOTX64.EFI" "${work}/mnt/efi/EFI/BOOT/BOOTX64.EFI"', post)
        self.assertIn('rm -rf "${iso_dir}" "${work}/mnt/efi/loader"', post)
        # Before the boot-config rewrite and the firmware entry.
        self.assertLess(post.index('iso_dir="${work}/mnt/efi/${ISO_ESP_DIR}"'), post.index('entry_dir="${bootdir}/loader/entries"'))
        self.assertEqual(raw_e2e.target_esp_problems(["EFI/BOOT/BOOTX64.EFI", "EFI/fedora/shimx64.efi"]), [])
        self.assertEqual(raw_e2e.target_esp_problems(["EFI/BOOT/BOOTX64.EFI", "loader/loader.conf", "utah-live/vmlinuz"]),
                         ["live boot files left: loader/loader.conf utah-live/vmlinuz"])
        self.assertEqual(raw_e2e.target_esp_problems(["EFI/fedora/shimx64.efi"]), ["no EFI/BOOT/BOOTX64.EFI"])

    def test_the_live_initramfs_attaches_the_cdrom_partitions_once(self):
        dracut = (RAW / "src/usr/lib/dracut/dracut.conf.d/60-utah-live.conf").read_text()
        containerfile = (RAW / "Containerfile").read_text()
        for path in ("/usr/bin/systemd-dissect", "/usr/lib/systemd/system/systemd-loop@.service",
                     "/usr/lib/systemd/system/systemd-loop@.service.d/50-utah-live.conf"):
            self.assertIn(path, dracut)
            self.assertIn(path.lstrip("/"), containerfile)
        dropin = (RAW / "src/usr/lib/systemd/system/systemd-loop@.service.d/50-utah-live.conf").read_text()
        self.assertIn("ConditionPathExists=|/etc/initrd-release", dropin)
        self.assertIn("ConditionKernelCommandLine=|!utah.live", dropin)

    def test_the_medium_is_attached_as_the_leg_asks(self):
        raw = raw_e2e.medium_drive("/w/medium.raw")
        self.assertIn("virtio-blk-pci", raw["device"])
        cd = raw_e2e.medium_drive("/w/medium.raw", "/w/medium.iso", "cdrom")
        self.assertIn("file=/w/medium.iso", cd["drive"])
        self.assertIn("media=cdrom", cd["drive"])
        self.assertTrue(cd["device"].startswith("ide-cd,"))
        usb = raw_e2e.medium_drive("/w/medium.raw", "/w/medium.iso", "usb")
        self.assertTrue(usb["device"].startswith("usb-storage,"))
        for d in (raw, cd, usb):
            self.assertIn("readonly=on", d["drive"])
            self.assertIn("bootindex=1", d["device"])
        with self.assertRaises(ValueError):
            raw_e2e.medium_drive("/w/medium.raw", "/w/medium.iso", "floppy")
        self.assertEqual(raw_e2e.target_disk(), "/dev/vdb")
        self.assertEqual(raw_e2e.target_disk("/w/medium.iso"), "/dev/vda")

    def test_systemd_boot_menu_is_told_from_grub_and_the_kernel_console(self):
        self.assertEqual(raw_e2e.sdboot_frame(SDBOOT_MENU_OCR), "sdboot")
        self.assertEqual(raw_e2e.sdboot_frame(SDBOOT_MENU_OCR.upper()), "sdboot")
        self.assertEqual(raw_e2e.sdboot_frame(GRUB_MENU_OCR_RAW), "grub")
        for ocr in ("", "TianoCore\n", LIVE_UNIT_OCR, "Reboot Into Firmware Interface\n"):
            with self.subTest(ocr=ocr):
                self.assertEqual(raw_e2e.sdboot_frame(ocr), "other")
        self.assertTrue(medium.LIVE_TITLE.lower().startswith(raw_e2e.SDBOOT_LIVE_TEXT))

    def test_the_root_must_be_where_the_drive_puts_it(self):
        self.assertEqual(raw_e2e.iso_boot_problems("cdrom", CDROM_FACTS), [])
        self.assertEqual(raw_e2e.iso_boot_problems("usb", USB_FACTS), [])
        self.assertTrue(raw_e2e.iso_boot_problems("cdrom", USB_FACTS))
        self.assertTrue(raw_e2e.iso_boot_problems("usb", CDROM_FACTS))
        twice = {**CDROM_FACTS, "loop_devices": "/dev/loop0:/sr0 /dev/loop1:/dev/sr0"}
        self.assertEqual(raw_e2e.iso_boot_problems("cdrom", twice), ["2 loop devices over the CD-ROM: /dev/loop0:/sr0 /dev/loop1:/dev/sr0"])
        grub = {**USB_FACTS, "loader": "GRUB 2.12", "loader_part_uuid": ""}
        self.assertEqual(len(raw_e2e.iso_boot_problems("usb", grub)), 2)
        text = "loader=systemd-boot 261.2\nroot_disk=/dev/loop0\nloop_devices=/dev/loop0:/dev/sr0 \nNAME SIZE\n"
        self.assertEqual(raw_e2e.parse_facts(text),
                         {"loader": "systemd-boot 261.2", "root_disk": "/dev/loop0", "loop_devices": "/dev/loop0:/dev/sr0"})

    def test_the_main_flavor_runs_both_iso_legs_after_the_raw_one(self):
        steps = self.workflow["jobs"]["e2e"]["steps"]
        names = [s.get("name") for s in steps]
        build = next(s for s in steps if s.get("id") == "iso")
        self.assertEqual(build["if"], "matrix.iso")
        self.assertIn('iso/raw/build-hybrid-iso.sh "$WORK/medium.raw" "$WORK/medium.iso"', build["run"])
        self.assertLess(names.index("Bake the Flatpaks into the medium and shrink it"), names.index("Build the hybrid ISO"))
        legs = {s["env"]["BOOT_AS"]: s for s in steps if "BOOT_AS" in s.get("env", {})}
        self.assertEqual(set(legs), set(raw_e2e.BOOT_AS))
        for leg in legs.values():
            self.assertEqual(leg["if"], "${{ !cancelled() && steps.iso.outcome == 'success' }}")
            self.assertIn('--iso "$WORK/medium.iso" --boot-as "$BOOT_AS"', leg["run"])
            self.assertLess(names.index("End-to-end with screenshots"), names.index(leg["name"]))
        resolve = self.workflow["jobs"]["resolve"]["steps"][-1]["run"]
        self.assertIn("iso:$iso", resolve)
        self.assertIn('[[ "$image" != "$main" ]] || iso=true', resolve)
        upload = next(s for s in steps if "upload-artifact" in s.get("uses", ""))
        self.assertIn("e2e-iso-*/screenshots/", upload["with"]["path"])


if __name__ == "__main__":
    unittest.main()
