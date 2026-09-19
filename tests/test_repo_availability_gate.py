"""`just check-repos` must fail closed, and must resolve against the pins.

`tests/test_package_resolution.py` already covers `pinned_inputs`,
`verified_bytes` and `unpack_metadata`. The two functions that decide whether
the gate is trustworthy -- `repository_metadata`, which decides what bytes the
resolver is allowed to see, and `main`, which decides which inputs the resolver
runs against and whether its verdict reaches CI -- had no executed coverage at
all: every refusal branch below fires only on malformed registry input, which a
green build run never produces.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load("check-repo-availability")

PACKAGE_IMAGE = "ghcr.io/projectbluefin/utah-packages"


def digest_of(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def repodata_layer(size=None, repomd=True):
    """A gzipped metadata layer carrying exactly one repomd.xml."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        directory = tarfile.TarInfo("repository/repodata")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        if repomd:
            entry = tarfile.TarInfo("repository/repodata/repomd.xml")
            entry.size = len(b"<repomd/>")
            archive.addfile(entry, io.BytesIO(b"<repomd/>"))
    raw = stream.getvalue()
    return raw, {"digest": digest_of(raw), "size": len(raw) if size is None else size}


class FakeRegistry:
    """Serves a token, a manifest and blobs over a stub of urlopen."""

    def __init__(self, manifest, blobs, token="pull-token"):
        self.manifest = manifest
        self.blobs = blobs
        self.token = token
        self.requests = []

    def urlopen(self, target, timeout=None):
        if isinstance(target, str):
            self.requests.append((target, {}))
            return io.BytesIO(json.dumps({"token": self.token}).encode())
        self.requests.append((target.full_url, dict(target.headers)))
        if "/manifests/" in target.full_url:
            return io.BytesIO(self.manifest)
        return io.BytesIO(self.blobs[target.full_url.rsplit("/", 1)[1]])


def registry_for(manifest_obj, blobs):
    manifest = json.dumps(manifest_obj).encode()
    return FakeRegistry(manifest, blobs), f"{PACKAGE_IMAGE}@{digest_of(manifest)}"


class RepositoryMetadataTests(unittest.TestCase):
    """The gate reads its repodata from the pinned digest, or not at all."""

    def fetch_into(self, registry, image, destination):
        with patch("urllib.request.urlopen", registry.urlopen):
            checker.repository_metadata(image, destination)

    def test_extracts_the_leading_metadata_layer_of_the_pinned_manifest(self):
        raw, layer = repodata_layer()
        registry, image = registry_for({"layers": [layer]}, {layer["digest"]: raw})
        destination = Path(self.enterContext(tempfile.TemporaryDirectory()))

        self.fetch_into(registry, image, destination)

        self.assertEqual((destination / "repodata/repomd.xml").read_bytes(), b"<repomd/>")

    def test_authorizes_the_blob_reads_with_the_pull_token(self):
        raw, layer = repodata_layer()
        registry, image = registry_for({"layers": [layer]}, {layer["digest"]: raw})
        destination = Path(self.enterContext(tempfile.TemporaryDirectory()))

        self.fetch_into(registry, image, destination)

        token_url, _ = registry.requests[0]
        self.assertIn("scope=repository%3Aprojectbluefin%2Futah-packages%3Apull", token_url)
        authorized = [headers for url, headers in registry.requests[1:]]
        self.assertTrue(authorized)
        for headers in authorized:
            self.assertEqual(headers.get("Authorization"), "Bearer pull-token")

    def test_refuses_a_metadata_layer_without_repomd_xml(self):
        raw, layer = repodata_layer(repomd=False)
        registry, image = registry_for({"layers": [layer]}, {layer["digest"]: raw})
        destination = Path(self.enterContext(tempfile.TemporaryDirectory()))

        with self.assertRaises(ValueError) as refusal:
            self.fetch_into(registry, image, destination)

        self.assertIn("repomd.xml", str(refusal.exception))

    def test_refuses_a_registry_other_than_ghcr(self):
        raw, layer = repodata_layer()
        registry, image = registry_for({"layers": [layer]}, {layer["digest"]: raw})
        destination = Path(self.enterContext(tempfile.TemporaryDirectory()))

        with self.assertRaises(ValueError) as refusal:
            self.fetch_into(registry, image.replace("ghcr.io", "quay.io"), destination)

        self.assertIn("ghcr.io", str(refusal.exception))
        self.assertEqual(registry.requests, [], "refused before contacting the registry")

    def test_refuses_a_manifest_that_does_not_match_the_pinned_digest(self):
        raw, layer = repodata_layer()
        registry, image = registry_for({"layers": [layer]}, {layer["digest"]: raw})
        registry.manifest = json.dumps({"layers": [layer], "tampered": True}).encode()
        destination = Path(self.enterContext(tempfile.TemporaryDirectory()))

        with self.assertRaises(ValueError):
            self.fetch_into(registry, image, destination)

    def test_refuses_a_blob_that_does_not_match_its_layer_digest(self):
        raw, layer = repodata_layer()
        registry, image = registry_for({"layers": [layer]}, {layer["digest"]: raw + b"\0"})
        destination = Path(self.enterContext(tempfile.TemporaryDirectory()))

        with self.assertRaises(ValueError):
            self.fetch_into(registry, image, destination)

    def test_refuses_an_image_whose_leading_layer_is_the_rpm_payload(self):
        """A 64 MiB+ first layer means repodata was not published first."""
        raw, layer = repodata_layer(size=64 * 1024 * 1024 + 1)
        registry, image = registry_for({"layers": [layer]}, {layer["digest"]: raw})
        destination = Path(self.enterContext(tempfile.TemporaryDirectory()))

        with self.assertRaises(ValueError) as refusal:
            self.fetch_into(registry, image, destination)

        self.assertIn("republish", str(refusal.exception))
        self.assertFalse(
            (destination / "repodata").exists(),
            "refused without unpacking anything",
        )

    def test_accepts_a_leading_layer_at_the_size_ceiling(self):
        raw, layer = repodata_layer(size=64 * 1024 * 1024)
        registry, image = registry_for({"layers": [layer]}, {layer["digest"]: raw})
        destination = Path(self.enterContext(tempfile.TemporaryDirectory()))

        self.fetch_into(registry, image, destination)

        self.assertTrue((destination / "repodata/repomd.xml").is_file())


class MainTests(unittest.TestCase):
    """The CLI resolves against the pins and hands CI the resolver's verdict."""

    def run_main(self, argv, returncode=0):
        """Run main() with the registry read and the engine stubbed out."""
        calls = {}

        def fake_metadata(image, destination):
            calls["image"] = image
            calls["destination"] = Path(destination)
            (Path(destination) / "repodata").mkdir(parents=True, exist_ok=True)

        def fake_run(command, **kwargs):
            calls["command"] = list(command)
            calls["metadata_present"] = (calls["destination"] / "repodata").is_dir()
            return subprocess.CompletedProcess(command, returncode)

        with patch.object(checker, "repository_metadata", fake_metadata), \
                patch("subprocess.run", fake_run), \
                patch.object(sys, "argv", ["check-repo-availability.py", *argv]), \
                contextlib.redirect_stdout(io.StringIO()):
            calls["exit"] = checker.main()
        return calls

    def manifest(self):
        return str(ROOT / "packages/bluefin.toml")

    def test_resolves_against_the_digest_pinned_images_from_the_containerfile(self):
        args = dict(re.findall(r"^ARG ([A-Z_]+)=(\S+)$", (ROOT / "Containerfile").read_text(), re.M))
        calls = self.run_main([self.manifest()])

        self.assertEqual(calls["image"], f"{args['PACKAGE_IMAGE']}@{args['PACKAGE_IMAGE_SHA']}")
        self.assertEqual(calls["command"][0], "podman")
        self.assertIn(args["BASE_IMAGE"], calls["command"])

    def test_defaults_the_overlay_to_utah_toml_beside_the_manifest(self):
        calls = self.run_main([self.manifest()])

        self.assertIn(
            f"{ROOT / 'packages/utah.toml'}:/tmp/utah.toml:ro,Z",
            calls["command"],
        )

    def test_honours_an_explicitly_named_overlay(self):
        overlay = ROOT / "packages/bluefin.toml"
        calls = self.run_main([self.manifest(), str(overlay)])

        self.assertIn(f"{overlay}:/tmp/utah.toml:ro,Z", calls["command"])
        self.assertNotIn(
            f"{ROOT / 'packages/utah.toml'}:/tmp/utah.toml:ro,Z",
            calls["command"],
        )

    def test_mounts_the_verified_metadata_the_resolver_reads(self):
        calls = self.run_main([self.manifest()])

        self.assertIn(
            f"{calls['destination']}:/etc/utah-packages:ro,Z",
            calls["command"],
            "the resolver must read the digest-verified repodata, not a stale copy",
        )
        self.assertTrue(calls["metadata_present"], "metadata is fetched before the engine runs")

    def test_resolves_rather_than_installs(self):
        calls = self.run_main([self.manifest()])
        command = calls["command"]

        self.assertEqual(
            command[-4:],
            ["/tmp/install-packages.py", "--resolve", "/tmp/bluefin.toml", "/tmp/utah.toml"],
        )
        self.assertIn(f"{ROOT / 'packages'}:/etc/yum.repos.d:ro,Z", command)
        self.assertIn(
            f"{ROOT / 'scripts/install-packages.py'}:/tmp/install-packages.py:ro,Z",
            command,
        )
        self.assertIn("linux/amd64", command)

    def test_honours_an_alternate_engine(self):
        calls = self.run_main([self.manifest(), "--engine", "docker"])

        self.assertEqual(calls["command"][0], "docker")

    def test_reports_the_resolver_verdict_to_ci(self):
        for returncode in (0, 1, 125):
            with self.subTest(returncode=returncode):
                calls = self.run_main([self.manifest()], returncode=returncode)
                self.assertEqual(calls["exit"], returncode)


if __name__ == "__main__":
    unittest.main()
