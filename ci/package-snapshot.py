#!/usr/bin/env python3
"""Prove Utah's RPM transaction can be replayed from a sealed local snapshot.

Run as root with buildah and createrepo_c on a disposable CI runner. This
does not compose, publish, or change Utah's production image.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RPM_FORMAT = "%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*args: str, capture: bool = False) -> str:
    print("+", " ".join(str(arg) for arg in args), flush=True)
    result = subprocess.run(args, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inventory(raw: str) -> list[str]:
    rows = raw.splitlines()
    if not rows or any(len(row.split("|")) != 5 for row in rows):
        raise ValueError("Empty or malformed RPM inventory")
    return sorted(rows)


def verify_inventory(rows: list[str], requested: list[str], versions: dict[str, str]) -> None:
    installed = {}
    for row in rows:
        name, _, version, _, _ = row.split("|")
        installed.setdefault(name, []).append(version)
    missing = sorted(set(requested) - installed.keys())
    if missing:
        raise ValueError(f"Missing contract packages: {missing}")
    for name, major in versions.items():
        if name not in installed or any(v.split(".")[0] != major for v in installed[name]):
            raise ValueError(f"{name}: expected major {major}, got {installed.get(name)}")


def seal(directory: Path, origins: dict[str, str]) -> list[dict]:
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Snapshot contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            files.append({"path": relative, "sha256": digest(path), "bytes": path.stat().st_size,
                          **({"repository": origins[path.name]} if path.suffix == ".rpm" else {})})
    if not files or not any(f["path"].endswith(".rpm") for f in files):
        raise ValueError("Transaction downloaded no RPMs; refusing an empty snapshot proof")
    return files


def verify_seal(directory: Path, files: list[dict]) -> None:
    expected = {f["path"] for f in files}
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Snapshot contains a symlink: {path}")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise ValueError("Snapshot file set changed after sealing")
    for entry in files:
        if Path(entry["path"]).is_absolute() or ".." in Path(entry["path"]).parts:
            raise ValueError("Invalid snapshot path")
        path = directory / entry["path"]
        if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError(f"Snapshot checksum mismatch: {entry['path']}")


def prove(output: Path) -> None:
    installer = load_script("install-packages")
    pins = load_script("check-repo-availability")
    base, factory_image = pins.pinned_inputs(ROOT / "Containerfile")
    overlay = ROOT / "packages/utah.toml"
    repos = installer.install_repos(ROOT / "packages")
    snapshot = output / "repository"
    snapshot.mkdir(parents=True, exist_ok=False)
    containers = []

    def create(image: str) -> str:
        container = run("buildah", "from", "--pull=missing", image, capture=True)
        containers.append(container)
        return container

    def execute(container: str, *command: str, network: str = "private",
                mounts: tuple[tuple[Path, str], ...] = (), capture: bool = False) -> str:
        args = ["buildah", "run", f"--network={network}"]
        for source, target in mounts:
            args += ["--volume", f"{source}:{target}:ro"]
        return run(*args, container, "--", *command, capture=capture)

    try:
        factory = create(factory_image)
        # The engine applies OCI whiteouts, ownership and absolute symlinks.
        # Manually unpacking layers is not equivalent to an OCI root filesystem.
        factory_root = Path(run("buildah", "mount", factory, capture=True))
        factory_repo = factory_root / "repository"
        if not (factory_repo / "repodata/repomd.xml").is_file():
            raise ValueError("Pinned factory image has no RPM repository")
        reference = create(base)
        major = execute(reference, "rpm", "-E", "%fedora", capture=True)
        if not major.isdigit():
            raise ValueError(f"Base image reports invalid Fedora release: {major!r}")
        wanted = installer.contract(ROOT / "packages/bluefin.toml", overlay, major)
        requested = sorted(set(wanted + installer.section(overlay, "build")))
        versions = tomllib.loads(overlay.read_text())["gnome"]["versions"]
        online_mounts = ((ROOT / "packages", "/etc/yum.repos.d"),
                         (factory_repo, "/etc/utah-packages"))
        run("buildah", "copy", reference, str(ROOT / "packages/RPM-GPG-KEY-redhat-release-2"),
            "/etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release-2")
        execute(reference, "dnf5", "clean", "all")
        execute(reference, "dnf5", "-y", "--disablerepo=*",
                *(f"--enablerepo={repo}" for repo in repos), "--setopt=keepcache=True",
                "-x", "PackageKit*", "install", *requested, mounts=online_mounts)
        expected = inventory(execute(reference, "rpm", "-qa", "--qf", RPM_FORMAT, capture=True))
        verify_inventory(expected, requested, versions)
        reference_root = Path(run("buildah", "mount", reference, capture=True))
        origins = {}
        for package in sorted((reference_root / "var/cache").rglob("*.rpm")):
            origin = next((repo for repo in repos
                           if any(part.startswith(repo + "-") for part in package.parts)), None)
            if origin is None or package.name in origins:
                raise ValueError(f"Unknown or duplicate cached RPM: {package}")
            # file:// factory RPMs may be consumed in place, without entering
            # DNF's cache. Their existing OCI digest is already the snapshot.
            if origin == "public-hummingbird-x86_64-rpms":
                origins[package.name] = origin
                shutil.copyfile(package, snapshot / package.name)
        run("createrepo_c", "--checksum", "sha256", str(snapshot))
        files = seal(snapshot, origins)
        lock = {"schema": 1, "utah_sha": run("git", "-c", f"safe.directory={ROOT}",
                                              "-C", str(ROOT), "rev-parse", "HEAD", capture=True),
                "base_image": base, "factory_image": factory_image, "fedora_major": major,
                "repositories": list(repos), "requested": requested, "files": files,
                "expected_inventory": expected,
                "factory_repomd_sha256": digest(factory_repo / "repodata/repomd.xml")}
        (output / "snapshot-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
        # Separate fresh base, sealed Hummingbird RPMs and the same immutable
        # factory image. No live repository and no network.
        replay = create(base)
        repo_config = output / "repos"
        repo_config.mkdir()
        import configparser
        hummingbird = configparser.ConfigParser()
        hummingbird.read(ROOT / "packages/hummingbird.repo")
        if set(hummingbird.sections()) != {"public-hummingbird-x86_64-rpms"}:
            raise ValueError("Unexpected Hummingbird repository set")
        hummingbird["public-hummingbird-x86_64-rpms"]["baseurl"] = "file:///snapshot/repository"
        with (repo_config / "hummingbird.repo").open("w") as handle:
            hummingbird.write(handle)
        shutil.copyfile(ROOT / "packages/utah-packages.repo", repo_config / "utah-packages.repo")
        if set(repos) != {"utah-packages", "public-hummingbird-x86_64-rpms"}:
            raise ValueError(f"Snapshot proof must support all installation repositories: {repos}")
        run("buildah", "copy", replay, str(ROOT / "packages/RPM-GPG-KEY-redhat-release-2"),
            "/etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release-2")
        verify_seal(snapshot, files)
        execute(replay, "dnf5", "-y", "--setopt=reposdir=/snapshot/repos",
                "--disablerepo=*", *(f"--enablerepo={repo}" for repo in repos),
                "-x", "PackageKit*", "install", *requested, network="none",
                mounts=((output, "/snapshot"), (factory_repo, "/etc/utah-packages")))
        actual = inventory(execute(replay, "rpm", "-qa", "--qf", RPM_FORMAT,
                                   network="none", capture=True))
        verify_inventory(actual, requested, versions)
        if actual != expected:
            raise ValueError("Offline replay differs from online reference: "
                             f"missing={sorted(set(expected) - set(actual))}, "
                             f"extra={sorted(set(actual) - set(expected))}")
        verify_seal(snapshot, files)
        report = {"result": "passed", "network": "none", "requested": len(requested),
                  "installed": len(actual), "snapshot_rpms": len(origins),
                  "snapshot_bytes": sum(f["bytes"] for f in files),
                  "inventory_matches_reference": True,
                  "snapshot_lock_sha256": digest(output / "snapshot-lock.json")}
        (output / "snapshot-proof.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    finally:
        for container in reversed(containers):
            subprocess.run(["buildah", "rm", container], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Use sudo: this proof uses rootful buildah to preserve OCI filesystem semantics")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    prove(output)


if __name__ == "__main__":
    main()
