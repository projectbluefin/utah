#!/usr/bin/env python3
"""Boot configuration and layout of the image-based installer's live medium.

build-raw-image.sh partitions the medium and runs `bootc install` with:

  medium.py layout                               the sfdisk script (LAYOUT)
  medium.py install-kargs                        the deployment's kargs, one per line

and, after `bootc install to-filesystem` has written the deployment's BLS
entry and GRUB environment:

  medium.py live-entry ENTRY OUT [EXTRA_KARGS]   write the "Utah Live" entry
  medium.py grubenv GRUBENV ENTRY_ID             make ENTRY_ID GRUB's default

and shrink-raw-image.sh asks it where to cut the image once the root
filesystem has been shrunk to its content:

  medium.py shrink-plan SFDISK_JSON FS_BYTES     print "PARTNO SECTORS IMAGE_BYTES"
  medium.py shrunk-dump DUMP PARTNO SECTORS      the sfdisk dump to write after the cut

and build-hybrid-iso.sh asks it for the hybrid ISO's systemd-boot setup on
the ESP and checks the El Torito catalog systemd-repart wrote:

  medium.py iso-entry LIVE_ENTRY OUT             write systemd-boot's live entry,
                                                 print "ESP_PATH BOOT_PATH" per file to copy
  medium.py iso-loader-conf OUT                  write loader.conf
  medium.py eltorito-rba IMAGE                   print the El Torito EFI image's block

The live entry is the deployment's own entry with `ro utah.live` in place of
`rw` (utah-live-root.service overlays the root with tmpfs in the initrd). Its
version is raised so GRUB's blscfg, which sorts highest first, lists it first.
utah-sysinstall-post removes the entry and the grubenv default from
the installed copy and fails if any entry still carries `utah.live`.
"""

import json
import re
import sys
from pathlib import Path

LIVE_KARG = "utah.live"
LIVE_TITLE = "Utah Live (try or install)"
VERSION_BUMP = 1000
GRUBENV_SIZE = 1024
GRUBENV_HEADER = "# GRUB Environment Block\n"
ALIGN_BYTES = 1024 * 1024
# Room after the last partition for the backup GPT (33 sectors), aligned.
GPT_TAIL_BYTES = ALIGN_BYTES
# The hybrid ISO (build-hybrid-iso.sh): systemd-boot's live entry, the
# kernel and initrd it boots and the medium's own removable-path loader
# (shim) live under /ISO_ESP_DIR on the ESP; utah-sysinstall-post puts shim
# back and removes the directory and /loader from the installed copy.
ISO_ESP_DIR = "utah-live"
ISO_ENTRY_ID = "utah-live.conf"
ISO_LOADER_TIMEOUT = 5
# Covers the ISO9660 volume descriptors (block 16 on) and the boot catalog
# systemd-repart writes right after them.
ISO9660_HEAD = 64 * 1024

# The medium's GPT, in order: (name, partition type, size; None = the rest).
# - BIOS-BOOT only because bootupd installs its BIOS component when it finds
#   one; install.sh drops it (UEFI-only target).
# - A separate ext4 /boot: install.sh encrypts only the root, and GRUB cannot
#   unlock systemd-repart's LUKS2 (argon2id), so /boot must stay outside it.
#   `bootc install to-disk --filesystem btrfs` would put /boot inside the root.
# - The root comes last: install.sh gives it the rest of the target disk and
#   shrink-raw-image.sh cuts the medium right after it.
# build-raw-image.sh formats partitions 2, 3 and 4 as vfat, ext4 and btrfs.
LAYOUT = (
    ("BIOS-BOOT", "21686148-6449-6E6F-744E-656564454649", "1MiB"),
    ("EFI-SYSTEM", "C12A7328-F81F-11D2-BA4B-00A0C93EC93B", "512MiB"),
    ("boot", "0FC63DAF-8483-4772-8E79-3D69D8477DE4", "1GiB"),
    ("root", "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709", None),
)
# rhgb quiet: the graphical, branded Plymouth splash and LUKS prompt on the
# installed disk. Neither the image's kargs.d nor `bootc install` sets them;
# fisherman adds them with bootSplash
# (docs/skills/desktop-contract/references/boot-splash.md). Without them the
# installed disk asked for the passphrase in Plymouth's text mode (spike run
# 44864314405). Install-time kargs carry over bootc upgrades.
INSTALL_KARGS = ("rhgb", "quiet")


def sfdisk_script(layout=LAYOUT) -> str:
    lines = ["label: gpt"]
    for name, ptype, size in layout:
        lines.append(", ".join(([f"size={size}"] if size else []) + [f"type={ptype}", f'name="{name}"']))
    return "\n".join(lines) + "\n"


def live_entry(entry: str, extra_kargs: list[str] | None = None) -> str:
    """The live variant of a BLS entry's text."""
    extra = list(extra_kargs or [])
    out = []
    saw_options = False
    for line in entry.splitlines():
        key, _, value = line.partition(" ")
        value = value.strip()
        if key == "title":
            line = f"title {LIVE_TITLE}"
        elif key == "version" and value.isdigit():
            line = f"version {int(value) + VERSION_BUMP}"
        elif key == "options":
            saw_options = True
            args = [a for a in value.split() if a not in ("rw", "ro", LIVE_KARG)]
            line = "options " + " ".join(args + ["ro", LIVE_KARG] + extra)
        out.append(line)
    if not saw_options:
        raise ValueError("BLS entry has no options line")
    return "\n".join(out) + "\n"


def grubenv(current: str, entry_id: str) -> str:
    """A GRUB environment block whose saved_entry is entry_id.

    GRUB reads the block as exactly GRUBENV_SIZE bytes padded with '#'.
    """
    keep = [line for line in current.split("\n")
            if line and not line.startswith("#") and not line.startswith("saved_entry=")]
    body = GRUBENV_HEADER + "".join(f"{line}\n" for line in keep + [f"saved_entry={entry_id}"])
    if len(body) > GRUBENV_SIZE:
        raise ValueError(f"grubenv content exceeds {GRUBENV_SIZE} bytes")
    return body + "#" * (GRUBENV_SIZE - len(body))


def shrink_plan(table: dict, fs_bytes: int) -> tuple[int, int, int]:
    """Where to cut the medium once the root filesystem is fs_bytes long.

    table is `sfdisk --json` output. The root filesystem is the last
    partition (the installer grows it to fill the target), so the image keeps
    every byte up to the end of a partition just large enough for the
    filesystem, plus the backup GPT. Returns (partition number, new size in
    sectors, new image size in bytes).
    """
    pt = table["partitiontable"]
    sector = int(pt.get("sectorsize", 512))
    parts = sorted(pt["partitions"], key=lambda p: int(p["start"]))
    if not parts:
        raise ValueError("partition table is empty")
    last = parts[-1]
    align = ALIGN_BYTES // sector
    sectors = -(-fs_bytes // sector)
    sectors = -(-sectors // align) * align
    if sectors > int(last["size"]):
        raise ValueError(f"filesystem ({fs_bytes} bytes) does not fit the last partition")
    digits = ""
    for ch in reversed(last["node"]):
        if not ch.isdigit():
            break
        digits = ch + digits
    number = int(digits) if digits else len(parts)
    image_bytes = (int(last["start"]) + sectors) * sector + GPT_TAIL_BYTES
    return number, sectors, image_bytes


PART_LINE = re.compile(r"^(\S*?)(\d+)(\s*:\s*start=\s*\d+,\s*size=\s*)(\d+)(.*)$")


def shrunk_dump(dump: str, partno: int, sectors: int) -> str:
    """An `sfdisk --dump` of the medium with partition partno cut to sectors.

    Written back to the truncated file, sfdisk lays out a fresh primary and
    backup GPT for the new size while keeping the disk and partition UUIDs,
    types and names. `last-lba:` is dropped so sfdisk derives it from the
    file's new size instead of the old one.
    """
    out, found = [], False
    for line in dump.splitlines():
        if line.startswith("last-lba:"):
            continue
        m = PART_LINE.match(line)
        if m and int(m.group(2)) == partno:
            line = f"{m.group(1)}{m.group(2)}{m.group(3)}{sectors}{m.group(5)}"
            found = True
        out.append(line)
    if not found:
        raise ValueError(f"partition {partno} not in the dump")
    if "label: gpt" not in out:
        raise ValueError("not a GPT dump")
    return "\n".join(out) + "\n"


def iso_entry(live: str, esp_dir: str = ISO_ESP_DIR) -> tuple[str, list[tuple[str, str]]]:
    """systemd-boot's Type #1 entry on the hybrid ISO's ESP, from the live BLS entry.

    Same title, version and options as GRUB's live entry; `linux` and
    `initrd` point at copies on the ESP under /esp_dir, because a medium
    booted from El Torito exposes only its ESP to the firmware. Returns the
    entry text and the (ESP path, /boot path) pairs to copy.
    """
    out, files, options = [], [], None
    for line in live.splitlines():
        key, _, value = line.partition(" ")
        value = value.strip()
        if key in ("linux", "initrd"):
            dest = f"/{esp_dir}/{value.rsplit('/', 1)[-1]}"
            if any(d == dest for d, _ in files):
                raise ValueError(f"two boot files named {dest}")
            files.append((dest, value))
            line = f"{key} {dest}"
        elif key == "options":
            options = value.split()
        out.append(line)
    if not any(line.startswith("linux ") for line in out):
        raise ValueError("live entry has no linux line")
    if options is None or LIVE_KARG not in options:
        raise ValueError(f"live entry options lack {LIVE_KARG}")
    return "\n".join(out) + "\n", files


def iso_loader_conf(entry_id: str = ISO_ENTRY_ID, timeout: int = ISO_LOADER_TIMEOUT) -> str:
    """loader.conf on the hybrid ISO's ESP: the live entry is the default,
    the menu shows for timeout seconds, and the auto-detected entries other
    than the firmware setup one stay hidden."""
    return f"default {entry_id}\ntimeout {timeout}\nauto-entries no\nauto-firmware yes\n"


def eltorito_boot_rba(head: bytes) -> int:
    """The load RBA (2048-byte blocks) of the El Torito EFI boot entry in the
    first ISO9660_HEAD bytes of an image, or raise ValueError.

    ECMA-119 puts the volume descriptors at block 16 on; El Torito's boot
    record names the catalog block, whose validation entry (platform 0xEF,
    EFI) is followed by the bootable no-emulation initial entry.
    """
    block = 2048
    for n in range(16, len(head) // block):
        desc = head[n * block:(n + 1) * block]
        if desc[1:6] != b"CD001":
            break
        if desc[0] == 255:
            break
        if desc[0] == 0 and desc[7:30] == b"EL TORITO SPECIFICATION":
            catalog = int.from_bytes(desc[0x47:0x4B], "little")
            entries = head[catalog * block:catalog * block + 64]
            if len(entries) < 64:
                raise ValueError(f"boot catalog at block {catalog} is past the bytes read")
            if entries[0] != 1 or entries[1] != 0xEF or entries[30:32] != b"\x55\xaa":
                raise ValueError("boot catalog has no EFI validation entry")
            if entries[32] != 0x88 or entries[33] != 0:
                raise ValueError("boot catalog's initial entry is not bootable without emulation")
            return int.from_bytes(entries[40:44], "little")
    raise ValueError("no El Torito boot record")


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "iso-entry":
        entry, files = iso_entry(Path(argv[2]).read_text())
        Path(argv[3]).write_text(entry)
        for dest, src in files:
            print(dest, src)
        return 0
    if len(argv) == 3 and argv[1] == "iso-loader-conf":
        Path(argv[2]).write_text(iso_loader_conf())
        return 0
    if len(argv) == 3 and argv[1] == "eltorito-rba":
        with open(argv[2], "rb") as f:
            print(eltorito_boot_rba(f.read(ISO9660_HEAD)))
        return 0
    if len(argv) >= 4 and argv[1] == "live-entry":
        extra = argv[4].split() if len(argv) > 4 else []
        Path(argv[3]).write_text(live_entry(Path(argv[2]).read_text(), extra))
        return 0
    if len(argv) == 4 and argv[1] == "grubenv":
        path = Path(argv[2])
        current = path.read_text() if path.exists() else ""
        path.write_text(grubenv(current, argv[3]))
        return 0
    if len(argv) == 5 and argv[1] == "shrunk-dump":
        sys.stdout.write(shrunk_dump(Path(argv[2]).read_text(), int(argv[3]), int(argv[4])))
        return 0
    if len(argv) == 4 and argv[1] == "shrink-plan":
        table = json.loads(Path(argv[2]).read_text())
        print(*shrink_plan(table, int(argv[3])))
        return 0
    if len(argv) == 2 and argv[1] == "layout":
        sys.stdout.write(sfdisk_script())
        return 0
    if len(argv) == 2 and argv[1] == "install-kargs":
        print("\n".join(INSTALL_KARGS))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
