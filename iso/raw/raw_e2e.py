#!/usr/bin/env python3
"""End-to-end test of the experimental image-based installer, with screenshots.

  raw_e2e.py --raw medium.raw --work DIR --candidate IMAGE@sha256:... --ssh-key KEY
             [--iso medium.iso --boot-as cdrom|usb]

Stages (every screenshot is listed in DIR/screenshots/manifest.json):
  1. Boot the raw disk image as live media (read-only virtio disk, GRUB's
     default "Utah Live" entry: ro utah.live, tmpfs overlay over the root).
     GRUB's menu is held and read back by tesseract: shim's fallback screen
     or no menu with the live entry fails the stage.
     With --iso the live VM boots the hybrid ISO (build-hybrid-iso.sh)
     instead, as a CD-ROM (AHCI, media=cdrom) or a USB stick (usb-storage):
     systemd-boot's menu is held and read back by tesseract
     (00-systemd-boot-menu), and the live system must show it was booted by
     systemd-boot from that drive, the root on a loop device over the CD-ROM
     or on the USB disk (evidence/iso-boot.txt). After the install the
     target's ESP must hold shim again and none of the ISO's live boot files.
  2. Install with the real gnome-setup window driving systemd-sysinstall
     (--install gui): language, keyboard, try-or-install, disk, confirmation,
     progress, the recovery key with its QR code, done. --install script runs
     the same systemd-sysinstall Run, utah-sysinstall-post@ and a recovery key
     enrollment over SSH, which exercises the installed disk independently of
     the GUI driver. With a TPM (--tpm, swtpm) the root is bound to it:
     every boot with the TPM must unlock without a prompt, and the bootc switch
     boot runs without the TPM and unlocks with the recovery key. Without one,
     gnome-setup's disk passphrase page sets a passphrase next to the recovery
     key: first boot and rollback unlock with the passphrase, the bootc switch
     boot with the recovery key.
  3. Boot the installed disk alone: Plymouth LUKS prompt, unlock,
     gnome-initial-setup (user creation; a Time Zone page GeoClue left without
     a location gets one from its city search), GNOME desktop, a terminal with
     `bootc status`.
  4. `bootc switch` to the candidate digest, reboot, unlock, log in at GDM:
     desktop + `bootc status`. Then `bootc rollback` and the same again.
     With --flatpaks (the medium's baked app list), every installed boot
     checks the baked apps are there; after the first boot one is removed
     (--remove-flatpak) and must stay removed through switch and rollback.
     With --backend composefs, every installed boot must run the composefs
     image with fs-verity enforced and every object must carry verity; the
     first boot also reads every object (composefs_check.py --read).
  5. The installed-system checks, on the first boot: no emergency mode, the
     /efi mountpoint, no bootc SELinux probe denial, logrotate's state, the
     flavor (image-info, OGC kernel, tun, NVIDIA module built for the booted
     kernel), unprivileged user namespaces, the rounded-blur runtime/devel
     split, CountMe, GNOME extension states, fastfetch read back by OCR from a
     terminal screenshot, the Flathub remote and every [services] unit of the
     installed contract.
     The rollback boot repeats the Flathub remote and the services as a repeat
     boot (#19). The bootc switch boot runs another image and gets none of
     them: an older image cannot meet this checkout's rules.

DIR/evidence/results.json records each stage (STAGES) as pass, fail or not
run with its duration, plus SELinux mode and failed units for every boot.

Input is what a user at the machine would give: a QEMU USB tablet clicks at a
widget's on-screen position and QEMU `sendkey` types. AT-SPI (atspi_drive.py,
run in the guest session) only observes: which page shows, what is sensitive,
and where a widget is inside its window. Buttons are pressed through their
AT-SPI "click" action. GTK 4 reports positions relative to the window, so the
harness maximizes it first: a maximized window spans the screen width and starts
right under the top bar (the dock reserves the bottom of the work area, run
44863382769), which turns window coordinates into screen coordinates.

Reuses iso/scripts/qemu_monitor.py, plymouth-prompt.py and luks-unlock.py on
the host, extension-state.sh and fastfetch-ocr-match.sh for the decisions,
and runs e2e-services.sh, verify-installed-flavor.py, verify-countme-runtime.sh
and scripts/verify-desktop-contract.py in the guest (copied to GUEST_SCRIPTS).
"""

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from enum import Enum, auto

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from qemu_monitor import run_hmp_command  # noqa: E402

PIN = "utahluks2026"
USER_FULL = "Utah Tester"
USER = "tester"
PASSWORD = "utahtest2026"
GS_APP = "setup"              # AT-SPI app name of gnome-setup (org.gnome.Setup)
GIS_APP = "initial-setup"
# gnome-initial-setup's Time Zone page ("... You can also search for a city to
# set it yourself."): without a GeoClue location the harness searches this city.
TIMEZONE_SEARCH_HINT = "search for a city"
TIMEZONE_CITY = "Berlin"
TARGET_SIZE_TEXT = "51.5 GB"  # glib's format_size of the 48 GiB target, as gnome-setup's disk row shows it
# gnome-setup's disk row subtitles (src/models/disk/disk.rs, display_desc).
DISK_EMPTY_TEXT = "This disk is fully empty"
DISK_DATA_TEXT = "This disk contains existing data"
BLANK_TARGET_DATA = ("gnome-setup reported existing data on the blank target disk "
                     "(empty-disk detection regressed)")
SCREEN_W, SCREEN_H = 1920, 1080
TOP_BAR = 32                  # GNOME Shell top bar at scale 1: top of the work area
ABS_MAX = 0x7FFF              # QEMU absolute pointer axis range

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=8"]

# /etc/resolv.conf on a boot (target, SELinux label) and the boot's SELinux
# denials, into the facts file. On composefs a build-injected empty
# /etc/resolv.conf (build-raw-image.sh) made resolved log "Failed to open
# /etc/resolv.conf: Permission denied" on every boot; record_boot fails on it.
RESOLV_FACTS = ("echo '== /etc/resolv.conf'; ls -laZ /etc/resolv.conf; readlink -f /etc/resolv.conf; "
                "echo '== SELinux denials'; "
                "journalctl -b --no-pager -o cat _TRANSPORT=audit | grep -o 'avc: *denied.*' "
                "| sed -E 's/ (pid|ino)=[0-9]+//g' | sort | uniq -c | sort -rn | head -20")

REPO = HERE.parent.parent
# Where the checks the guest runs are copied to, readable by the test user.
GUEST_SCRIPTS = "/var/tmp/utah-e2e"
# GNOME extensions that must reach ACTIVE at first login (#969).
EXTENSIONS = ("gsconnect@andyholmes.github.io",)
# GRUB's menu on the medium: its help text and the live entry
# (medium.LIVE_TITLE), as tesseract reads them, so a kernel console line
# naming a "Utah live" unit is no menu. shim's fallback (fbx64.efi) screen
# instead restores a firmware boot entry and resets (build-raw-image.sh
# removes fbx64.efi).
GRUB_LIVE_TEXT = "utah live"
GRUB_MENU_TEXT = ("boot the selected", "entry is highlighted")
SHIM_FALLBACK_TEXT = ("boot option restoration", "stop system reset")
# Held while waiting for GRUB's menu (1 s timeout on the medium): any key
# stops the countdown, and GRUB's menu gives "x" no meaning (no hotkeys).
GRUB_HOLD_KEY = "x"
GRUB_WAIT = 180
# GRUB's menu is two colours (grey text on black, run 45935827310's
# debug/grub-probe.png): any frame that is not flat is read back.
GRUB_MIN_COLORS = 2
# fastfetch-ocr-match.sh needs fastfetch's body and this sentinel.
FASTFETCH_CMD = "fastfetch && echo UTAH-E2E-FASTFETCH"
# /efi exists and neither EFI mount unit failed (#1330).
EFI_CHECK = ('test -d /efi && for unit in efi.automount efi.mount; do '
             'load=$(systemctl show "$unit" -p LoadState --value) && '
             'state=$(systemctl show "$unit" -p ActiveState --value) && '
             'echo "$unit: load=$load active=$state" && test "$state" != failed || exit 1; done')
# clean-stage removes /var/lib from the image: tmpfiles must recreate logrotate's.
LOGROTATE_CHECK = ("sh -eu -c 'test -d /var/lib/logrotate; systemctl start logrotate.service; "
                   "test -s /var/lib/logrotate/logrotate.status'")
# What identifies the caller of a denied bootc mac_admin probe (#1198).
BOOTC_PROBE_DIAG = (
    "echo '## SYSCALL records of chcon/bootc'; grep -E 'type=SYSCALL.*comm=\"(chcon|bootc)\"' /var/log/audit/audit.log; "
    "echo '## labels'; ls -Z /usr/bin/bootc /usr/bin/chcon /usr/bin/bash /usr/bin/unshare "
    "/usr/bin/ublue-booted-image-sync /usr/libexec/projectbluefin-countme 2>&1; "
    "echo '## mounts'; findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS / /usr /etc /var /sysroot 2>&1; "
    "echo '## services by main PID'; systemctl show --type=service --all -p Id -p ExecMainPID "
    "-p ExecMainStartTimestampMonotonic '*' | awk -F= '/^Id=/{id=$2} /^ExecMainPID=/{pid=$2} "
    "/^ExecMainStartTimestampMonotonic=/{if (pid > 0) print pid, $2, id}' | sort -n")

# The stages results.json reports, in order.
STAGES = ("live boot", "install", "first boot unlock", "initial setup",
          "first boot desktop", "bootc switch", "bootc rollback")

# The hybrid ISO (--iso, build-hybrid-iso.sh). systemd-boot's menu lists the
# live entry (medium.LIVE_TITLE) and, with auto-firmware, "Reboot Into
# Firmware Interface", where GRUB says "UEFI Firmware Settings". The harness
# presses SDBOOT_HOLD_KEY between screendumps until the menu is read:
# systemd-boot drops keys typed before its menu, but one typed while it shows
# stops the 5 s countdown (loader.conf), and this one is no hotkey there
# (boot.c) and names no entry. Then Enter boots the default entry.
BOOT_AS = ("cdrom", "usb")
SDBOOT_LIVE_TEXT = "utah live"
SDBOOT_MENU_TEXT = "reboot into firmware"
GRUB_TEXT = ("gnu grub", "entry is highlighted", "uefi firmware settings")
SDBOOT_HOLD_KEY = "x"
SDBOOT_WAIT = 180
ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LOADER_GUID = "4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
# How the live system was booted and where its root is: key=value lines
# (parse_facts) for iso_boot_problems.
ISO_BOOT_FACTS = (
    "efivar() { tail -c +5 /sys/firmware/efi/efivars/$1-" + LOADER_GUID + " 2>/dev/null | tr -d '\\0'; }; "
    "uuid=$(sed -n 's/.*root=UUID=\\([^ ]*\\).*/\\1/p' /proc/cmdline); "
    "part=$(realpath /dev/disk/by-uuid/$uuid); disk=/dev/$(lsblk -rno PKNAME $part); "
    "echo loader=$(efivar LoaderInfo); echo loader_part_uuid=$(efivar LoaderDevicePartUUID); "
    "echo root_part=$part; echo root_disk=$disk; echo root_tran=$(lsblk -dno TRAN $disk); "
    "echo loop_backing=$(losetup -nO BACK-FILE $disk 2>/dev/null); "
    "echo loop_devices=$(losetup -nO NAME,BACK-FILE | tr -s ' ' ':' | tr '\\n' ' ')"
)


def parse_failed_units(text):
    """Unit names from `systemctl --failed --no-legend --plain` output."""
    units = []
    for line in (text or "").splitlines():
        words = line.replace("\u25cf", " ").split()
        if words and "." in words[0]:
            units.append(words[0])
    return units


class Results:
    """Per-stage pass/fail with durations, and what every boot looked like."""

    def __init__(self, install, clock=time.monotonic):
        self.install = install
        self.clock = clock
        self.stages = {}
        self.boots = {}
        self.flatpaks = {}
        self.composefs = {}
        self.backend = None
        self.installer = "gnome-setup"
        self.encryption = {}
        self.medium = {"medium": "raw"}
        self.current = None
        self.started = None
        self.error = None
        self.checks = {}

    def begin(self, stage):
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        self.finish()
        self.current, self.started = stage, self.clock()

    def finish(self, error=None):
        """Close the running stage: failed if error is set, else passed."""
        if self.current is None:
            if error is not None and self.error is None:
                self.error = str(error)
            return
        self.stages[self.current] = {"status": "fail" if error is not None else "pass",
                                     "seconds": round(self.clock() - self.started)}
        if error is not None:
            self.error = str(error)
        self.current = None

    def boot(self, phase, selinux, failed_units):
        self.boots[phase] = {"selinux": selinux, "failed_units": failed_units}

    def flatpak(self, phase, installed, problems):
        self.flatpaks[phase] = {"apps": len(installed), "problems": problems}

    def composefs_boot(self, phase, facts, problems):
        self.composefs[phase] = {**{k: v for k, v in facts.items() if k != "cmdline"}, "problems": problems}

    def check(self, phase, name, problems):
        self.checks.setdefault(phase, {})[name] = problems

    def as_dict(self, timings=None):
        stages = [{"stage": s, **self.stages.get(s, {"status": "not run", "seconds": None})} for s in STAGES]
        passed = self.error is None and all(s["status"] == "pass" for s in stages)
        return {"result": "pass" if passed else "fail", "error": self.error, "install": self.install,
                "installer": self.installer, "medium": self.medium, "backend": self.backend,
                "encryption": self.encryption,
                "stages": stages, "boots": self.boots, "flatpaks": self.flatpaks,
                "composefs": self.composefs, "installed_checks": self.checks, "timings": dict(timings or {})}


def flatpak_problems(baked, installed, removed=None):
    """What is wrong with the installed system's Flatpaks.

    baked: apps the medium carries (bake-flatpaks.sh); removed: the app the
    user removed before bootc switch, which must stay removed. Every other
    baked app must still be there.
    """
    installed = set(installed)
    problems = [f"missing {app}" for app in sorted(set(baked) - installed) if app != removed]
    if removed and removed in installed:
        problems.append(f"{removed} came back after it was removed")
    return problems


def composefs_karg(cmdline):
    """(digest, strict) from the composefs karg on a kernel command line, or None."""
    for arg in cmdline.split():
        key, eq, value = arg.partition("=")
        if eq and key in ("composefs", "composefs.digest"):
            return value.lstrip("?"), not value.startswith("?")
    return None


def composefs_problems(facts):
    """What is wrong with an installed composefs boot (composefs_check.py's facts).

    The installed entries must enforce fs-verity (no `?` on the karg, as on
    the live entry), the root must be the composefs image of that
    digest, every object must carry verity, and a full read must find no
    error and no "FILE CORRUPTED" report.
    """
    problems = []
    karg = composefs_karg(facts.get("cmdline", ""))
    if karg is None:
        return ["no composefs karg on the kernel command line"]
    digest, strict = karg
    if not strict:
        problems.append("the composefs karg does not enforce fs-verity ('?')")
    if not facts.get("root_source", "").endswith(digest.rsplit(":", 1)[-1]):
        problems.append(f"root is {facts.get('root_source')!r}, not the composefs image {digest}")
    objects, verity = facts.get("objects", 0), facts.get("verity_objects", 0)
    if not objects:
        problems.append("no objects in /sysroot/composefs/objects")
    elif verity != objects:
        problems.append(f"{objects - verity} of {objects} composefs objects have no fs-verity")
    if facts.get("read_errors"):
        problems.append(f"{facts['read_errors']} composefs objects failed to read")
    if facts.get("corrupted_reports"):
        problems.append(f"{facts['corrupted_reports']} fs-verity FILE CORRUPTED reports this boot")
    return problems


def image_name(ref):
    """The repository name of an image reference: ghcr.io/projectbluefin/utah-nvidia@sha256:... -> utah-nvidia."""
    return ref.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]


def emergency_entered(show):
    """Whether `systemctl show` of emergency.target/emergency.service says either started this boot.

    The image locks root, so sulogin exits at once and the boot carries on to
    the graphical target: reaching it does not prove there was no emergency."""
    for line in show.splitlines():
        key, eq, value = line.partition("=")
        if eq and key in ("InactiveExitTimestampMonotonic", "ExecMainStartTimestamp") \
                and value.strip() not in ("", "0", "n/a"):
            return True
    return False


BOOTC_PROBE_DENIAL = re.compile(r'denied .*comm="(chcon|bootc)"')


def bootc_probe_denials(avc):
    """AVC lines that deny bootc's mac_admin probe: utah_bootc_probe is not in effect (#1198)."""
    return [line for line in avc.splitlines() if BOOTC_PROBE_DENIAL.search(line)]


def flathub_configured(remotes):
    """`flatpak remotes --system --columns=name` lists flathub."""
    return "flathub" in (line.strip() for line in remotes.splitlines())


def rounded_blur_problems(runtime_rc, devel_rc):
    """rpm -q exit codes: the runtime must be installed and the build-only -devel absent (rc 1)."""
    problems = []
    if runtime_rc != 0:
        problems.append("installed image lacks the gnome-rounded-blur runtime package")
    if devel_rc == 0:
        problems.append("build-only gnome-rounded-blur-devel leaked into the installed image")
    elif devel_rc != 1:
        problems.append("could not verify gnome-rounded-blur-devel is absent")
    return problems


def extension_state(info):
    """(state, active) from `gnome-extensions info` output, as iso/scripts/extension-state.sh decides it."""
    r = subprocess.run(["bash", "-c", 'source "$1"; state=$(extension_state_from_info); printf "%s\\n" "$state"; '
                        'extension_state_is_active "$state"', "extension-state", str(SCRIPTS / "extension-state.sh")],
                       input=info, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode == 0


def fastfetch_seen(transcript):
    """Whether a tesseract transcript file proves fastfetch ran (iso/scripts/fastfetch-ocr-match.sh)."""
    return subprocess.run(["bash", str(SCRIPTS / "fastfetch-ocr-match.sh"), str(transcript)],
                          capture_output=True).returncode == 0


def welcome_dialog_showing(ocr):
    """GNOME Shell's first-login "Welcome ... Skip / Take Tour" dialog is over the screenshot."""
    return "take tour" in ocr.lower()


def boot_frame(ocr):
    """What a tesseract transcript of an early boot frame shows: "fallback"
    (shim's fbx64.efi restoring a firmware boot entry before a reset), "grub"
    (GRUB's menu listing the live entry) or "other" (firmware logo, kernel)."""
    text = " ".join(ocr.lower().split())
    if any(s in text for s in SHIM_FALLBACK_TEXT):
        return "fallback"
    if GRUB_LIVE_TEXT in text and any(s in text for s in GRUB_MENU_TEXT):
        return "grub"
    return "other"


def setup_state_script(boot_pass):
    """e2e-services.sh's verify_setup_state, run in the guest as the test user.

    Its ssh_target/scp_target reach the installed system from the host; here
    they run locally. TEST_PASSWORD feeds its `sudo -S`."""
    return (f"ROOT={GUEST_SCRIPTS} TEST_USER={USER} TEST_PASSWORD={PASSWORD}; "
            'ssh_target() { bash -c "$1"; }; scp_target() { cp "$1" "${2#*:}"; }; '
            'fail() { echo "FAIL: $*" >&2; exit 1; }; '
            f"source {GUEST_SCRIPTS}/e2e-services.sh; verify_setup_state {shlex.quote(boot_pass)}")


class Fail(Exception):
    pass


class Page(Enum):
    FAILURE = auto()
    DONE = auto()
    LANGUAGE = auto()
    KEYBOARD = auto()
    TRY_OR_INSTALL = auto()
    DISK = auto()
    DATA_FOUND = auto()
    ERASE = auto()
    CONFIRM = auto()
    PASSPHRASE = auto()
    RECOVERY_KEY = auto()
    QR = auto()
    PROGRESS = auto()
    UNKNOWN = auto()


def classify_setup_page(nodes):
    """Visible headings win over navigation buttons and hidden old pages.

    Adapted from dakota-iso's atspi-installer-driver: failure must win even
    when the same snapshot still contains completion text.
    """
    visible = [n for n in nodes if "showing" in n.get("states", [])]
    for page, headings in (
        (Page.FAILURE, ("Installation Failed", "Could not install")),
        (Page.DONE, ("Installation Complete",)),
        (Page.QR, ("Scan to Back Up",)),
        (Page.RECOVERY_KEY, ("Back Up Recovery Key",)),
        (Page.PASSPHRASE, ("Choose a Disk Passphrase",)),
        (Page.DATA_FOUND, ("Existing Data Found",)),
        # gnome-setup's erase confirmations (src/page/installer/disk/confirm.rs),
        # after 'Use Entire Disk' or forced for a disk short of free space or
        # full, all answer 'Erase and Install' (the forced ones count down,
        # 'Erase and Install (3)'). Not their headings: 'Disk is Full' is a
        # substring of 'This disk is fully empty'.
        (Page.ERASE, ("Erase and Install",)),
        # The nondestructive one an empty disk gets: 'Begin Installation?', 'Install'.
        (Page.CONFIRM, ("Begin Installation",)),
        (Page.DISK, ("Select a Disk", "Unknown Disk")),
        (Page.PROGRESS, ("Installing", "Copying Partition")),
        (Page.TRY_OR_INSTALL, ("Install Now",)),
        (Page.KEYBOARD, ("Keyboard Layout",)),
        (Page.LANGUAGE, ("English",)),
    ):
        if any(has_text(visible, text) for text in headings):
            return page
    return Page.UNKNOWN


def blank_target_problem(page, target_row=()):
    """Why gnome-setup's view contradicts the blank target, or None.

    install.qcow2 is created fresh (all zeroes) for every run, so the one
    correct route is the empty-disk one: the target's row reads 'This disk is
    fully empty' and selecting it asks 'Begin Installation?' with a plain
    'Install'. The data page or an erase confirmation means gnome-setup
    misread the disk; it read every disk that way while it judged emptiness
    from currentSizeBytes, which systemd-repart reports as the device size.
    target_row: the visible nodes of the target's disk row, when on the disk page.
    """
    if page in (Page.DATA_FOUND, Page.ERASE) or has_text(target_row, DISK_DATA_TEXT):
        return BLANK_TARGET_DATA
    return None


def medium_drive(raw, iso=None, boot_as=None):
    """The live VM's medium: the raw image as a read-only virtio disk, or the
    hybrid ISO as a CD-ROM on q35's AHCI controller (media=cdrom, 2048-byte
    sectors) or as a USB stick on the xHCI controller the VM always has."""
    if iso is None:
        return {"drive": f"file={raw},format=raw,if=none,id=medium,readonly=on",
                "device": "virtio-blk-pci,drive=medium,bootindex=1"}
    if boot_as == "cdrom":
        return {"drive": f"file={iso},format=raw,if=none,id=medium,media=cdrom,readonly=on",
                "device": "ide-cd,drive=medium,bootindex=1"}
    if boot_as == "usb":
        return {"drive": f"file={iso},format=raw,if=none,id=medium,readonly=on",
                "device": "usb-storage,drive=medium,bootindex=1,removable=on"}
    raise ValueError(f"--boot-as must be one of {BOOT_AS}, not {boot_as!r}")


def target_disk(iso=None):
    """The install target as the live system names it: virtio-blk disks take
    vdX names in PCI order, and only the raw medium is one of them."""
    return "/dev/vdb" if iso is None else "/dev/vda"


def sdboot_frame(ocr):
    """What a tesseract transcript of an early boot frame of the hybrid ISO
    shows: "sdboot" (systemd-boot's menu listing the live entry), "grub"
    (GRUB's menu: the medium booted shim -> GRUB instead) or "other"."""
    text = " ".join(ocr.lower().split())
    if any(s in text for s in GRUB_TEXT):
        return "grub"
    if SDBOOT_LIVE_TEXT in text and SDBOOT_MENU_TEXT in text:
        return "sdboot"
    return "other"


def parse_facts(text):
    """key=value lines (ISO_BOOT_FACTS) into a dict; the first of a key wins,
    so the diagnostics printed after the facts cannot shadow them."""
    facts = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key and " " not in key:
            facts.setdefault(key, value.strip())
    return facts


def optical(path):
    """Whether a device path names an optical drive (sr0, /dev/sr0)."""
    return re.fullmatch(r"(.*/)?sr[0-9]+", path or "") is not None


def iso_boot_problems(boot_as, facts):
    """What contradicts a live boot of the hybrid ISO from boot_as.

    systemd-boot must have booted it (LoaderInfo) and reported the ESP it
    started from. As a CD-ROM the kernel sees no partitions on the drive, so
    the root must sit on one loop device over the optical drive (the
    initrd's systemd-loop@ attaches it, the live system attaches no second
    one). As a USB stick the root is on the USB disk itself."""
    problems = []
    if not facts.get("loader", "").startswith("systemd-boot"):
        problems.append(f"booted by {facts.get('loader') or 'an unknown loader'}, not systemd-boot")
    if not facts.get("loader_part_uuid"):
        problems.append("systemd-boot exported no LoaderDevicePartUUID")
    disk = facts.get("root_disk", "")
    if boot_as == "cdrom":
        # The loop device was attached in the initrd, so losetup reports its
        # backing device relative to the initrd's /dev ("/sr0").
        if not disk.startswith("/dev/loop") or not optical(facts.get("loop_backing", "")):
            problems.append(f"root is on {disk or 'no disk'} (backed by {facts.get('loop_backing') or 'nothing'}), "
                            "not on a loop device over the CD-ROM")
        over_drive = [d for d in facts.get("loop_devices", "").split() if optical(d.split(":", 1)[-1])]
        if len(over_drive) > 1:
            problems.append(f"{len(over_drive)} loop devices over the CD-ROM: {' '.join(over_drive)}")
    elif boot_as == "usb":
        if facts.get("root_tran") != "usb":
            problems.append(f"root is on {disk or 'no disk'} (transport {facts.get('root_tran') or 'none'}), not on the USB disk")
    else:
        raise ValueError(f"unknown boot_as {boot_as!r}")
    return problems


# check_target_esp prints it when EFI/BOOT/grubx64.efi is the vendor GRUB
# again, not the systemd-boot the hybrid ISO's shim started.
VENDOR_GRUB_MARK = "REMOVABLE-GRUB-IS-VENDOR-GRUB"


def target_esp_problems(files):
    """What the installed ESP (paths relative to it, plus VENDOR_GRUB_MARK)
    still carries of the hybrid ISO's live systemd-boot setup, or lacks of
    shim -> GRUB on the removable path."""
    lower = {f.lower().strip("/") for f in files}
    problems = []
    for need in ("EFI/BOOT/BOOTX64.EFI", "EFI/BOOT/grubx64.efi"):
        if need.lower() not in lower:
            problems.append(f"no {need}")
    if "efi/boot/grubx64.efi" in lower and VENDOR_GRUB_MARK not in files:
        problems.append("EFI/BOOT/grubx64.efi is not the vendor GRUB")
    leftovers = sorted(f for f in files if f.lower().strip("/").split("/", 1)[0] in ("utah-live", "loader"))
    if leftovers:
        problems.append("live boot files left: " + " ".join(leftovers))
    return problems


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_firmware():
    pairs = [("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
             ("/usr/share/edk2/ovmf/OVMF_CODE.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd"),
             ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd")]
    for code, var in pairs:
        if os.path.exists(code) and os.path.exists(var):
            return code, var
    raise Fail("OVMF firmware not found")


class Harness:
    def __init__(self, args):
        self.args = args
        self.work = Path(args.work).resolve()
        self.shots = self.work / "screenshots"
        self.evidence = self.work / "evidence"
        self.debug = self.work / "debug"
        for d in (self.shots, self.evidence, self.debug):
            d.mkdir(parents=True, exist_ok=True)
        self.manifest = []
        self.vm = None
        self.mon_sock = None
        self.qmp_sock = None
        self.serial = None
        self.ssh_port = None
        self.code, self.vars_src = find_firmware()
        self.baseline_digest = None
        self.candidate_digest = args.candidate.split("@", 1)[1] if "@" in args.candidate else None
        self.timings = {}
        self.results = Results(args.install)
        decision = json.loads(Path(args.backend_decision).read_text()) if args.backend_decision else {}
        if decision and decision.get("backend") != args.backend:
            raise SystemExit(f"--backend {args.backend} contradicts {args.backend_decision}: {decision}")
        self.results.backend = {"backend": args.backend, **decision}
        self.tpm = bool(args.tpm)
        if self.tpm and not shutil.which("swtpm"):
            raise SystemExit("--tpm needs swtpm")
        self.tpm_state = self.work / "swtpm-state"
        self.swtpm = None
        self.recovery_key = None
        self.results.encryption = {"tpm2": self.tpm, "unlocks": {}}
        self.target_disk = target_disk(args.iso)
        self.results.medium = ({"medium": "raw"} if args.iso is None
                               else {"medium": "iso", "boot_as": args.boot_as})
        if not shutil.which("tesseract"):
            raise SystemExit("needs tesseract: the fastfetch screenshot is read back by OCR")
        self.flavor = args.flavor or flavor_of(args.candidate)
        log(f"flavor under test: {self.flavor}")

    # ---- QEMU ---------------------------------------------------------------
    def start_vm(self, name, drives, vars_file, ssh_port, tpm=False):
        self.mon_sock = str(self.work / f"{name}-monitor.sock")
        self.qmp_sock = str(self.work / f"{name}-qmp.sock")
        self.serial = self.work / f"{name}-serial.log"
        self.ssh_port = ssh_port
        cmd = ["qemu-system-x86_64", "-name", name, "-machine", "q35", "-m", str(self.args.ram),
               "-smp", str(self.args.cpus),
               "-drive", f"if=pflash,format=raw,readonly=on,file={self.code}",
               "-drive", f"if=pflash,format=raw,file={vars_file}",
               "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
               "-device", "virtio-net-pci,netdev=n0",
               "-vga", "none", "-device", "VGA,xres=1920,yres=1080",
               # Absolute pointer: clicks land at exact screen coordinates.
               "-device", "qemu-xhci", "-device", "usb-tablet",
               "-display", "none", "-serial", f"file:{self.serial}",
               "-monitor", f"unix:{self.mon_sock},server,nowait",
               "-qmp", f"unix:{self.qmp_sock},server=on,wait=off"]
        cmd += ["-accel", "kvm", "-cpu", "host"] if os.access("/dev/kvm", os.W_OK) else ["-accel", "tcg"]
        if tpm:
            cmd += self.start_swtpm(name)
        for d in drives:
            cmd += ["-drive", d["drive"], "-device", d["device"]]
        log(f"starting VM {name}: {' '.join(shlex.quote(c) for c in cmd)}")
        self.vm = subprocess.Popen(cmd, stdout=open(self.work / f"{name}-qemu.log", "w"), stderr=subprocess.STDOUT)
        for _ in range(50):
            if os.path.exists(self.mon_sock) and os.path.exists(self.qmp_sock):
                break
            time.sleep(0.2)

    def start_swtpm(self, name):
        """A software TPM 2.0 whose state persists across the VMs of this run."""
        sock = self.work / f"{name}-swtpm.sock"
        sock.unlink(missing_ok=True)
        self.tpm_state.mkdir(exist_ok=True)
        out = self.evidence / f"{name}-swtpm.out"
        with out.open("w") as stream:
            self.swtpm = subprocess.Popen(["swtpm", "socket", "--tpm2", "--tpmstate", f"dir={self.tpm_state}",
                                           "--ctrl", f"type=unixio,path={sock}",
                                           "--log", f"file={self.evidence / f'{name}-swtpm.log'},level=1"],
                                          stdout=stream, stderr=subprocess.STDOUT)
        for _ in range(50):
            if sock.exists():
                break
            if self.swtpm.poll() is not None:
                break
            time.sleep(0.2)
        if not sock.exists():
            raise Fail(f"swtpm did not create its control socket (exit {self.swtpm.poll()}): "
                       f"{out.read_text().strip()[-500:] or 'no output'}")
        return ["-chardev", f"socket,id=chrtpm,path={sock}",
                "-tpmdev", "emulator,id=tpm0,chardev=chrtpm", "-device", "tpm-crb,tpmdev=tpm0"]

    def stop_swtpm(self):
        if self.swtpm is not None and self.swtpm.poll() is None:
            self.swtpm.terminate()
            try:
                self.swtpm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.swtpm.kill()
        self.swtpm = None

    def stop_vm(self, graceful_cmd=None):
        if self.vm is None:
            self.stop_swtpm()
            return
        if graceful_cmd:
            try:
                graceful_cmd()
            except Exception as e:  # noqa: BLE001
                log(f"graceful stop failed: {e}")
            try:
                self.vm.wait(timeout=180)
            except subprocess.TimeoutExpired:
                log("VM did not power off in time; killing")
        if self.vm.poll() is None:
            self.vm.kill()
            self.vm.wait()
        self.vm = None
        self.stop_swtpm()

    def mon(self, command):
        return run_hmp_command(self.mon_sock, command, settle=0.1, prompt=b"(qemu)", recv_timeout=5)

    def qmp(self, *commands):
        """Run QMP commands on a fresh connection; returns their replies."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.qmp_sock)
            f = s.makefile("rwb", buffering=0)

            def reply():
                while True:
                    line = f.readline()
                    if not line:
                        raise Fail("QMP connection closed")
                    msg = json.loads(line)
                    if "return" in msg or "error" in msg:
                        return msg

            f.readline()  # greeting
            f.write(b'{"execute": "qmp_capabilities"}\n')
            reply()
            out = []
            for c in commands:
                f.write(json.dumps(c).encode() + b"\n")
                r = reply()
                if "error" in r:
                    raise Fail(f"QMP {c['execute']}: {r['error']}")
                out.append(r)
            return out

    def click_at(self, x, y):
        x = max(0, min(SCREEN_W - 1, int(x)))
        y = max(0, min(SCREEN_H - 1, int(y)))
        ev = lambda events: {"execute": "input-send-event", "arguments": {"events": events}}  # noqa: E731
        self.qmp(ev([{"type": "abs", "data": {"axis": "x", "value": round(x * ABS_MAX / (SCREEN_W - 1))}},
                     {"type": "abs", "data": {"axis": "y", "value": round(y * ABS_MAX / (SCREEN_H - 1))}}]))
        time.sleep(0.4)
        self.qmp(ev([{"type": "btn", "data": {"down": True, "button": "left"}}]))
        time.sleep(0.1)
        self.qmp(ev([{"type": "btn", "data": {"down": False, "button": "left"}}]))
        time.sleep(0.5)

    def key(self, k):
        self.mon(f"sendkey {k}")
        time.sleep(0.08)

    def type(self, text, enter=False):
        for ch in text:
            if ch.isdigit() or ("a" <= ch <= "z"):
                k = ch
            elif "A" <= ch <= "Z":
                k = f"shift-{ch.lower()}"
            else:
                k = {" ": "spc", "-": "minus", ".": "dot", "_": "shift-minus", "/": "slash"}[ch]
            self.key(k)
        if enter:
            self.key("ret")

    def screendump(self, path):
        ppm = path.with_suffix(".ppm")
        ppm.unlink(missing_ok=True)
        self.mon(f"screendump {ppm}")
        for _ in range(20):
            if ppm.exists() and ppm.stat().st_size > 0:
                break
            time.sleep(0.3)
        time.sleep(0.5)
        return ppm

    def to_png(self, ppm, png):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(ppm), str(png)], check=True)
        ppm.unlink(missing_ok=True)

    def shot(self, label, proves, retake_until_drawn=False, required=False, below_top_bar=False):
        """A screenshot into the manifest. retake_until_drawn retakes a flat frame
        (below_top_bar: one flat below the top bar) for a minute; required then
        fails the run on a frame that never drew (a flat or frozen screen is
        no proof a session started, #786)."""
        png = self.shots / f"{label}.png"
        for attempt in range(12 if retake_until_drawn else 1):
            ppm = self.screendump(png)
            ok = drawn(ppm, below_top_bar=below_top_bar)
            if not retake_until_drawn or ok:
                break
            log(f"  {label}: frame not drawn yet ({attempt + 1}/12)")
            self.key("shift")
            time.sleep(5)
        self.to_png(ppm, png)
        self.record_shot(png, label, proves)
        if required and not ok:
            raise Fail(f"screenshot {png.name} shows a blank or flat-colour frame rather than: {proves}")
        return png

    def record_shot(self, png, label, proves):
        self.manifest.append({"file": png.name, "stage": label, "proves": proves})
        self.write_manifest()
        log(f"  screenshot {png.name}: {proves}")

    def debug_shot(self, label):
        """Diagnostic frame outside the stage table (debug/)."""
        try:
            png = self.debug / f"{label}.png"
            self.to_png(self.screendump(png), png)
        except Exception as e:  # noqa: BLE001
            log(f"  debug shot {label} failed: {e}")

    def write_manifest(self):
        (self.shots / "manifest.json").write_text(json.dumps(self.manifest, indent=2) + "\n")

    def wait_serial(self, needle, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if self.vm.poll() is not None:
                raise Fail(f"VM exited while waiting for {needle!r} on serial")
            if self.serial.exists() and needle.encode() in self.serial.read_bytes():
                return
            time.sleep(3)
        raise Fail(f"serial never showed {needle!r} within {timeout}s")

    # ---- SSH ----------------------------------------------------------------
    def ssh(self, user, command, check=True, timeout=600, password=None):
        base = (["sshpass", "-p", password, "ssh"] if password else ["ssh", "-i", self.args.ssh_key])
        cmd = base + SSH_OPTS + ["-p", str(self.ssh_port), f"{user}@127.0.0.1", command]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise Fail(f"ssh {user}: {command!r} failed ({r.returncode}): {r.stderr.strip()[-2000:]}")
        return r.stdout

    def scp_to(self, user, src, dst, password=None):
        base = (["sshpass", "-p", password, "scp"] if password else ["scp", "-i", self.args.ssh_key])
        subprocess.run(base + SSH_OPTS + ["-P", str(self.ssh_port), str(src), f"{user}@127.0.0.1:{dst}"], check=True)

    def scp_from(self, user, src, dst, password=None):
        base = (["sshpass", "-p", password, "scp"] if password else ["scp", "-i", self.args.ssh_key])
        subprocess.run(base + SSH_OPTS + ["-P", str(self.ssh_port), f"{user}@127.0.0.1:{src}", str(dst)], check=True)

    def wait_ssh(self, user, timeout, password=None):
        end = time.time() + timeout
        while time.time() < end:
            if self.vm.poll() is not None:
                raise Fail("VM exited while waiting for SSH")
            try:
                self.ssh(user, "true", timeout=20, password=password)
                return
            except (Fail, subprocess.TimeoutExpired):
                time.sleep(5)
        raise Fail(f"SSH as {user} not reachable within {timeout}s")

    def collect_journal(self, tag):
        """Best-effort guest journal for diagnosis (live user via sudo, or root)."""
        for user, pw, cmd in (("liveuser", "live", "sudo journalctl -b --no-pager -o short-monotonic"),
                              ("root", None, "journalctl -b --no-pager -o short-monotonic")):
            try:
                out = self.ssh(user, cmd, timeout=120, password=pw)
                self.save(f"journal-{tag}.txt", out)
                return
            except Exception:  # noqa: BLE001
                continue

    def save(self, name, text):
        (self.evidence / name).write_text(text)

    # ---- AT-SPI observation + pointer/keyboard input -------------------------
    def atspi(self, runner, *argv):
        # `|| true`: a miss (exit 2/3) still prints its JSON error, which callers handle.
        out = runner("python3 /var/tmp/atspi_drive.py " + " ".join(shlex.quote(a) for a in argv) + " || true")
        try:
            return json.loads(out) if out.strip() else None
        except json.JSONDecodeError:
            raise Fail(f"atspi {argv}: unparsable output {out[:500]!r}")

    def snapshot(self, runner, app):
        try:
            nodes = self.atspi(runner, "snapshot", app)
        except (Fail, subprocess.TimeoutExpired):
            return []
        return nodes if isinstance(nodes, list) else []

    def poll(self, runner, app, predicate, timeout):
        end = time.time() + timeout
        while True:
            nodes = self.snapshot(runner, app)
            if nodes and predicate(nodes):
                return nodes
            if time.time() >= end:
                return None
            time.sleep(2)

    def wait_for(self, runner, app, predicate, what, timeout=300):
        nodes = self.poll(runner, app, predicate, timeout)
        if nodes is not None:
            return nodes
        nodes = self.snapshot(runner, app)
        self.save(f"atspi-timeout-{slug(what)}.json", json.dumps(nodes, indent=1))
        for line in summarize_nodes(nodes):
            log(f"  atspi [{slug(what)}]: {line}")
        self.shot(f"fail-{slug(what)}", f"FAILURE: {what} never appeared")
        raise Fail(f"{what} never appeared in {app}")

    def to_screen(self, nodes, node):
        frame = top_frame(nodes)
        if frame is None or not node.get("ext"):
            raise Fail(f"no window extents for {node.get('role')} {node.get('name')!r}")
        fx, fy, fw, fh = frame["ext"]
        x, y, w, h = node["ext"]
        if fw >= SCREEN_W - 2:
            # Fullscreen covers the screen; maximized starts under the top bar.
            ox, oy = 0, (0 if fh >= SCREEN_H - 2 else TOP_BAR)
        else:
            # Mutter centers new windows in the work area (fallback; logged).
            ox, oy = (SCREEN_W - fw) // 2, TOP_BAR + (SCREEN_H - TOP_BAR - fh) // 2
            log(f"  window {fw}x{fh} not maximized; assuming it is centered")
        return ox + (x - fx) + w // 2, oy + (y - fy) + h // 2

    def click_node(self, nodes, node):
        x, y = self.to_screen(nodes, node)
        log(f"  click {node['role']} {node['name'] or node['text'][:30]!r} at ({x}, {y}) ext={node['ext']}")
        self.click_at(x, y)

    def maximize(self, runner, app):
        """Maximize the app's window so window coordinates map to the screen."""
        nodes = []
        for attempt in range(4):
            nodes = self.snapshot(runner, app)
            frame = top_frame(nodes)
            if frame is not None and frame["ext"][2] >= SCREEN_W - 2:
                log(f"  {app} window maximized: frame ext={frame['ext']}")
                return nodes
            log(f"  {app} frame ext={frame and frame['ext']}; maximizing (attempt {attempt + 1})")
            if attempt % 2 == 0 and has_name(nodes, "Maximize", sensitive=True):
                self.atspi(runner, "act", app, "Maximize")
            else:
                self.atspi(runner, "frame-action", app, "window.toggle-maximized")
            time.sleep(3)
        self.save(f"atspi-maximize-{slug(app)}.json", json.dumps(nodes, indent=1))
        log(f"  {app} window could not be maximized; clicks use the centered-window fallback")
        return nodes

    def press(self, runner, app, name, done, what, timeout=120, role=None):
        """Press a button by its AT-SPI click action (pointer if it has none)."""
        act = ["act", app, name] if role is None else ["act", app, name, role]
        for attempt in range(3):
            res = self.atspi(runner, *act)
            if not res or "error" in res:
                log(f"  press {name!r}: {res}")
                time.sleep(3)
                continue
            if res["how"] == "none":
                nodes = self.snapshot(runner, app)
                node = next((n for n in nodes if n["path"] == res["path"]), None)
                if node is not None:
                    self.click_node(nodes, node)
            got = self.poll(runner, app, done, 30 if attempt < 2 else timeout)
            if got is not None:
                return got
            self.debug_shot(f"press-miss-{slug(what)}-{attempt}")
        self.save(f"atspi-timeout-{slug(what)}.json", json.dumps(self.snapshot(runner, app), indent=1))
        self.shot(f"fail-{slug(what)}", f"FAILURE: pressing {name!r} never reached {what}")
        raise Fail(f"pressing {name!r} never reached {what}")

    def fill(self, runner, app, node, text, clear=True):
        """Click an entry, then type into it."""
        nodes = self.snapshot(runner, app)
        node = next((n for n in nodes if n["path"] == node["path"]), node)
        self.click_node(nodes, node)
        time.sleep(1)
        if clear:
            self.key("ctrl-a")
            self.key("backspace")
        self.type(text)
        time.sleep(1)

    def record_boot(self, phase, user, password=None):
        """SELinux mode and failed units of the running boot, into results.json.

        Fails the boot if systemd-resolved could not read /etc/resolv.conf: a
        build-injected empty file in the deployment's /etc (build-raw-image.sh)
        did that on every composefs boot without failing a unit. Fails it too
        on any SELinux denial on /boot: an unlabeled /boot root denied logind
        on every installed composefs boot (#1368); build-raw-image.sh labels it."""
        sudo = "" if user == "root" else "sudo "
        out = self.ssh(user, "getenforce; echo ---; systemctl --failed --no-legend --plain; echo ---; "
                       f"{sudo}journalctl -b --no-pager -o cat -u systemd-resolved | grep -c 'resolv.conf: Permission denied'; echo ---; "
                       f"{sudo}journalctl -b --no-pager -o cat _TRANSPORT=audit | grep -c 'avc: *denied.* path=\"/boot[/\"]'",
                       password=password, check=False)
        selinux, _, rest = out.partition("---")
        failed, _, rest = rest.partition("---")
        denied, _, boot_avcs = rest.partition("---")
        self.results.boot(phase, selinux.strip() or "unknown", parse_failed_units(failed))
        if denied.strip() not in ("", "0"):
            raise Fail(f"{phase}: systemd-resolved could not open /etc/resolv.conf ({denied.strip()} times)")
        if boot_avcs.strip() not in ("", "0"):
            raise Fail(f"{phase}: {boot_avcs.strip()} SELinux denials on /boot (facts file)")

    # ---- Stages -------------------------------------------------------------
    def live(self):
        iso = self.args.iso
        what = "the raw disk image" if iso is None else f"the hybrid ISO as {self.args.boot_as}"
        log(f"=== Stage 1: live boot of {what} ===")
        self.results.begin("live boot")
        target = self.work / "install.qcow2"
        target.unlink(missing_ok=True)
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(target), self.args.target_size], check=True)
        self.vars_live = self.work / "vars-live.fd"
        shutil.copy(self.vars_src, self.vars_live)
        self.start_vm("live", [
            medium_drive(self.args.raw, iso, self.args.boot_as),
            {"drive": f"file={target},format=qcow2,if=none,id=target",
             "device": "virtio-blk-pci,drive=target"},
        ], self.vars_live, self.args.ssh_port, tpm=self.tpm)
        if iso is None:
            self.shot_when_grub()
        else:
            self.shot_when_sdboot()
        self.wait_serial("UTAH_LIVE_READY", 1200)
        live_pw = "live"
        self.wait_ssh("liveuser", 300, password=live_pw)
        runner = lambda c: self.ssh("liveuser", "export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus; " + c, password=live_pw)  # noqa: E731
        self.live_runner = runner
        facts = self.ssh("liveuser", "cat /proc/cmdline; echo; findmnt -no FSTYPE,SOURCE,OPTIONS /sysroot; findmnt -no FSTYPE,OPTIONS /; getenforce; "
                         "lsblk -o NAME,SIZE,RO,TYPE,FSTYPE,LABEL,PARTLABEL,MOUNTPOINTS; systemctl --failed --no-legend; sudo bootc status || true; "
                         + RESOLV_FACTS.replace("journalctl", "sudo journalctl"),
                         password=live_pw, check=False)
        self.save("live-facts.txt", facts)
        self.record_boot("live", "liveuser", password=live_pw)
        if "utah.live" not in facts.split("\n", 1)[0]:
            raise Fail("live VM did not boot the utah.live entry")
        if iso is not None:
            self.check_iso_boot(live_pw)
        self.scp_to("liveuser", HERE / "atspi_drive.py", "/var/tmp/atspi_drive.py", password=live_pw)
        # Plymouth's theme and watermark, for the installed-boot prompt check.
        self.ssh("liveuser", 'theme=$(plymouth-set-default-theme) && file=/usr/share/plymouth/themes/$theme/$theme.plymouth && '
                 'dir=$(sed -n "s/^ImageDir=//p" "$file" | head -n 1) && cp "$file" /var/tmp/theme.plymouth && cp "$dir/watermark.png" /var/tmp/watermark.png',
                 password=live_pw)
        self.scp_from("liveuser", "/var/tmp/theme.plymouth", self.work / "theme.plymouth", password=live_pw)
        self.scp_from("liveuser", "/var/tmp/watermark.png", self.work / "watermark.png", password=live_pw)
        end = time.time() + 300
        while time.time() < end and "gnome-shell" not in self.ssh("liveuser", "pgrep -a -u liveuser gnome-shell || true", password=live_pw):
            time.sleep(5)
        time.sleep(10)
        self.shot("01-live-session", f"{what[0].upper()}{what[1:]} booted as live media (utah.live, tmpfs overlay): GNOME session for the live user",
                  True, required=True, below_top_bar=True)

    def hold_key(self, key, stop):
        """Press key every quarter second over QMP (the HMP monitor stays free
        for screendumps) until stop is set."""
        while not stop.is_set():
            try:
                self.qmp({"execute": "send-key", "arguments": {"keys": [{"type": "qcode", "data": key}]}})
            except (Fail, OSError):
                pass
            stop.wait(0.25)

    def shot_when_grub(self):
        """00-grub-menu: GRUB's menu on the medium, read back by tesseract
        (boot_frame, evidence/grub-menu-ocr.txt). GRUB_HOLD_KEY is held from
        power-on until the menu is read, which stops its 1 s countdown; Enter
        then boots the default entry. shim's fallback screen, or no menu
        within GRUB_WAIT, fails the stage."""
        proves = "Firmware -> shim -> GRUB on the medium (no shim fallback); 'Utah Live' BLS entry listed"
        probe = self.debug / "grub-probe.png"
        transcripts = []
        stop = threading.Event()
        holder = threading.Thread(target=self.hold_key, args=(GRUB_HOLD_KEY, stop), daemon=True)
        holder.start()
        try:
            end = time.time() + GRUB_WAIT
            while time.time() < end:
                if self.vm.poll() is not None:
                    raise Fail("live VM exited before GRUB's menu was drawn")
                ppm = self.screendump(probe)
                if not drawn(ppm, min_colors=GRUB_MIN_COLORS):
                    time.sleep(0.5)
                    continue
                ocr = subprocess.run(["tesseract", str(ppm), "stdout"], capture_output=True, text=True).stdout
                verdict = boot_frame(ocr)
                transcripts.append(f"== {time.strftime('%H:%M:%S')} {verdict}\n{ocr}")
                if verdict == "other":
                    continue
                stop.set()
                holder.join()
                png = self.shots / "00-grub-menu.png"
                self.to_png(ppm, png)
                self.record_shot(png, "00-grub-menu", proves)
                if verdict == "fallback":
                    raise Fail("shim's fallback (EFI/BOOT/fbx64.efi: 'Boot Option Restoration', a firmware boot entry "
                               "and a reset) ran instead of GRUB: screenshots/00-grub-menu.png")
                self.key("ret")
                return
            if probe.with_suffix(".ppm").exists():
                self.to_png(probe.with_suffix(".ppm"), probe)
            raise Fail(f"no GRUB menu listing the live entry read back within {GRUB_WAIT}s "
                       "(evidence/grub-menu-ocr.txt, debug/grub-probe.png)")
        finally:
            stop.set()
            holder.join()
            self.save("grub-menu-ocr.txt", "\n".join(transcripts))

    def shot_when_sdboot(self):
        """00-systemd-boot-menu: systemd-boot's menu on the hybrid ISO, read
        back by tesseract (sdboot_frame, evidence/sdboot-menu-ocr.txt).
        SDBOOT_HOLD_KEY is pressed between screendumps until the menu is read,
        so the countdown stops; Enter then boots the default entry. GRUB's menu,
        or no menu within SDBOOT_WAIT, fails the stage."""
        proves = (f"Firmware -> El Torito/{self.args.boot_as} -> shim -> systemd-boot on the hybrid ISO's ESP; "
                  "'Utah Live' Type #1 entry listed")
        probe = self.debug / "sdboot-probe.png"
        transcripts = []
        try:
            end = time.time() + SDBOOT_WAIT
            while time.time() < end:
                if self.vm.poll() is not None:
                    raise Fail("live VM exited before systemd-boot's menu was drawn")
                self.key(SDBOOT_HOLD_KEY)
                ppm = self.screendump(probe)
                if not drawn(ppm, min_colors=2):
                    continue
                ocr = subprocess.run(["tesseract", str(ppm), "stdout"], capture_output=True, text=True).stdout
                verdict = sdboot_frame(ocr)
                transcripts.append(f"== {time.strftime('%H:%M:%S')} {verdict}\n{ocr}")
                if verdict == "other":
                    continue
                png = self.shots / "00-systemd-boot-menu.png"
                self.to_png(ppm, png)
                self.record_shot(png, "00-systemd-boot-menu", proves)
                if verdict == "grub":
                    raise Fail("GRUB's menu showed instead of systemd-boot's: screenshots/00-systemd-boot-menu.png")
                self.key("ret")
                return
            if probe.with_suffix(".ppm").exists():
                self.to_png(probe.with_suffix(".ppm"), probe)
            raise Fail(f"no systemd-boot menu listing the live entry read back within {SDBOOT_WAIT}s "
                       "(evidence/sdboot-menu-ocr.txt, debug/sdboot-probe.png)")
        finally:
            self.save("sdboot-menu-ocr.txt", "\n".join(transcripts))

    def check_iso_boot(self, password):
        """The live system came from the hybrid ISO through systemd-boot, its
        root where the drive puts it (iso_boot_problems, evidence/iso-boot.txt)."""
        out = self.ssh("liveuser", ISO_BOOT_FACTS + "; echo; lsblk -o NAME,SIZE,RO,TYPE,TRAN,FSTYPE,PARTUUID; "
                       "systemctl status --no-pager 'systemd-loop@*' 2>&1; "
                       "sudo journalctl -b --no-pager -o short-monotonic -u 'systemd-loop@*' 2>&1 | tail -20",
                       password=password, check=False)
        self.save("iso-boot.txt", out)
        facts = parse_facts(out)
        problems = iso_boot_problems(self.args.boot_as, facts)
        self.results.medium.update({k: facts.get(k, "") for k in ("loader", "root_part", "root_disk", "loop_backing")},
                                   problems=problems)
        log(f"  ISO boot ({self.args.boot_as}): {facts.get('loader')}, root {facts.get('root_part')} "
            f"on {facts.get('root_disk')} {facts.get('loop_backing') or facts.get('root_tran')}")
        if problems:
            raise Fail(f"hybrid ISO as {self.args.boot_as}: " + "; ".join(problems) + " (evidence/iso-boot.txt)")

    def check_target_esp(self):
        """After an install from the hybrid ISO, the target's ESP boots shim ->
        GRUB like a raw-medium install: utah-sysinstall-post put the medium's
        EFI/BOOT files back and removed the ISO's systemd-boot setup
        (target_esp_problems)."""
        out = self.ssh("liveuser", f"esp=$(lsblk -rnpo NAME,PARTTYPE {self.target_disk} | awk 'tolower($2) == \"{ESP_TYPE}\" {{print $1}}'); "
                       "sudo sh -c 'd=$(mktemp -d) && mount -o ro \"$1\" \"$d\" && find \"$d\" -type f -printf \"%P\\n\" | sort; "
                       "for v in \"$d\"/EFI/*/grubx64.efi; do case \"$v\" in */EFI/BOOT/*) ;; "
                       f"*) cmp -s \"$v\" \"$d/EFI/BOOT/grubx64.efi\" && echo {VENDOR_GRUB_MARK} ;; esac; done; "
                       "umount \"$d\"; rmdir \"$d\"' sh \"$esp\"",
                       password="live", check=False)
        self.save("target-esp-files.txt", out)
        problems = target_esp_problems(out.split())
        self.results.medium["target_esp_problems"] = problems
        if problems:
            raise Fail("the installed ESP after an ISO install: " + "; ".join(problems) + " (evidence/target-esp-files.txt)")

    # ---- gnome-setup ----------------------------------------------------------
    def gnome_setup_install(self):
        log("=== Stage 2: gnome-setup GUI (systemd-sysinstall) ===")
        self.results.begin("install")
        r, app = self.live_runner, GS_APP
        try:
            self.wait_for(r, app, lambda nodes: classify_setup_page(nodes) is not Page.UNKNOWN,
                          "first setup page", 300)
            self.key("esc")
            time.sleep(3)
            self.maximize(r, app)
            self.drive_setup(r)
            self.grab_install_log()
            self.grab_sysinstall_journal()
            self.check_slots()
        except Exception:
            self.setup_diagnose(r, "failure")
            raise
        self.after_install()

    def setup_diagnose(self, runner, label):
        """Best effort: never replace the original failure with a diagnostic error."""
        for collect in (
            lambda: self.setup_dump(runner, label),
            lambda: self.shot(f"fail-setup-{label}", "FAILURE: current installer page"),
            self.grab_install_log,
            self.grab_sysinstall_journal,
        ):
            try:
                collect()
            except Exception as error:  # noqa: BLE001
                log(f"  setup diagnostics: {error}")

    def setup_dump(self, runner, label):
        nodes = self.atspi(runner, "snapshot-all", GS_APP)
        self.save(f"atspi-gs-{label}.json", json.dumps(nodes, indent=1))
        if isinstance(nodes, list):
            for line in summarize_nodes(nodes):
                log(f"  setup [{label}]: {line}")

    def setup_press(self, runner, nodes, *names):
        """One action only; the next poll decides which page actually appeared."""
        for name in names:
            node = next((n for n in nodes if norm(n["name"]) == norm(name)
                         and n["role"] in ("button", "push button", "check box")
                         and "sensitive" in n["states"]), None)
            if node is None:
                continue
            result = self.atspi(runner, "act", GS_APP, name, node["role"])
            if result and "error" not in result:
                if result.get("how") == "none":
                    self.click_node(nodes, node)
                return True
        self.setup_dump(runner, "miss")
        return False

    def drive_setup(self, runner):
        start = time.monotonic()
        deadline = start + 4200
        previous, since = None, start
        seen = set()
        qr_seen = False
        target_selected = False
        passphrase_filled = False
        confirmed_at = None
        while time.monotonic() < deadline:
            nodes = [n for n in self.snapshot(runner, GS_APP) if "showing" in n["states"]]
            page = classify_setup_page(nodes)
            now = time.monotonic()
            if page != previous:
                previous, since = page, now
                log(f"  setup page: {page.name}")
            if page not in seen:
                self.save(f"atspi-gs-{page.name.lower()}.json", json.dumps(nodes, indent=1))
                self.shot(f"setup-{page.name.lower()}", f"Installer page: {page.name.lower()}")
                seen.add(page)
            if page is Page.FAILURE:
                raise Fail("gnome-setup: installation failed")
            if page is Page.DONE:
                if not self.recovery_key:
                    raise Fail("installation completed without a recovery key")
                self.timings["gui-install-seconds-after-confirm"] = int(now - (confirmed_at or start))
                return
            problem = blank_target_problem(page)
            if problem:
                raise Fail(problem)
            limit = 60 if page is Page.UNKNOWN else (3600 if page is Page.PROGRESS else 120)
            if now - since >= limit:
                raise Fail(f"gnome-setup stuck on {page.name.lower()} page")
            if page in (Page.LANGUAGE, Page.KEYBOARD):
                if has_name(nodes, "Next", sensitive=True, role="button"):
                    self.setup_press(runner, nodes, "Next")
                else:
                    text = "English (United States)" if page is Page.LANGUAGE else "English (US)"
                    row = row_containing(nodes, text)
                    if row is not None:
                        self.click_node(nodes, row)
                    else:
                        self.setup_dump(runner, "miss")
            elif page is Page.TRY_OR_INSTALL:
                self.setup_press(runner, nodes, "Install Now")
            elif page is Page.DISK:
                row = row_containing(nodes, TARGET_SIZE_TEXT)
                if row is None:
                    self.setup_dump(runner, "miss")
                else:
                    shown = subtree(nodes, row)
                    problem = blank_target_problem(page, shown)
                    if problem:
                        raise Fail(problem)
                    if not target_selected:
                        status = "empty" if has_text(shown, DISK_EMPTY_TEXT) else "status not exposed"
                        log(f"  target row {TARGET_SIZE_TEXT}: {status}")
                    self.click_node(nodes, row)
                    target_selected = True
            elif page is Page.CONFIRM:
                # The dialog names no disk: it confirms the row this run picked by size.
                if not target_selected:
                    raise Fail(f"refusing to confirm installation: the {TARGET_SIZE_TEXT} target row was never selected")
                if self.setup_press(runner, nodes, "Install"):
                    confirmed_at = confirmed_at or now
            elif page is Page.PASSPHRASE:
                if self.tpm:
                    raise Fail("gnome-setup asked for a disk passphrase although the VM has a TPM")
                if not passphrase_filled:
                    entries = [named(nodes, name, sensitive=True) for name in ("Passphrase", "Confirm Passphrase")]
                    if all(entries):
                        for entry in entries:
                            self.fill(runner, GS_APP, entry, PIN)
                        passphrase_filled = True
                    else:
                        self.setup_dump(runner, "miss")
                else:
                    self.setup_press(runner, nodes, "Next")
            elif page is Page.QR:
                qr_seen = True
                self.setup_press(runner, nodes, "Done")
            elif page is Page.RECOVERY_KEY:
                key = self.recovery_key_text(nodes)
                if key:
                    self.recovery_key = key
                    self.save("recovery-key.txt", key + "\n")
                    if not qr_seen:
                        self.setup_press(runner, nodes, "Show QR Code for Recovery Key")
                    elif has_name(nodes, "Done", sensitive=True, role="button"):
                        self.setup_press(runner, nodes, "Done")
                    else:
                        checkbox = named(nodes, "I backed up my recovery key", role="check box")
                        if checkbox is None or "checked" not in checkbox["states"]:
                            self.setup_press(runner, nodes, "I backed up my recovery key")
            time.sleep(2)
        raise Fail("gnome-setup exceeded overall installation deadline")

    # systemd-cryptenroll recovery keys: eight groups of eight lowercase letters.
    KEY_RE = re.compile(r"[a-z]{8}(?:-[a-z]{8}){7}")

    @staticmethod
    def recovery_key_text(nodes):
        node = named(nodes, "Recovery Key")
        if node is not None and len((node["text"] or "").strip()) > 40:
            return node["text"].strip()
        # The key label's accessible name is the key itself (run 44972858422),
        # so fall back to the key shape in any name or text.
        for n in nodes:
            for field in (n.get("name") or "", n.get("text") or ""):
                if Harness.KEY_RE.fullmatch(field.strip()):
                    return field.strip()
        return ""

    def grab_sysinstall_journal(self):
        out = self.ssh("liveuser", "sudo journalctl -b --no-pager -o short-monotonic -u 'utah-sysinstall@*' "
                       "-u 'utah-sysinstall-post@*' -u 'utah-cryptenroll@*'; cat /run/utah-live/repart.d/*.conf",
                       password="live", check=False)
        self.save("sysinstall-journal.txt", out)

    def debug_live_state(self, label):
        """Best-effort live-VM state when a gnome-setup step fails: the sysinstall
        socket/units, the repart definitions, the block layout, and every size-like
        row text on screen. Distinguishes 'sysinstall lists no candidates' from
        'the row is there but the size text differs from TARGET_SIZE_TEXT'."""
        try:
            nodes = self.snapshot(self.live_runner, GS_APP)
        except (Fail, subprocess.TimeoutExpired):
            nodes = []
        sizes = sorted({f"{n['name']} {n['text']}".strip() for n in nodes
                        if n.get("ext") and ("GB" in f"{n['name']} {n['text']}" or "GiB" in f"{n['name']} {n['text']}")})
        log(f"  live debug [{label}]: size-like rows: {sizes or 'NONE'}")
        self.save(f"atspi-gs-{label}-debug.json", json.dumps(nodes, indent=1))
        out = self.ssh("liveuser",
                       "echo '--- lsblk ---'; lsblk -rno NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT; "
                       "echo '--- sysinstall socket ---'; systemctl status utah-sysinstall.socket --no-pager; "
                       "echo '--- sysinstall instances ---'; systemctl list-units 'utah-sysinstall@*' --no-pager --all; "
                       "echo '--- journal ---'; sudo journalctl -b --no-pager -o short-monotonic -u 'utah-sysinstall@*' -u utah-sysinstall.socket; "
                       "echo '--- repart.d ---'; ls -la /run/utah-live/repart.d/; cat /run/utah-live/repart.d/*.conf; "
                       "echo '--- definitions link ---'; ls -la /usr/lib/gnome-setup/definitions",
                       password="live", check=False)
        self.save(f"live-debug-{label}.txt", out)

    def check_slots(self):
        """The target's LUKS2 keyslots: a recovery key, TPM2 when there is one, else the user's passphrase.

        The empty passphrase systemd-repart enrolled must be gone: with a TPM no password slot
        is left, without one exactly one (the passphrase from the disk passphrase page)."""
        out = self.ssh("liveuser", f"dev=$(lsblk -rnpo NAME,FSTYPE {self.target_disk} | awk '$2 == \"crypto_LUKS\" {{print $1}}'); "
                       "echo \"$dev\"; sudo /usr/libexec/utah-sysinstall/systemd-cryptenroll \"$dev\"",
                       password="live", check=False)
        self.save("luks-slots.txt", out)
        types = [line.split()[1] for line in out.splitlines()[1:] if len(line.split()) >= 2 and line.split()[0].isdigit()]
        self.results.encryption["slots"] = sorted(types)
        log(f"  LUKS2 slots on the target: {sorted(types)}")
        want = ["recovery", "tpm2"] if self.tpm else ["password", "recovery"]
        if sorted(types) != want:
            raise Fail(f"target keyslots are {sorted(types)}, expected {want} (evidence/luks-slots.txt)")
        if not self.tpm:
            # The password slot is the user's passphrase, not the empty one systemd-repart enrolled.
            test = self.ssh("liveuser", f"dev=$(lsblk -rnpo NAME,FSTYPE {self.target_disk} | awk '$2 == \"crypto_LUKS\" {{print $1}}'); "
                            f"printf %s {PIN} | sudo cryptsetup open --test-passphrase --key-file=- \"$dev\" && echo PASSPHRASE-OK; "
                            "printf '' | sudo cryptsetup open --test-passphrase --key-file=- \"$dev\" && echo EMPTY-OK",
                            password="live", check=False)
            self.save("luks-passphrase-test.txt", test)
            if "PASSPHRASE-OK" not in test or "EMPTY-OK" in test:
                raise Fail("the password slot is not the chosen passphrase, or the empty one survived (evidence/luks-passphrase-test.txt)")

    def gnome_setup_script_install(self):
        """What gnome-setup does, over SSH: sysinstall Run, the post-install unit, a passphrase
        when there is no TPM, a recovery key (wiping the empty passphrase systemd-repart enrolled)."""
        log("=== Stage 2 (script): systemd-sysinstall + utah-sysinstall-post over SSH ===")
        self.results.begin("install")
        start = time.time()
        call = json.dumps({"node": self.target_disk, "definitions": ["/run/utah-live/repart.d"], "erase": True})
        unit = "utah-sysinstall-post@" + self.target_disk.strip("/").replace("/", "-") + ".service"
        cryptenroll = "/usr/libexec/utah-sysinstall/systemd-cryptenroll"
        passphrase = "" if self.tpm else f"sudo NEWPASSWORD={PIN} {cryptenroll} --unlock-key-file=/dev/null --password \"$dev\" && "
        out = self.ssh("liveuser", f"sudo varlinkctl call --more /run/systemd/io.systemd.SysInstall io.systemd.SysInstall.Run "
                       f"{shlex.quote(call)} && sudo systemctl start {unit} && "
                       f"dev=$(lsblk -rnpo NAME,FSTYPE {self.target_disk} | awk '$2 == \"crypto_LUKS\" {{print $1}}') && "
                       f"empty=$(sudo {cryptenroll} \"$dev\" | awk '$2 == \"password\" {{print $1}}') && {passphrase}"
                       f"sudo {cryptenroll} --unlock-key-file=/dev/null --recovery-key --wipe-slot=\"$empty\" \"$dev\" 2>&1",
                       password="live", timeout=3600, check=False)
        self.timings["script-install-seconds"] = int(time.time() - start)
        self.save("script-install.log", out)
        self.grab_install_log()
        self.grab_sysinstall_journal()
        keys = [w for w in out.split() if w.count("-") == 7 and len(w) == 71]
        if not keys:
            raise Fail("no recovery key from systemd-cryptenroll (evidence/script-install.log)")
        self.recovery_key = keys[-1]
        self.save("recovery-key.txt", self.recovery_key + "\n")
        self.check_slots()
        self.after_install()

    def after_install(self):
        facts = self.ssh("liveuser", "sudo efibootmgr -v; lsblk -o NAME,SIZE,TYPE,FSTYPE,UUID,PARTLABEL", password="live", check=False)
        self.save("live-after-install.txt", facts)
        if self.args.iso is not None:
            self.check_target_esp()
        self.stop_vm(lambda: self.ssh("liveuser", "sudo systemctl poweroff --no-block", password="live", check=False))

    def grab_install_log(self):
        # The script writes it as root (it re-executes through sudo).
        out = self.ssh("liveuser", "sudo cat /run/utah-install.log", password="live", check=False)
        if out:
            self.save("utah-install.log", out)
        else:
            log("  no /run/utah-install.log")

    def installed_boot(self, phase, unlock_label, unlock="pin"):
        """Boot (or reboot) the installed disk: unlock, graphical target.

        unlock: "pin" types the passphrase of gnome-setup's disk passphrase page and "recovery" gnome-setup's
        recovery key at the branded Plymouth prompt; "tpm" expects no prompt at
        all (gnome-setup's TPM2 enrollment) and screenshots the first frame the
        unlocked system draws."""
        self.results.encryption["unlocks"][phase] = unlock
        if unlock == "tpm":
            try:
                self.wait_ssh("root", 900)
            except Fail:
                self.shot(f"fail-tpm-unlock-{slug(phase)}", f"FAILURE: {phase}: no TPM2 unlock (a prompt instead?)")
                raise
        else:
            status = subprocess.run([sys.executable, str(SCRIPTS / "plymouth-prompt.py"), "wait", self.mon_sock, str(self.serial),
                                     str(self.work / "theme.plymouth"), str(self.work / "watermark.png"),
                                     str(self.shots / f"{unlock_label}.ppm"), "600"]).returncode
            ppm = self.shots / f"{unlock_label}.ppm"
            what = "passphrase" if unlock == "pin" else "recovery key"
            if ppm.exists():
                self.to_png(ppm, self.shots / f"{unlock_label}.png")
                self.manifest.append({"file": f"{unlock_label}.png", "stage": unlock_label,
                                      "proves": f"{phase}: graphical, Bluefin-branded Plymouth LUKS prompt, answered with the {what} (plymouth-prompt.py exit {status})"})
                self.write_manifest()
            if status != 0:
                self.debug_shot(f"no-prompt-{slug(phase)}")
                raise Fail(f"{phase}: no branded Plymouth prompt (plymouth-prompt.py exit {status})")
            secret = PIN if unlock == "pin" else self.recovery_key
            r = subprocess.run([sys.executable, str(SCRIPTS / "luks-unlock.py"), "qemu", self.mon_sock, secret, str(self.serial),
                                str(self.ssh_port)], capture_output=True, text=True)
            self.save(f"unlock-{slug(phase)}.log", r.stdout + r.stderr)
            if r.returncode != 0:
                self.shot(f"fail-unlock-{slug(phase)}", f"FAILURE: {phase} unlock")
                raise Fail(f"{phase}: LUKS unlock failed ({r.returncode})")
        self.wait_ssh("root", 600)
        end = time.time() + 600
        while time.time() < end:
            if self.ssh("root", "systemctl is-active graphical.target", check=False).strip() == "active":
                break
            time.sleep(5)
        else:
            raise Fail(f"{phase}: graphical.target not reached")
        facts = self.ssh("root", "cat /proc/cmdline; echo; findmnt -no FSTYPE,SOURCE /sysroot; findmnt -no FSTYPE,SOURCE,OPTIONS /boot; "
                         "lsblk -f; getenforce; cat /etc/crypttab; "
                         "systemctl is-active gdm.service graphical.target; systemctl --failed --no-legend; bootc status; "
                         + RESOLV_FACTS, check=False)
        self.save(f"facts-{slug(phase)}.txt", facts)
        self.record_boot(phase, "root")
        if unlock == "tpm":
            unlock_log = self.ssh("root", "journalctl -b --no-pager -o cat -u 'systemd-cryptsetup@*'", check=False)
            self.save(f"unlock-{slug(phase)}.log", unlock_log)
            time.sleep(10)
            self.shot(unlock_label, f"{phase}: no Plymouth prompt; the LUKS2 root was unlocked by the TPM2 "
                      "(PCR 7) and the system reached the graphical target", True)
        if "utah.live" in facts.split("\n", 1)[0]:
            raise Fail(f"{phase}: installed system booted with utah.live")
        self.scp_to("root", HERE / "atspi_drive.py", "/var/tmp/atspi_drive.py")
        self.scp_to("root", HERE / "vte_term.py", "/var/tmp/vte_term.py")
        self.ssh("root", "chmod 0755 /var/tmp/atspi_drive.py /var/tmp/vte_term.py")
        if self.args.backend == "composefs":
            self.check_composefs(phase, read=phase == "first boot")

    def check_composefs(self, phase, read):
        """fs-verity is enforced on this boot; on the first boot, every object reads back clean."""
        self.scp_to("root", HERE / "composefs_check.py", "/var/tmp/composefs_check.py")
        out = self.ssh("root", "python3 /var/tmp/composefs_check.py" + (" --read" if read else ""), timeout=3600)
        self.save(f"composefs-{slug(phase)}.json", out)
        facts = json.loads(out)
        problems = composefs_problems(facts)
        self.results.composefs_boot(phase, facts, problems)
        log(f"  {phase}: composefs {facts['verity_objects']}/{facts['objects']} objects with fs-verity"
            + (f", read {facts['read_bytes'] / 1e9:.2f} GB in {facts['seconds']}s" if read else "")
            + f"; problems: {'; '.join(problems) or 'none'}")
        if problems:
            raise Fail(f"{phase}: composefs: {'; '.join(problems)}")

    def user_runner(self, user):
        def run(command):
            uid = self.ssh("root", f"id -u {user}").strip()
            env = f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus XDG_RUNTIME_DIR=/run/user/{uid}"
            return self.ssh("root", f"runuser -u {user} -- env {env} sh -c {shlex.quote(command)}")
        return run

    def copy_guest_scripts(self):
        """The guest-side checks, from their iso/scripts/ paths, readable by the test user."""
        self.ssh("root", f"mkdir -p {GUEST_SCRIPTS}/scripts")
        for src, dst in ((SCRIPTS / "e2e-services.sh", "e2e-services.sh"),
                         (SCRIPTS / "verify-installed-flavor.py", "verify-installed-flavor.py"),
                         (SCRIPTS / "verify-countme-runtime.sh", "verify-countme-runtime.sh"),
                         (REPO / "scripts/verify-desktop-contract.py", "scripts/verify-desktop-contract.py")):
            self.scp_to("root", src, f"{GUEST_SCRIPTS}/{dst}")
        self.ssh("root", f"chmod -R a+rX {GUEST_SCRIPTS}")

    def check_installed(self, phase, prefix, repeat=False):
        """The checks on the installed system, with their pass/fail.

        Every check runs and the boot fails with all their problems together;
        repeat is the repeat boot (#19): the Flathub remote and the contract's
        units again."""
        self.copy_guest_scripts()
        boot_pass = "repeat" if repeat else "first"
        checks = [("flathub remote", self.check_flathub),
                  ("setup units", lambda: self.check_setup_state(phase, boot_pass))]
        if not repeat:
            checks = [("boot state", lambda: self.check_boot_state(phase)),
                      ("flavor", lambda: self.check_flavor(phase)),
                      ("countme", lambda: self.check_countme(phase)),
                      ("extensions", lambda: self.check_extensions(phase)),
                      ("fastfetch", lambda: self.check_fastfetch(phase, prefix))] + checks
        problems = []
        for name, check in checks:
            try:
                found = check()
            except (Fail, subprocess.TimeoutExpired) as e:
                found = [str(e)]
            self.results.check(phase, name, found)
            log(f"  {phase}: {name}: {'; '.join(found) or 'ok'}")
            problems += [f"{name}: {p}" for p in found]
        if problems:
            raise Fail(f"{phase}: {'; '.join(problems)}")

    def check_boot_state(self, phase):
        """No emergency mode, /efi, no denied bootc SELinux probe, logrotate's boot-created state."""
        problems = []
        show = self.ssh("root", "systemctl show -p InactiveExitTimestampMonotonic emergency.target; "
                        "systemctl show -p ExecMainStartTimestamp emergency.service", check=False)
        if emergency_entered(show):
            problems.append("the installed system entered emergency mode during boot (evidence/journal)")
            self.save(f"emergency-{slug(phase)}.txt", show + self.ssh(
                "root", "journalctl -b --no-pager -o short-monotonic -p err", check=False))
        efi = self.ssh("root", EFI_CHECK + " && echo EFI-OK", check=False)
        if "EFI-OK" not in efi:
            problems.append(f"EFI mountpoint is missing or an EFI mount unit failed: {efi.strip()}")
        avc = self.ssh("root", "grep -hF avc: /var/log/audit/audit.log 2>/dev/null; "
                       "journalctl -b --no-pager -o cat 2>/dev/null | grep -F avc:; true", check=False)
        self.save(f"avc-{slug(phase)}.log", avc)
        if bootc_probe_denials(avc):
            self.save(f"bootc-probe-diag-{slug(phase)}.log", self.ssh("root", BOOTC_PROBE_DIAG, check=False))
            problems.append("chcon was denied mac_admin during boot (#1198): utah_bootc_probe is not in effect "
                            f"(evidence/avc-{slug(phase)}.log, bootc-probe-diag-{slug(phase)}.log)")
        if "LOGROTATE-OK" not in self.ssh("root", LOGROTATE_CHECK + " && echo LOGROTATE-OK", check=False):
            problems.append("logrotate state directory or service failed on the installed system")
        return problems

    def check_flavor(self, phase):
        """verify-installed-flavor.py (flavor, OGC kernel, tun, NVIDIA module for the booted
        kernel), unprivileged user namespaces, the rounded-blur runtime/devel split."""
        problems = []
        try:
            out = self.ssh("root", f"python3 {GUEST_SCRIPTS}/verify-installed-flavor.py --flavor {shlex.quote(self.flavor)}")
            self.save(f"flavor-checks-{slug(phase)}.json", out)
        except Fail as e:
            problems.append(f"installed flavor probe failed: {e}")
        try:
            self.user_runner(USER)("unshare --user --map-root-user true")
        except Fail:
            problems.append("unprivileged user namespaces unavailable for Flatpaks")
        rcs = self.ssh("root", "rpm -q gnome-rounded-blur >/dev/null; echo $?; "
                       "rpm -q --quiet gnome-rounded-blur-devel; echo $?", check=False).split()
        if len(rcs) != 2:
            return problems + [f"could not query the rounded-blur packages: {rcs}"]
        return problems + rounded_blur_problems(int(rcs[0]), int(rcs[1]))

    def check_countme(self, phase):
        """verify-countme-runtime.sh: timer, a mocked report naming the flavor, the opt-out."""
        # stderr carries the reason; self.ssh puts it in the Fail.
        out = self.ssh("root", f"bash {GUEST_SCRIPTS}/verify-countme-runtime.sh {shlex.quote(self.flavor)}")
        self.save(f"countme-{slug(phase)}.log", out)
        return []

    def check_extensions(self, phase):
        """Every EXTENSIONS uuid reaches ACTIVE within two minutes; never re-enabled here."""
        run = self.user_runner(USER)
        problems = []
        for uuid in EXTENSIONS:
            info, state, active = "", "", False
            for _ in range(24):
                try:
                    info = run(f"gnome-extensions info {shlex.quote(uuid)}")
                except Fail as e:
                    info = str(e)
                state, active = extension_state(info)
                if active:
                    break
                time.sleep(5)
            if active:
                log(f"  extension {uuid}: {state}")
                continue
            try:
                errors = run("gdbus call --session --dest org.gnome.Shell.Extensions --object-path /org/gnome/Shell/Extensions "
                             f"--method org.gnome.Shell.Extensions.GetExtensionErrors {shlex.quote(uuid)}")
            except Fail as e:
                errors = str(e)
            self.save(f"extension-{uuid.split('@')[0]}-{slug(phase)}.txt", f"{info}\n== shell-reported errors\n{errors}\n")
            self.debug_shot(f"extension-{uuid.split('@')[0]}-{slug(phase)}")
            problems.append(f"extension {uuid}: " + (f"state={state}" if state else "state could not be read"))
        return problems

    def check_fastfetch(self, phase, prefix):
        """A terminal on the desktop running fastfetch, read back by tesseract
        (fastfetch-ocr-match.sh), and a frame that differs from the clean desktop's."""
        label = f"{prefix}-fastfetch"
        unit = f"utah-e2e-term-{label}"
        png = self.shots / f"{label}.png"
        transcript = self.evidence / f"fastfetch-ocr-{slug(phase)}.txt"
        self.ssh("root", f"systemd-run --machine={USER}@.host --user --collect --unit={unit} "
                         f"python3 /var/tmp/vte_term.py fastfetch {shlex.quote(FASTFETCH_CMD)}", check=False)
        seen = False
        try:
            time.sleep(15)
            for _ in range(24):
                self.to_png(self.screendump(png), png)
                # tesseract reads identical pixels differently between runs (#375): re-read before a new shot.
                for _ in range(3):
                    ocr = subprocess.run(["tesseract", str(png), "stdout"], capture_output=True, text=True).stdout
                    transcript.write_text(ocr)
                    seen = fastfetch_seen(transcript)
                    if seen:
                        break
                if seen:
                    break
                if welcome_dialog_showing(ocr):
                    log("  GNOME Shell welcome dialog is showing; pressing Skip")
                    self.key("ret")
                    time.sleep(3)
                time.sleep(5)
        finally:
            if png.exists():
                self.manifest.append({"file": png.name, "stage": label,
                                      "proves": f"{phase}: fastfetch in a terminal on the desktop, read back by OCR"})
                self.write_manifest()
            self.ssh("root", f"systemctl --machine={USER}@.host --user stop {unit}", check=False)
        problems = [] if seen else [f"fastfetch output was not visible in {png.name} (evidence/{transcript.name})"]
        desktop = self.shots / f"{prefix}-desktop.png"
        if desktop.exists() and png.exists() and desktop.read_bytes() == png.read_bytes():
            problems.append(f"{desktop.name} and {png.name} are byte-identical; they must capture distinct states")
        return problems

    def check_flathub(self):
        remotes = self.ssh("root", "flatpak remotes --system --columns=name", check=False)
        return [] if flathub_configured(remotes) else ["system Flathub remote is not configured"]

    def check_setup_state(self, phase, boot_pass):
        """e2e-services.sh's verify_setup_state as the test user: the setup units ran, every
        [services] unit of the installed contract is still enabled and has not failed."""
        out = self.user_runner(USER)(f"bash -c {shlex.quote(setup_state_script(boot_pass))}")
        self.save(f"setup-units-{slug(phase)}.txt", out)
        return []

    def initial_setup(self):
        log("=== Stage 3: gnome-initial-setup creates the user ===")
        # ps truncates long user names and comm is cut at 15 characters: match the
        # executable path and resolve the uid.
        find_gis = ("ps -eo uid=,args= | awk '{u=$1; $1=\"\"; if ($0 ~ /\\/gnome-initial-setup( |$)/) {print u; exit}}' "
                    "| xargs -r id -nu")
        gis_user = ""
        end = time.time() + 300
        while not gis_user and time.time() < end:
            gis_user = self.ssh("root", find_gis, check=False).strip()
            if not gis_user:
                time.sleep(5)
        if not gis_user:
            self.shot("fail-no-gis", "FAILURE: gnome-initial-setup never started")
            raise Fail("gnome-initial-setup did not start on first boot")
        log(f"  gnome-initial-setup runs as {gis_user}")
        r, app = self.user_runner(gis_user), GIS_APP
        self.wait_for(r, app, lambda n: has_name(n, "Next") or has_name(n, "~Start Using"), "initial setup window", 300)
        self.maximize(r, app)
        previous = None
        for step in range(20):
            nodes = self.snapshot(r, app)
            self.save(f"atspi-gis-{step:02d}.json", json.dumps(nodes, indent=1))
            page = page_title(nodes)
            log(f"  initial setup page {step}: {page!r}")
            entries = [n for n in nodes if n["role"] in ("text", "entry", "password text") and "editable" in n["states"]]
            pw = [n for n in entries if n["role"] == "password text"]
            txt = [n for n in entries if n["role"] != "password text"]
            if pw:
                for n in pw[:2]:
                    self.fill(r, app, n, PASSWORD)
            elif has_text(nodes, "Full Name") or has_name(nodes, "Full Name"):
                if txt:
                    self.fill(r, app, txt[0], USER_FULL)
                    time.sleep(1)
                if len(txt) > 1:
                    self.fill(r, app, txt[1], USER)
            elif txt and has_text(nodes, TIMEZONE_SEARCH_HINT) and next_button(nodes) is None:
                nodes = self.search_time_zone(r, app, txt[0])
                if not has_text(nodes, TIMEZONE_SEARCH_HINT):
                    # Return in the search went on to the next page: drive that one.
                    previous = page
                    continue
            time.sleep(1)
            self.shot(f"13-gis-{step:02d}-{slug(page)[:30]}", f"gnome-initial-setup on the installed system: {page}", True)
            nodes = self.snapshot(r, app)
            start_using = [n for n in nodes if norm(n["name"]).startswith("start using") and "sensitive" in n["states"]]
            if start_using:
                self.atspi(r, "act", app, start_using[0]["name"])
                break
            nxt = next_button(nodes)
            if nxt is None:
                self.save(f"atspi-gis-stuck-{step:02d}.json", json.dumps(nodes, indent=1))
                raise Fail(f"initial setup page {page!r}: no sensitive Next/Skip")
            sig = page_sig(nodes)
            self.press(r, app, nxt["name"], lambda n, s=sig: page_sig(n) != s or has_name(n, "~Start Using"),
                       f"page after {page}", timeout=60, role=nxt["role"])
            previous = page
        else:
            raise Fail(f"initial setup did not finish in 20 pages (last {previous!r})")
        self.wait_user_shell()

    def search_time_zone(self, runner, app, entry):
        """Give gnome-initial-setup's Time Zone page a location through its city search.

        The page completes only once it has a location, from GeoClue (the
        runner's network geolocation) or from that search. Run 45926390728:
        GeoClue found none on two of four legs, Next stayed insensitive and
        the harness clicked the label inside it for three minutes. Type a city
        and take the first completion, as a user without geolocation does."""
        for attempt in range(2):
            self.fill(runner, app, entry, TIMEZONE_CITY)
            time.sleep(3)
            self.key("down")
            self.key("ret")
            got = self.poll(runner, app,
                            lambda n: next_button(n) is not None or not has_text(n, TIMEZONE_SEARCH_HINT), 30)
            if got is not None:
                log(f"  time zone set by searching {TIMEZONE_CITY!r} (attempt {attempt + 1})")
                return got
        nodes = self.snapshot(runner, app)
        self.save("atspi-timeout-time-zone-search.json", json.dumps(nodes, indent=1))
        for line in summarize_nodes(nodes):
            log(f"  atspi [time-zone-search]: {line}")
        self.shot("fail-time-zone-search", f"FAILURE: searching {TIMEZONE_CITY!r} never completed the Time Zone page")
        raise Fail(f"searching {TIMEZONE_CITY!r} never completed the Time Zone page")

    def wait_user_shell(self):
        end = time.time() + 300
        while time.time() < end:
            if self.ssh("root", f"pgrep -u {USER} -x gnome-shell >/dev/null && echo up", check=False).strip() == "up":
                time.sleep(20)
                return
            time.sleep(5)
        self.shot("fail-no-session", f"FAILURE: no GNOME session for {USER}")
        raise Fail(f"no gnome-shell for {USER}")

    def gdm_login(self, phase, label):
        self.key("shift")
        time.sleep(3)
        self.shot(label, f"{phase}: GDM greeter offers the account created by gnome-initial-setup", True, required=True)
        self.key("ret")
        time.sleep(3)
        self.type(PASSWORD, enter=True)
        self.wait_user_shell()

    def close_overview(self):
        """Leave the overview GNOME Shell opens at login, checked through its D-Bus property.

        Run 44866213203: one Escape right after the first login (gnome-initial-setup's
        hand-off) left the overview up. Press Escape like a user until the Shell says
        it is closed; set the property only if keys never take."""
        r = self.user_runner(USER)
        prop = ("gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell "
                "--method org.freedesktop.DBus.Properties.{} org.gnome.Shell OverviewActive{}")
        for attempt in range(5):
            try:
                active = "true" in r(prop.format("Get", ""))
            except Fail as e:
                log(f"  OverviewActive unreadable: {e}")
                self.key("esc")
                time.sleep(3)
                return
            if not active:
                return
            if attempt < 4:
                self.key("esc")
            else:
                log("  overview still active after Escape; closing it through org.gnome.Shell")
                try:
                    r(prop.format("Set", " '<false>'"))
                except Fail as e:
                    log(f"  could not close the overview: {e}")
            time.sleep(3)

    def desktop_and_status(self, phase, prefix):
        # Close the overview (and Shell's first-login welcome dialog) over the desktop.
        self.close_overview()
        self.shot(f"{prefix}-desktop", f"{phase}: GNOME desktop for {USER}", True, required=True)
        status = self.ssh("root", "bootc status", check=False)
        status_json = self.ssh("root", "bootc status --json", check=False)
        self.save(f"bootc-status-{slug(phase)}.txt", status)
        self.save(f"bootc-status-{slug(phase)}.json", status_json)
        digest_cmd = f"echo {PASSWORD} | sudo -S -p '' bootc status"
        self.terminal_shot(prefix, "bootc-status", "bootc status", digest_cmd, f"{phase}: terminal on the desktop running `bootc status`")
        if phase == "first boot":
            luks = (f"echo {PASSWORD} | sudo -S -p '' true; dev=$(lsblk -rnpo NAME,FSTYPE | awk '$2 == \"crypto_LUKS\" {{print $1}}'); "
                    "echo \"== keyslots of $dev\"; sudo systemd-cryptenroll \"$dev\"; echo '== /proc/cmdline'; "
                    "tr ' ' '\\n' </proc/cmdline | grep rd.luks; echo '== unlock'; "
                    "sudo journalctl -b -o cat -u 'systemd-cryptsetup@*' | tail -n 8")
            self.terminal_shot(prefix, "luks", "LUKS2 root", luks,
                               f"{phase}: the root's LUKS2 keyslots (recovery key and "
                               f"{'TPM2, no passphrase' if self.tpm else 'the chosen passphrase'}) and how it was unlocked")
        try:
            return json.loads(status_json)["status"]["booted"]["image"]["imageDigest"]
        except (KeyError, TypeError, json.JSONDecodeError):
            raise Fail(f"{phase}: bootc status --json has no booted image digest")

    def terminal_shot(self, prefix, name, title, cmd, proves):
        unit = f"utah-e2e-term-{prefix}-{name}"
        self.ssh("root", f"systemd-run --machine={USER}@.host --user --collect --unit={unit} "
                         f"python3 /var/tmp/vte_term.py {shlex.quote(title)} {shlex.quote(cmd)}", check=False)
        time.sleep(15)
        self.shot(f"{prefix}-{name}", proves, True)
        self.ssh("root", f"systemctl --machine={USER}@.host --user stop {unit}", check=False)

    def check_flatpaks(self, phase, removed=None):
        """The baked Flatpaks are all there, except the one the user removed."""
        if not self.args.flatpaks:
            return
        baked = [a for a in Path(self.args.flatpaks).read_text().split() if a]
        # flatpak-preinstall.service runs in the background on every boot and
        # has the network here: let it finish, so a removed app it would
        # reinstall is back before we look.
        end = time.time() + 900
        while time.time() < end:
            state = self.ssh("root", "systemctl show -P ActiveState flatpak-preinstall.service", check=False).strip()
            if state not in ("activating", "deactivating"):
                break
            time.sleep(10)
        installed = self.ssh("root", "flatpak list --system --app --columns=application", check=False).split()
        self.save(f"flatpaks-{slug(phase)}.txt", "\n".join(installed) + "\n\n" + self.ssh(
            "root", "journalctl -b -u flatpak-preinstall.service --no-pager -n 40", check=False))
        problems = flatpak_problems(baked, installed, removed)
        self.results.flatpak(phase, installed, problems)
        log(f"  {phase}: {len(installed)} Flatpak apps; problems: {problems or 'none'}")
        if problems:
            raise Fail(f"{phase}: Flatpaks: {'; '.join(problems)}")

    def remove_flatpak(self):
        """Remove one baked app the way a user would; it must stay removed."""
        if not self.args.flatpaks:
            return None
        baked = sorted(a for a in Path(self.args.flatpaks).read_text().split() if a)
        app = self.args.remove_flatpak if self.args.remove_flatpak in baked else baked[0]
        self.save("flatpak-remove.log", self.ssh("root", f"flatpak uninstall --system --noninteractive -y {shlex.quote(app)} 2>&1"))
        log(f"  removed Flatpak {app}")
        return app

    def restart_installed(self, name, tpm):
        """Power the installed system off and boot it again, with or without the TPM."""
        self.stop_vm(lambda: self.ssh("root", "systemctl poweroff --no-block", check=False))
        self.start_vm(name, self.installed_drives, self.vars_installed, self.args.ssh_port + 1, tpm=tpm)

    def installed(self):
        log("=== Stage 3: first boot of the installed disk ===")
        self.results.begin("first boot unlock")
        self.vars_installed = self.work / "vars-installed.fd"
        shutil.copy(self.vars_live, self.vars_installed)
        self.installed_drives = [
            {"drive": f"file={self.work / 'install.qcow2'},format=qcow2,if=none,id=target",
             "device": "virtio-blk-pci,drive=target,bootindex=1"},
        ]
        self.start_vm("installed", self.installed_drives, self.vars_installed, self.args.ssh_port + 1, tpm=self.tpm)
        if self.tpm:
            self.installed_boot("first boot", "12-first-boot-tpm2-unlock", unlock="tpm")
        else:
            # The passphrase gnome-setup's disk passphrase page set.
            self.installed_boot("first boot", "12-first-boot-luks-prompt")
        self.results.begin("initial setup")
        self.initial_setup()
        self.results.begin("first boot desktop")
        self.baseline_digest = self.desktop_and_status("first boot", "14-first-boot")
        self.check_flatpaks("first boot")
        self.check_installed("first boot", "14-first-boot")
        removed = self.remove_flatpak()
        log(f"  baseline digest {self.baseline_digest}")

        log("=== Stage 4: bootc switch to the candidate digest ===")
        self.results.begin("bootc switch")
        start = time.time()
        out = self.ssh("root", f"bootc switch {shlex.quote(self.args.candidate)} 2>&1", timeout=3600, check=False)
        self.save("bootc-switch.log", out)
        self.timings["bootc-switch-seconds"] = int(time.time() - start)
        staged = (json.loads(self.ssh("root", "bootc status --json"))["status"].get("staged") or {})
        staged_digest = (staged.get("image") or {}).get("imageDigest")
        log(f"  staged: {staged_digest}")
        if not staged_digest:
            raise Fail("bootc switch staged nothing (evidence/bootc-switch.log)")
        # Without the TPM, the recovery key gnome-setup showed (the second key when there is no TPM).
        self.restart_installed("installed-switch", tpm=False)
        self.installed_boot("after bootc switch", "20-switch-recovery-key-prompt", unlock="recovery")
        self.gdm_login("after bootc switch", "21-switch-gdm")
        digest = self.desktop_and_status("after bootc switch", "22-switch")
        self.check_flatpaks("after bootc switch", removed)
        if self.candidate_digest and digest != self.candidate_digest:
            raise Fail(f"after switch booted {digest}, expected {self.candidate_digest}")

        log("=== Stage 5: bootc rollback ===")
        self.results.begin("bootc rollback")
        self.save("bootc-rollback.log", self.ssh("root", "bootc rollback 2>&1"))
        self.restart_installed("installed-rollback", tpm=self.tpm)
        if self.tpm:
            self.installed_boot("after bootc rollback", "30-rollback-tpm2-unlock", unlock="tpm")
        else:
            self.installed_boot("after bootc rollback", "30-rollback-luks-prompt")
        self.gdm_login("after bootc rollback", "31-rollback-gdm")
        digest = self.desktop_and_status("after bootc rollback", "32-rollback")
        self.check_flatpaks("after bootc rollback", removed)
        if digest != self.baseline_digest:
            raise Fail(f"after rollback booted {digest}, expected baseline {self.baseline_digest}")
        self.check_installed("after bootc rollback", "32-rollback", repeat=True)
        self.stop_vm(lambda: self.ssh("root", "systemctl poweroff --no-block", check=False))


def norm(text):
    return (text or "").replace("_", "").strip().lower()


def slug(text):
    return "".join(c if c.isalnum() else "-" for c in (text or "page").lower()).strip("-") or "page"


def canon_space(text):
    """AT-SPI text carries glib's size formatting, which separates the value from
    the unit with a non-breaking space ('51.5\\xa0GB'); matchers use plain spaces."""
    return (text or "").replace("\u00a0", " ").replace("\u202f", " ")


def summarize_nodes(nodes):
    """One log line per button, check box and label: name plus showing/sensitive
    states. Timeouts already save the full snapshot as json; this puts the
    actionable part in the CI log itself so the next diagnosis starts there."""
    lines = []
    for n in nodes:
        if n.get("role") not in ("button", "push button", "check box", "label"):
            continue
        states = n.get("states", [])
        lines.append(
            f"{n['role']} name={n.get('name', '')!r} "
            f"showing={'showing' in states} sensitive={'sensitive' in states}")
    return lines or ["no buttons, check boxes or labels"]


def has_text(nodes, text):
    t = canon_space(text).lower()
    return any(t in canon_space(n["name"] + " " + n["text"]).lower() for n in nodes)


def has_name(nodes, name, sensitive=False, role=None):
    return named(nodes, name, sensitive, role) is not None


def named(nodes, name, sensitive=False, role=None):
    # Several nodes can share a name (recovery page: a panel, a button and a
    # label are all 'Done'): pass role='button' for press targets and
    # sensitivity predicates so a sensitive panel cannot stand in for a
    # still-insensitive button (run 44978209923).
    want = norm(name)
    for n in nodes:
        if role is not None and n.get("role") != role:
            continue
        got = norm(n["name"])
        if (want[1:] in got if want.startswith("~") else got == want):
            if not sensitive or "sensitive" in n["states"]:
                return n
    return None


def row_containing(nodes, text):
    """The list row (or, failing that, the node) whose name/text contains text."""
    t = canon_space(text).lower()
    by_path = {n["path"]: n for n in nodes}
    for n in nodes:
        if t not in canon_space(f"{n['name']} {n['text']}").lower() or not n.get("ext"):
            continue
        parts = n["path"].split(".")
        while parts:
            cand = by_path.get(".".join(parts))
            if cand and cand["role"] in ("list item", "table row", "row") and cand.get("ext"):
                return cand
            parts.pop()
        return n
    return None


def subtree(nodes, root):
    """root and the nodes below it (AT-SPI paths are dotted child indices)."""
    prefix = root["path"] + "."
    return [n for n in nodes if n is root or n["path"].startswith(prefix)]


def top_frame(nodes):
    frames = [n for n in nodes if n["role"] in ("frame", "window", "dialog") and n.get("ext") and n["ext"][2] > 0]
    return max(frames, key=lambda n: n["ext"][2] * n["ext"][3]) if frames else None


def next_button(nodes):
    """The page's sensitive Next or Skip button. Never the label inside it: that
    label stays sensitive while its button is not (run 45926390728)."""
    return next((n for n in nodes if n["role"] in ("button", "push button")
                 and norm(n["name"]) in ("next", "skip") and "sensitive" in n["states"]), None)


def page_sig(nodes):
    """What identifies a page: its showing headings and labels."""
    return frozenset((n["role"], n["name"] or n["text"]) for n in nodes if n["role"] in ("heading", "label"))


def page_title(nodes):
    for n in nodes:
        if n["role"] == "heading" and (n["name"] or n["text"]):
            return n["name"] or n["text"]
    labels = [n["name"] or n["text"] for n in nodes if n["role"] == "label" and (n["name"] or n["text"])]
    return labels[0] if labels else "page"


def drawn(ppm, min_colors=8, below_top_bar=False):
    """True when a PPM screendump is more than a flat or near-flat frame.

    below_top_bar: judge only what is below the top tenth, so a frame holding
    nothing but GNOME's top bar is not drawn either."""
    try:
        data = ppm.read_bytes()
    except OSError:
        return False
    if not data.startswith(b"P6"):
        return False
    parts = data.split(b"\n", 3)
    try:
        w, h = map(int, parts[1].split())
        body = parts[3]
    except (ValueError, IndexError):
        return False
    if below_top_bar:
        body = body[(h // 10) * w * 3:]
    step = max(1, (w * h) // 20000) * 3
    colors = {body[i:i + 3] for i in range(0, len(body) - 3, step)}
    return len(colors) >= min_colors


def flavor_of(ref):
    """The configured flavor that publishes ref's image (config/flavors.json, scripts/flavors.py flavor)."""
    r = subprocess.run([sys.executable, str(REPO / "scripts/flavors.py"), "flavor", image_name(ref)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"cannot tell the flavor of {ref} ({r.stderr.strip()}); pass --flavor")
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--iso", help="hybrid ISO of the medium (build-hybrid-iso.sh): the live VM boots it instead of --raw")
    ap.add_argument("--boot-as", choices=BOOT_AS, help="how the live VM attaches --iso: an optical drive or a USB stick")
    ap.add_argument("--work", required=True)
    ap.add_argument("--candidate", required=True, help="image ref (prefer @sha256 digest) for bootc switch")
    ap.add_argument("--ssh-key", required=True, help="private key whose public half is root's authorized key on the image")
    ap.add_argument("--ssh-port", type=int, default=2240)
    ap.add_argument("--ram", type=int, default=6144)
    ap.add_argument("--cpus", type=int, default=4)
    ap.add_argument("--target-size", default="48G")
    ap.add_argument("--install", choices=("gui", "script"), default="gui")
    ap.add_argument("--tpm", action="store_true",
                    help="give the VMs a software TPM 2.0 (swtpm) that persists across them "
                         "(gnome-setup: without it, the disk passphrase page is filled and used)")
    ap.add_argument("--backend", choices=("composefs", "ostree"), default="composefs",
                    help="bootc backend the medium was built with (build-raw-image.sh backend.json)")
    ap.add_argument("--backend-decision",
                    help="build-raw-image.sh's backend.json (scripts/kernel_verity.py), recorded in results.json")
    ap.add_argument("--flatpaks", help="apps baked into the medium (bake-flatpaks.sh flatpak-apps.txt)")
    ap.add_argument("--remove-flatpak", default="org.gnome.Calculator",
                    help="baked app to remove after first boot (default: first baked app if absent)")
    ap.add_argument("--flavor",
                    help="flavor the installed system must be (config/flavors.json); default: the flavor "
                         "that publishes --candidate's image, which is the image under test's repository")
    args = ap.parse_args()
    if (args.iso is None) != (args.boot_as is None):
        ap.error("--iso and --boot-as go together")
    h = Harness(args)
    try:
        h.live()
        if args.install == "gui":
            h.gnome_setup_install()
        else:
            h.gnome_setup_script_install()
        h.installed()
        h.results.finish()
        log("PASS: all stages")
        return 0
    except Exception as e:  # noqa: BLE001
        h.results.finish(e)
        log(f"FAIL: {e}")
        if h.vm is not None and h.vm.poll() is None:
            try:
                h.shot("fail-final", f"FAILURE state: {e}")
            except Exception:  # noqa: BLE001
                pass
            if h.serial is not None and h.serial.name.startswith("live"):
                h.grab_install_log()
                h.grab_sysinstall_journal()
            h.collect_journal("failure")
        return 1
    finally:
        h.write_manifest()
        h.save("timings.json", json.dumps(h.timings, indent=1) + "\n")
        h.save("results.json", json.dumps(h.results.as_dict(h.timings), indent=1) + "\n")
        if h.vm is not None and h.vm.poll() is None:
            h.vm.kill()


if __name__ == "__main__":
    sys.exit(main())
