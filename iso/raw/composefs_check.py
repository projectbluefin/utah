#!/usr/bin/env python3
"""Runs on the installed system (raw_e2e.py copies it over): composefs facts as JSON.

Usage: composefs_check.py [--read]

Prints the kernel command line, the root mount's source (composefs:<digest>
when bootc's initramfs-setup mounted the composefs image), the number of
objects in /sysroot/composefs/objects and how many carry fs-verity. With
--read it also reads every object end to end, so fs-verity checks each block
against the Merkle tree, and counts read errors and the kernel's
"FILE CORRUPTED" reports for this boot. That is the proof that verity
survived the block copy, the btrfs UUID change and the medium's compression.
raw_e2e.composefs_problems() judges the result.
"""

import fcntl
import json
import os
import struct
import subprocess
import sys
import time

OBJECTS = "/sysroot/composefs/objects"
FS_IOC_GETFLAGS = 0x80086601
FS_VERITY_FL = 0x00100000
CHUNK = 1 << 20


def main(argv):
    read = "--read" in argv[1:]
    facts = {"objects": 0, "verity_objects": 0, "read": read, "read_errors": 0, "read_bytes": 0}
    with open("/proc/cmdline") as f:
        facts["cmdline"] = f.read().strip()
    facts["root_source"] = subprocess.run(["findmnt", "-no", "SOURCE", "/"], capture_output=True,
                                          text=True).stdout.strip()
    start = time.monotonic()
    for top, _, files in os.walk(OBJECTS):
        for name in files:
            facts["objects"] += 1
            try:
                fd = os.open(os.path.join(top, name), os.O_RDONLY)
            except OSError:
                facts["read_errors"] += 1
                continue
            try:
                flags = struct.unpack("q", fcntl.ioctl(fd, FS_IOC_GETFLAGS, b"\0" * 8))[0]
                if flags & FS_VERITY_FL:
                    facts["verity_objects"] += 1
                if read:
                    while chunk := os.read(fd, CHUNK):
                        facts["read_bytes"] += len(chunk)
            except OSError:
                facts["read_errors"] += 1
            finally:
                os.close(fd)
    facts["seconds"] = round(time.monotonic() - start)
    kernel = subprocess.run(["journalctl", "-k", "-b", "-o", "cat", "--no-pager"], capture_output=True,
                            text=True).stdout
    facts["corrupted_reports"] = sum("FILE CORRUPTED" in line for line in kernel.splitlines())
    print(json.dumps(facts))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
