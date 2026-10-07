#!/usr/bin/env python3
"""Normalize generated metadata before each layer while retaining RPM mtimes."""
import os
import re
from pathlib import Path
import subprocess


def normalize(root, packaged, epoch, mounts=()):
    mounts = set(mounts)
    paths = [root]
    for base in ("usr", "etc", "var", "boot", "tmp", "run"):
        directory = root / base
        if not directory.is_dir() or directory in mounts or directory.is_mount():
            continue
        paths.append(directory)
        for current, directories, files in os.walk(directory, followlinks=False):
            directories[:] = [name for name in directories if Path(current) / name not in mounts and not (Path(current) / name).is_mount()]
            paths.extend(Path(current) / name for name in directories + files)
    for path in paths:
        if (path in mounts or path.is_mount()) and path != root:
            continue
        info = path.lstat()
        name = "/" + str(path.relative_to(root))
        if info.st_mtime_ns != packaged.get(name, -1) * 1_000_000_000:
            os.utime(path, ns=(info.st_atime_ns, epoch * 1_000_000_000), follow_symlinks=False)


def check_rewrite_epoch(packaged, epoch):
    newer = [name for name, timestamp in packaged.items() if timestamp > epoch]
    if newer:
        raise RuntimeError(f"rewrite epoch {epoch} predates RPM payloads: {newer[:5]}")


def main():
    root = Path("/")
    result = subprocess.run(["rpm", "-qa", "--qf", "[%{FILENAMES}\t%{FILEMTIMES}\n]"],
                            check=True, text=True, capture_output=True)
    packaged = {}
    for line in result.stdout.splitlines():
        name, timestamp = line.rsplit("\t", 1)
        packaged[name] = int(timestamp)
    if not packaged:
        raise RuntimeError("RPM returned no payload mtimes")
    # ismount alone cannot detect bind mounts on the same filesystem.
    mounts = {Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]))
              for line in Path("/proc/self/mountinfo").read_text().splitlines()}
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "1704067200"))
    if os.environ.get("UTAH_REWRITE_TIMESTAMPS") == "1":
        check_rewrite_epoch(packaged, epoch)
    normalize(root, packaged, epoch, mounts)


if __name__ == "__main__":
    main()
