#!/usr/bin/env python3
"""Find the selected release's kernel in a mounted live image, not the host."""
import argparse
from collections import deque
from pathlib import Path, PurePosixPath
import shutil


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


def bootc_release(root, promote_ogc=False):
    root = Path(root).resolve(strict=True)
    modules = root / "usr/lib/modules"
    receipt = root / "usr/lib/utah/ogc-kernel-release"
    ogc = receipt.read_text().strip() if receipt.is_file() else None
    if ogc:
        # Validate the release and source before removing any base boot files.
        source = kernel_file(root, ogc)
        if source.stat().st_size == 0:
            raise ValueError("Empty OGC kernel")
        target_dir = modules / ogc
        if not target_dir.is_dir() or not target_dir.resolve().is_relative_to(root):
            raise ValueError("OGC module directory is missing or escapes the image")
        if promote_ogc:
            other = [d for d in modules.iterdir() if d.name != ogc]
            if any(not d.resolve().is_relative_to(root) for d in other):
                raise ValueError("Kernel module directory escapes the image")
            target = target_dir / "vmlinuz"
            if target.is_symlink():
                target.unlink()
            if source != target:
                shutil.copy2(source, target)
            for directory in other:
                # Keep module trees needed by module installers and RPM metadata;
                # only the selected kernel is a bootc deployment kernel.
                for name in ("vmlinuz", "initramfs.img"):
                    (directory / name).unlink(missing_ok=True)
    releases = []
    for directory in modules.iterdir():
        try:
            selected = image_file(root, f"/usr/lib/modules/{directory.name}/vmlinuz")
        except FileNotFoundError:
            continue
        if selected.stat().st_size == 0:
            raise ValueError("Empty bootc kernel")
        releases.append(directory.name)
    if len(releases) != 1:
        raise ValueError(f"Expected one bootc kernel, found {releases}")
    if ogc and releases[0] != ogc:
        raise ValueError("OGC flavor still selects the base bootc kernel")
    return releases[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("release", nargs="?")
    parser.add_argument("--select-bootc", action="store_true")
    parser.add_argument("--promote-ogc", action="store_true")
    args = parser.parse_args()
    if args.promote_ogc and not args.select_bootc:
        parser.error("--promote-ogc requires --select-bootc")
    if args.select_bootc:
        print(bootc_release(args.root, args.promote_ogc))
    else:
        print(kernel_file(args.root, args.release))
