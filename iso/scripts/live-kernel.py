#!/usr/bin/env python3
"""Find the selected release's kernel in a mounted live image, not the host."""
import argparse
from collections import deque
from pathlib import Path, PurePosixPath


def image_file(root, name):
    root = Path(root).resolve(strict=True)
    pending = deque(PurePosixPath(name).parts)
    relative = Path()
    links = 0
    while pending:
        part = pending.popleft()
        if part in {"/", "."}:
            continue
        if part == "..":
            relative = relative.parent
            continue
        candidate = root / relative / part
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise ValueError(f"Too many kernel symlinks inside image: {name}")
            target = candidate.readlink()
            if target.is_absolute():
                relative = Path()
            pending.extendleft(reversed(PurePosixPath(target).parts))
        else:
            if pending and not candidate.is_dir():
                raise FileNotFoundError(f"Missing image directory: {candidate}")
            relative /= part
    result = root / relative
    if not result.is_file():
        raise FileNotFoundError(f"Missing image file: {name}")
    return result


def kernel_file(root, release):
    if not release or release in {".", ".."} or "/" in release:
        raise ValueError("Expected a single kernel release directory name")
    # Fedora kernel-core uses the module directory; Utah's OGC installer uses
    # a versioned /boot file. Never fall back to an unversioned/different kernel.
    for name in [f"/usr/lib/modules/{release}/vmlinuz", f"/boot/vmlinuz-{release}"]:
        try:
            return image_file(root, name)
        except FileNotFoundError:
            continue
    raise FileNotFoundError(f"Missing live kernel for release {release} in {root}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("release")
    args = parser.parse_args()
    print(kernel_file(args.root, args.release))
