"""The ISO's offline payload: size harness, bake parser, and embedded store."""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
import tempfile
import unittest

from test_iso_ci import ROOT


class MeasureIsoStoreTests(unittest.TestCase):
    """#875: the size harness must assemble and squashfs each strategy and emit
    a valid evidence table -- tested with the build tools stubbed so it runs on
    a plain CI runner with no podman/skopeo/mksquashfs installed."""

    SCRIPT = ROOT / "iso/scripts/measure-iso-store.sh"
    EVIDENCE = ROOT / "evidence/iso-size.json"

    def _stubs(self, tmp: Path, record: Path) -> dict[str, str]:
        """A PATH directory of fakes that reproduce the harness's build tools.

        `podman image mount` prints a real directory full of files so the
        `cp -a` and the sha256sum/find dedupe path all operate on real data;
        `mksquashfs` records its args so the test can assert the compression
        axis; `du` returns a fixed byte count for both -b and -sb."""
        mount_dir = tmp / "mount"
        mount_dir.mkdir()
        # UTAH_ISO_WORKDIR must exist so the harness's mktemp -d succeeds;
        # without it measure_strategy aborts and emits no table.
        (tmp / "work").mkdir()
        (mount_dir / "var").mkdir()
        (mount_dir / "etc").mkdir()
        (mount_dir / "etc").joinpath("utah").write_text("utah\n")
        (mount_dir / "etc").joinpath("hostname").write_text("utah\n")
        bin = tmp / "bin"
        bin.mkdir()

        def write(name, body):
            p = bin / name
            p.write_text(body)
            p.chmod(0o755)
            return p

        # The fake podman rejects a multi-line or empty image reference the way
        # real podman does, and `run` creates <graphroot>/<driver> from the
        # mounted storage.conf so the harness's driver check sees a real store.
        write("podman", r"""#!/usr/bin/env bash
check_ref() {
  case "$1" in
    ""|*$'\n'*) echo "invalid reference format: '$1'" >&2; exit 125 ;;
  esac
}
case "$1" in
 image)
  case "$2" in
   mount) check_ref "$3"; printf '%s\n' "$MOUNT_DIR" ;;
   exists) exit 0 ;;
  esac ;;
 unshare) shift; exec "$@" ;;
 build)
  # Real podman build fails without these: the Containerfile COPYs the
  # verifier and configure-live.sh exits without the mascot art.
  ctx="${@: -1}"
  for f in src/utah-verify-desktop-contract.py src/images/utahraptor.png; do
    [[ -s "$ctx/$f" ]] || { echo "build context missing $f" >&2; exit 1; }
  done
  echo "STEP 1/1: FROM fake" ;;
 run)
  shift; store=""; conf=""; image=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -v)
        case "$2" in
          *:/payload-store) store="${2%:/payload-store}" ;;
          *:/tmp/storage.conf:ro) conf="${2%:/tmp/storage.conf:ro}" ;;
        esac
        shift 2 ;;
      --*) shift ;;
      *) image="$1"; break ;;
    esac
  done
  check_ref "$image"
  [[ -z "${NOISY_TOOLS:-}" ]] || echo "Copying blob sha256:abc done"
  driver="${PODMAN_FORCE_DRIVER:-$(sed -n 's/^driver = "\(.*\)"$/\1/p' "$conf")}"
  mkdir -p "$store/$driver" ;;
esac
exit 0
""")
        # NOISY_TOOLS makes skopeo, podman run and mksquashfs print progress on
        # stdout the way the real tools do.
        write("skopeo",
              "#!/usr/bin/env bash\n[[ -z \"${NOISY_TOOLS:-}\" ]] || "
              "printf 'Getting image source signatures\\nWriting manifest to image destination\\n'\n"
              "exit 0\n")
        write("mksquashfs",
              "#!/usr/bin/env bash\n[[ -z \"${MKSQUASHFS_FAIL:-}\" ]] || exit 1\n"
              "[[ -z \"${NOISY_TOOLS:-}\" ]] || "
              "printf 'Parallel mksquashfs: Using 4 processors\\n[=====] 100%%\\n'\n"
              ": > \"$2\"\nprintf '%s\\n' \"$*\" >> \"$RECORD_FILE\"\nexit 0\n")
        write("du", "#!/usr/bin/env bash\nprintf '%s\\t%s\\n' \"$DU_BYTES\" \"$2\"\n")
        env = dict(os.environ)
        env["PATH"] = f"{bin}:{env.get('PATH','')}"
        env["MOUNT_DIR"] = str(mount_dir)
        env["RECORD_FILE"] = str(record)
        env["DU_BYTES"] = "1234567890"
        env["UTAH_ISO_WORKDIR"] = str(tmp / "work")
        # Serve the installer art from a local file:// tree pinned by a temp
        # lock, so fetch-artwork.sh runs for real without the network.
        art_path = ("assets/raster/dinos/jacob-schnurr/utah/utah-look/"
                    "black-outline/utah-look-black-outline.png")
        commit = "0" * 40
        art = tmp / "artwork" / commit / art_path
        art.parent.mkdir(parents=True)
        art.write_bytes(b"\x89PNG fake mascot\n")
        digest = hashlib.sha256(art.read_bytes()).hexdigest()
        lock = tmp / "artwork.lock"
        lock.write_text(f"commit {commit}\n{digest} {art_path}\n")
        env["UTAH_ARTWORK_LOCK"] = str(lock)
        env["UTAH_ARTWORK_BASE"] = f"file://{tmp / 'artwork'}"
        return env

    def test_harness_file_exists_and_is_executable(self):
        self.assertTrue(self.SCRIPT.is_file())
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))

    def test_harness_covers_every_experiment_axis(self):
        text = self.SCRIPT.read_text()
        # storage.conf driver flip (overlay vs vfs) and zstd level plumbing.
        self.assertIn("${STORAGE_CONF}", text)
        self.assertIn("vfs", text)
        self.assertIn("-comp zstd -Xcompression-level", text)
        # The strategy matrix rows: baseline, vfs store, three zstd levels,
        # a wider block, and hardlink dedupe.
        for row in ("baseline", "vfs-store", "zstd-7", "zstd-11", "zstd-15",
                    "block-1m", "hardlink-dedupe"):
            self.assertIn(row, text)

    def test_evidence_file_has_the_harness_schema(self):
        data = json.loads(self.EVIDENCE.read_text())
        self.assertEqual(data["issue"], "projectbluefin/utah#875")
        self.assertIn("strategies", data)
        for entry in data["strategies"]:
            for key in ("flavor", "strategy", "driver", "comp_level",
                        "block_size", "dedupe", "squashfs_bytes",
                        "rootfs_uncompressed_bytes"):
                self.assertIn(key, entry)
        # full_iso_reference is emitted by the harness heredoc, so it must stay
        # in the committed file (finding #1545-7: the harness used to drop it).
        self.assertIn("full_iso_reference", data)

    def test_justfile_wires_the_measure_recipe(self):
        just = (ROOT / "Justfile").read_text()
        self.assertRegex(just, re.compile(r"^measure-iso\s*\*flavors:", re.MULTILINE))
        self.assertIn("iso/scripts/measure-iso-store.sh", just)

    def _run(self, tmp: Path, env: dict[str, str]):
        # OUTPUT_JSON points at a temp file so the stubbed run never touches
        # the committed evidence/iso-size.json.
        out = tmp / "iso-size.json"
        env["OUTPUT_JSON"] = str(out)
        result = subprocess.run(
            ["bash", str(self.SCRIPT), "main=ghcr.io/projectbluefin/utah:testing"],
            capture_output=True, text=True, env=env)
        return result, out

    def test_measure_harness_runs_and_emits_a_valid_table(self):
        original = self.EVIDENCE.read_text()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            record = tmp / "mksquashfs.log"
            env = self._stubs(tmp, record)
            result, out = self._run(tmp, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Building live image", result.stdout)

            data = json.loads(out.read_text())
            self.assertEqual(data["issue"], "projectbluefin/utah#875")
            self.assertIn("full_iso_reference", data)
            self.assertEqual(len(data["strategies"]), 7,
                             "one strategy row per matrix entry")
            for entry in data["strategies"]:
                self.assertEqual(entry["flavor"], "main")
                self.assertIsInstance(entry["squashfs_bytes"], int)
                self.assertIsInstance(entry["rootfs_uncompressed_bytes"], int)
                self.assertNotIn("error", entry)
                self.assertIn(entry["strategy"],
                              {"baseline", "vfs-store", "zstd-7", "zstd-11",
                               "zstd-15", "block-1m", "hardlink-dedupe"})
                self.assertIn(entry["driver"], {"overlay", "vfs"})

            # The compression axis really reached mksquashfs: zstd at the
            # baseline level (3) and the strongest level (15) were used.
            log = record.read_text()
            self.assertIn("-comp zstd", log)
            self.assertIn("-Xcompression-level 3", log)
            self.assertIn("-Xcompression-level 15", log)
        self.assertEqual(self.EVIDENCE.read_text(), original)

    def test_harness_stages_the_live_build_context_like_build_iso(self):
        # iso/live/Containerfile COPYs the verifier and configure-live.sh
        # needs the mascot; neither is committed, so the harness must stage
        # both before podman build and remove them on exit.
        text = self.SCRIPT.read_text()
        self.assertIn("cp scripts/verify-desktop-contract.py "
                      "iso/live/src/utah-verify-desktop-contract.py", text)
        self.assertIn("scripts/fetch-artwork.sh", text)
        self.assertIn("iso/live/src/images/utahraptor.png", text)
        self.assertRegex(text, r"trap cleanup_staged EXIT")
        self.assertLess(text.index("trap cleanup_staged EXIT"),
                        text.index("\n_json_start\n"))

    def test_measure_harness_removes_staged_build_context(self):
        verifier = ROOT / "iso/live/src/utah-verify-desktop-contract.py"
        mascot = ROOT / "iso/live/src/images/utahraptor.png"
        if verifier.exists() or mascot.exists():
            self.skipTest("staged live build-context files already present")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._stubs(tmp, tmp / "mksquashfs.log")
            result, _ = self._run(tmp, env)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(verifier.exists())
        self.assertFalse(mascot.exists())

    def test_measure_harness_records_a_failed_row_and_keeps_the_rest(self):
        # One failing strategy (here the vfs store, whose driver directory
        # never appears) must not discard the other rows: it is recorded with
        # null byte counts and an error, and the run exits non-zero.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._stubs(tmp, tmp / "mksquashfs.log")
            env["PODMAN_FORCE_DRIVER"] = "overlay"
            result, out = self._run(tmp, env)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            rows = {e["strategy"]: e for e in json.loads(out.read_text())["strategies"]}
            self.assertEqual(len(rows), 7)
            self.assertIsNone(rows["vfs-store"]["squashfs_bytes"])
            self.assertIsNone(rows["vfs-store"]["rootfs_uncompressed_bytes"])
            self.assertIn("error", rows["vfs-store"])
            for name, entry in rows.items():
                if name != "vfs-store":
                    self.assertIsInstance(entry["squashfs_bytes"], int, name)

    def test_measure_harness_fails_when_every_strategy_fails(self):
        original = self.EVIDENCE.read_text()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._stubs(tmp, tmp / "mksquashfs.log")
            env["MKSQUASHFS_FAIL"] = "1"
            result, out = self._run(tmp, env)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            for entry in json.loads(out.read_text())["strategies"]:
                self.assertIsNone(entry["squashfs_bytes"])
                self.assertIn("error", entry)
        self.assertEqual(self.EVIDENCE.read_text(), original)

    def test_measure_harness_ignores_tool_progress_on_stdout(self):
        # Real skopeo, in-container skopeo and mksquashfs print progress on
        # stdout; only the byte pair may reach the parser.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._stubs(tmp, tmp / "mksquashfs.log")
            env["NOISY_TOOLS"] = "1"
            result, out = self._run(tmp, env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = json.loads(out.read_text())["strategies"]
            self.assertEqual(len(rows), 7)
            for entry in rows:
                self.assertNotIn("error", entry)
                self.assertEqual(entry["squashfs_bytes"], 1234567890)
                self.assertEqual(entry["rootfs_uncompressed_bytes"], 1234567890)

    def test_measure_harness_preserves_full_iso_reference(self):
        # Only "strategies" is rewritten; hand-filled reference sizes survive.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._stubs(tmp, tmp / "mksquashfs.log")
            existing = json.loads(self.EVIDENCE.read_text())
            existing["full_iso_reference"]["nvidia-gaming"] = {
                "bytes": 9000000000, "source": "hand-filled"}
            (tmp / "iso-size.json").write_text(json.dumps(existing))
            result, out = self._run(tmp, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(out.read_text())
            self.assertEqual(data["full_iso_reference"], existing["full_iso_reference"])
            self.assertEqual(data["note"], existing["note"])
            self.assertEqual(len(data["strategies"]), 7)


class IsoBakeParserTests(unittest.TestCase):
    """The ISO bake must ship the Flatpak parser it calls.

    Post-Testing E2E run 37346082548 failed five cells with exit 127: both
    ISO Containerfiles call
    /usr/local/libexec/utah-verify-desktop-contract, but the stages are FROM
    the shipped image, which strips build-time scripts (clean-stage), and the
    iso/live/ build context cannot reach repo-root scripts/. So the build
    scripts stage the parser into the context (removed by trap) and the
    Containerfiles ship it persistently in the overlay -- which is also what
    puts it on the live guest luks-e2e shells into (absolute path; the
    overlay is not on the default PATH).
    """

    PARSER = "src/utah-verify-desktop-contract.py"
    INSTALLED = "/usr/local/libexec/utah-verify-desktop-contract"
    STAGED = "iso/live/src/utah-verify-desktop-contract.py"

    def test_both_containerfiles_ship_the_parser(self):
        for name in ("iso/live/Containerfile", "iso/live/Containerfile.tacklebox"):
            with self.subTest(containerfile=name):
                text = (ROOT / name).read_text()
                self.assertIn(
                    f"COPY --chmod=0755 {self.PARSER} {self.INSTALLED}", text,
                    f"{name} must ship the parser the bake RUN calls")

    def test_both_build_scripts_stage_and_clean_the_parser(self):
        staged = "cp scripts/verify-desktop-contract.py " + self.STAGED
        for name in ("iso/scripts/build-iso.sh",
                     "iso/scripts/build-iso-tacklebox.sh"):
            with self.subTest(script=name):
                text = (ROOT / name).read_text()
                self.assertIn(staged, text,
                              f"{name} must stage the parser into the build context")
                self.assertIn(self.STAGED, text.split("trap", 1)[1],
                              f"{name} must remove the staged parser on exit")

    def test_staged_copy_is_not_committed(self):
        # The staged file must never exist in the tree: it is build litter
        # the traps remove. The COPY source of truth stays scripts/.
        self.assertFalse((ROOT / self.STAGED).exists(),
                         "staged parser left behind by a build run")

    def test_luks_copies_the_parser_to_the_target(self):
        # /usr/local is the admin's domain, not image content: a bootc
        # deployment never carries /usr/local/libexec, so the verifier the
        # live guest has is absent (exit 127) on the installed system.
        # luks-e2e must copy the repo parser over scp and run it with the
        # target's python3 -- never call the live-only absolute path.
        text = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertIn("scp_target", text,
                      "luks-e2e must define an scp helper for the installed target")
        self.assertIn("scripts/verify-desktop-contract.py", text,
                      "luks-e2e must copy the repo parser to the installed system")
        self.assertIn("python3 /tmp/utah-e2e-verify-desktop-contract.py --flatpaks", text,
                      "luks-e2e must run the copied parser with the target python3")
        self.assertNotIn(f'"{self.INSTALLED} --flatpaks', text,
                         "the live-only installed path does not exist on bootc deployments")
        self.assertNotIn('"utah-verify-desktop-contract --flatpaks', text,
                         "bare parser name is not on the guest PATH")


class EmbedStorePayloadTests(unittest.TestCase):
    """Live media must carry the receipt fisherman requires (PR #1131).

    Fisherman 58eaf29e refuses to install on live media (the live-iso-mode
    marker) unless /etc/bootc-installer/media-input.json binds a verified
    read-only payload: review VM run 43815907502 failed every flavor with
    local_source_unavailable. The OCI-layout form of that receipt broke the
    ISO budget (run 43870670314: 9.6-11.8 GiB), so the payload is an unpacked
    containers-storage graphroot, bound with containersStoragePath. The
    builder runs here with skopeo and podman stubbed, and the receipt is
    checked against fisherman's v1 schema (internal/mediainput,
    PreflightBoundStoreMediaInput).
    """

    SCRIPT = ROOT / "iso/scripts/embed-store-payload.sh"
    LIVE_IMAGE = "localhost/utah-live:testing"

    FAKE_SKOPEO = r"""#!/usr/bin/env python3
import hashlib, json, os, sys
from pathlib import Path
with open(os.environ["SKOPEO_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
src, dest = sys.argv[-2:]
assert dest.startswith("dir:"), dest
export = Path(dest[4:])
export.mkdir(parents=True)
config = json.dumps({"os": "linux", "architecture": "amd64"}).encode()
config_digest = hashlib.sha256(config).hexdigest()
(export / config_digest).write_bytes(config)
layer = b"layer"
layer_digest = hashlib.sha256(layer).hexdigest()
(export / layer_digest).write_bytes(layer)
(export / "manifest.json").write_text(json.dumps({
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {"digest": "sha256:" + config_digest, "size": len(config)},
    "layers": [{"digest": "sha256:" + layer_digest, "size": len(layer)}],
}))
"""

    # Stands in for `podman run <live image> sh -c 'skopeo copy ...; skopeo
    # inspect ...'`: fills the mounted store and reports the store's digest,
    # which is the exported manifest's unless STORE_DIGEST overrides it.
    FAKE_PODMAN = r"""#!/usr/bin/env python3
import hashlib, json, os, sys
from pathlib import Path
with open(os.environ["PODMAN_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
mounts = {}
for flag, value in zip(args, args[1:]):
    if flag == "-v":
        host, guest = value.split(":")[:2]
        mounts[guest] = Path(host)
store = mounts["/payload-store"] / "overlay" / "abc" / "diff"
store.mkdir(parents=True)
(store / "file").write_text("payload")
manifest = (mounts["/payload"] / "manifest.json").read_bytes()
digest = os.environ.get("STORE_DIGEST") or "sha256:" + hashlib.sha256(manifest).hexdigest()
(mounts["/payload-meta"] / "inspect.json").write_text(json.dumps({"Digest": digest}))
Path(os.environ["PODMAN_LOG"] + ".manifest").write_bytes(manifest)
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("skopeo", self.FAKE_SKOPEO), ("podman", self.FAKE_PODMAN)):
            tool = bin_dir / name
            tool.write_text(body)
            tool.chmod(0o755)
        self.root = self.tmp / "root"
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.log = self.tmp / "skopeo.log"
        self.podman_log = self.tmp / "podman.log"
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "SKOPEO_LOG": str(self.log), "PODMAN_LOG": str(self.podman_log)}

    def _live_root(self, ref):
        etc = self.root / "etc/bootc-installer"
        etc.mkdir(parents=True)
        (etc / "recipe.json").write_text(json.dumps(
            {"imgref": ref, "targetImgref": ref, "image": "", "composeFsBackend": False}))
        (etc / "images.json").write_text(json.dumps(
            {"default_image": ref, "images": [{"imgref": ref}]}))

    def _run(self, payload, published, **env):
        return subprocess.run(
            ["bash", str(self.SCRIPT), payload, published, str(self.root), str(self.work),
             self.LIVE_IMAGE],
            capture_output=True, text=True, env={**self.env, **env})

    def test_receipt_binds_the_read_only_store(self):
        ref = "ghcr.io/projectbluefin/utah:review-vm-abc"
        self._live_root(ref)
        result = self._run("localhost/utah:testing", ref)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((self.root / "etc/bootc-installer/media-input.json").read_text())
        podman = json.loads(self.podman_log.read_text().splitlines()[0])
        self.assertIn(self.LIVE_IMAGE, podman)
        self.assertEqual(podman[-1], ref)
        manifest_bytes = Path(f"{self.podman_log}.manifest").read_bytes()
        manifest = json.loads(manifest_bytes)
        self.assertEqual(receipt, {
            "schemaVersion": 1,
            "kind": "utah-live",
            "image": ref,
            "platformManifestDigest": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
            "configDigest": manifest["config"]["digest"],
            "containersStoragePath": "/var/lib/containers/storage",
            "targetImgref": ref,
            "platform": "linux/amd64",
        })
        for digest in (receipt["platformManifestDigest"], receipt["configDigest"]):
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        # The unpacked store lands where the live system bind-mounts it from.
        self.assertEqual(
            (self.root / "usr/lib/containers/storage/overlay/abc/diff/file").read_text(), "payload")
        # Local candidates export their own containers-storage; the store
        # keeps those manifest bytes.
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls[0].split()[-2], "containers-storage:localhost/utah:testing")
        self.assertNotIn("--preserve-digests", calls[0])
        for scratch in ("utah-payload", "payload-store", "payload-meta", "payload-storage.conf"):
            self.assertFalse((self.work / scratch).exists(), scratch)

    def _containers_common_skeleton(self):
        # containers-common's empty additional image store; the review run
        # failed with "already exists" on it.
        store = self.root / "usr/lib/containers/storage"
        for sub, lock in (("overlay-images", "images.lock"), ("overlay-layers", "layers.lock")):
            (store / sub).mkdir(parents=True)
            (store / sub / lock).touch()
        return store

    def test_containers_common_empty_store_is_replaced(self):
        ref = "ghcr.io/projectbluefin/utah:review-vm-abc"
        self._live_root(ref)
        self._containers_common_skeleton()
        result = self._run("localhost/utah:testing", ref)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.root / "usr/lib/containers/storage/overlay/abc/diff/file").read_text(), "payload")
        self.assertTrue((self.root / "etc/bootc-installer/media-input.json").exists())

    def test_a_populated_store_directory_fails_the_build(self):
        ref = "ghcr.io/projectbluefin/utah:review-vm-abc"
        self._live_root(ref)
        store = self._containers_common_skeleton()
        (store / "overlay-images/images.json").write_text("[]")
        result = self._run("localhost/utah:testing", ref)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("holds more than containers-common's empty store", result.stderr)
        self.assertFalse((self.root / "etc/bootc-installer/media-input.json").exists())

    def test_store_digest_must_match_the_exported_manifest(self):
        # Fisherman compares `skopeo inspect containers-storage:<image>` with
        # the receipt; a store that rewrote the manifest would fail there.
        ref = "ghcr.io/projectbluefin/utah:review-vm-abc"
        self._live_root(ref)
        result = self._run("localhost/utah:testing", ref, STORE_DIGEST="sha256:" + "b" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the embedded store reports digest", result.stderr)
        self.assertFalse((self.root / "etc/bootc-installer/media-input.json").exists())

    def test_pinned_payload_copies_registry_blobs(self):
        ref = "ghcr.io/projectbluefin/utah@sha256:" + "a" * 64
        self._live_root(ref)
        result = self._run(ref, ref)
        self.assertEqual(result.returncode, 0, result.stderr)
        first = self.log.read_text().splitlines()[0]
        self.assertIn("--preserve-digests", first)
        self.assertEqual(first.split()[-2], f"docker://{ref}")

    def test_pinned_payload_must_match_the_live_image(self):
        ref = "ghcr.io/projectbluefin/utah@sha256:" + "a" * 64
        self._live_root(ref)
        result = self._run("localhost/utah:testing", ref)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must match", result.stderr)

    def test_recipe_naming_another_image_fails_the_build(self):
        # Fisherman refuses a request naming another image than the receipt's.
        self._live_root("ghcr.io/projectbluefin/utah:testing")
        result = self._run("localhost/utah:testing", "ghcr.io/projectbluefin/utah:review-vm-abc")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recipe.json imgref", result.stderr)
        self.assertFalse((self.root / "etc/bootc-installer/media-input.json").exists())

    def test_live_system_binds_the_store_read_only(self):
        script = (ROOT / "iso/live/src/configure-live.sh").read_text()
        for line in ("What=/run/rootfsbase/usr/lib/containers/storage",
                     "Where=/var/lib/containers/storage",
                     "Options=bind,ro",
                     "systemctl enable var-lib-containers-storage.mount",
                     'graphroot = "/var/lib/containers/live-storage"',
                     'additionalimagestores = ["/var/lib/containers/storage"]'):
            self.assertIn(line, script)
