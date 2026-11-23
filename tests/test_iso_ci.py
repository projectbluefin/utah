"""CI must test the complete exact-digest set before publication."""
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "digests.txt"
        self.digest = "sha256:" + "a" * 64
        self.path.write_text(f"utah={self.digest}\nutah|amd64|{self.digest}\n")
        self.run = dict(conclusion="success", head_branch="testing", event="workflow_dispatch",
                        repository={"full_name": "projectbluefin/utah"},
                        head_repository={"full_name": "projectbluefin/utah"},
                        path=".github/workflows/build.yml", head_sha="b" * 40)

    def resolve(self, expected=None):
        return load("resolve-e2e-inputs").resolve(
            self.run, expected or ["utah"], self.tmp.name, "projectbluefin/utah")

    def test_dual_artifact_formats_agree(self):
        self.assertEqual(self.resolve()["include"][0]["ref"],
                         f"ghcr.io/projectbluefin/utah@{self.digest}")

    def test_rejects_untrusted_or_failed_builds(self):
        for field, value in [("event", "pull_request"), ("conclusion", "failure"),
                             ("head_branch", "main"), ("head_sha", "bad"),
                             ("path", "other.yml"),
                             ("head_repository", {"full_name": "attacker/utah"})]:
            with self.subTest(field=field):
                old = self.run[field]
                self.run[field] = value
                with self.assertRaises(ValueError):
                    self.resolve()
                self.run[field] = old

    def test_missing_flavor_is_fatal(self):
        with self.assertRaises(ValueError):
            self.resolve(["utah", "second-image"])

    def test_rejects_conflicts_tags_unknown_names_and_architectures(self):
        for line in ["utah=latest", "intruder=" + self.digest,
                     "utah|arm64|" + self.digest, "utah=sha256:" + "c" * 64]:
            with self.subTest(line=line):
                self.path.write_text(f"utah={self.digest}\n{line}\n")
                with self.assertRaises(ValueError):
                    self.resolve()


class EvidenceTests(unittest.TestCase):
    def test_privileged_tacklebox_consumers_share_one_digest_pin(self):
        pin = (ROOT / "config/tacklebox-image").read_text().strip()
        self.assertRegex(pin, r"^ghcr.io/tuna-os/tacklebox:latest@sha256:[a-f0-9]{64}$")
        for path in ("iso/scripts/build-iso-tacklebox.sh", ".github/workflows/post-testing-e2e.yml"):
            source = (ROOT / path).read_text()
            self.assertIn("config/tacklebox-image", source)
            self.assertNotIn("ghcr.io/tuna-os/tacklebox:latest", source)
        managers = json.loads((ROOT / "renovate.json").read_text())["customManagers"]
        self.assertTrue(any("/^config/tacklebox-image$/" in m["managerFilePatterns"] for m in managers))

    def test_ogc_config_gate_rejects_each_missing_live_boot_feature(self):
        script = (ROOT / "scripts/install-ogc-kernel.sh").read_text()
        # Execute only the pure config gate, never the package/kernel installer.
        gate = "required_config=" + script.split("required_config=", 1)[1].split(
            '\nif [ -f "${CACHE_DIR}/ogc.tar" ]; then', 1)[0]
        names = gate.split("(", 1)[1].split(")", 1)[0].split()
        for required in ["OVERLAY_FS", "SQUASHFS", "SQUASHFS_ZSTD", "EROFS_FS",
                         "BTRFS_FS", "BLK_DEV_LOOP", "DM_SNAPSHOT", "DM_CRYPT",
                         "CRYPTO_XTS", "FUSE_FS", "FS_VERITY", "SYSFB_SIMPLEFB",
                         "DRM_SIMPLEDRM", "VIDEO_DEV", "DRM_AMDGPU", "DRM_XE"]:
            self.assertIn(required, names)
            self.assertRegex(script, rf"--(?:enable|module) {required}(?:\s|$)")
        self.assertEqual(script.count("verify_config /usr/lib/utah/ogc-kernel.config"), 2)
        self.assertIn("make olddefconfig", script.split("verify_config .config")[0])
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            for missing in [None, *names]:
                with self.subTest(missing=missing):
                    config.write_text("".join(f"CONFIG_{name}=y\n"
                                              for name in names if name != missing))
                    result = subprocess.run(
                        ["bash", "-eu", "-c", gate + '\nverify_config "$1"',
                         "config-test", str(config)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0 if missing is None else 1)
                    if missing:
                        self.assertIn(f"CONFIG_{missing}", result.stderr)

    def test_luks_harness_preflights_the_btrfs_live_kernel_module(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        preflight = "if ! ssh_live 'sudo modprobe btrfs'; then"
        self.assertIn(preflight, script)
        self.assertIn("live kernel cannot load the btrfs module", script)
        self.assertLess(script.index(preflight),
                        script.index("sudo /usr/local/bin/fisherman"))

    def test_offline_payload_preserves_manifest_digest(self):
        build = (ROOT / "iso/scripts/build-iso.sh").read_text()
        self.assertIn('bash iso/scripts/embed-store-payload.sh "${PAYLOAD_IMAGE}" '
                      '"${PUBLISHED_IMAGE}" "${SQUASHFS_ROOT}" "${WORK}" "${LIVE_IMAGE}"', build)
        script = (ROOT / "iso/scripts/embed-store-payload.sh").read_text()
        self.assertNotIn("oci-archive:", script)
        self.assertNotIn("--dest-compress", script)
        # An OCI layout's tar blobs cannot share squashfs blocks with the live
        # root: run 43870670314's ISOs were 9.6-11.8 GiB with one.
        self.assertNotIn('"oci:', script)
        self.assertEqual(script.count("--preserve-digests"), 2)
        self.assertIn('"dir:${PAYLOAD_EXPORT}"', script)
        self.assertIn('skopeo copy --preserve-digests dir:/payload "containers-storage:$1"', script)

    def test_iso_compression_level_reaches_unshare_assembly(self):
        script = (ROOT / "iso/scripts/build-iso.sh").read_text()
        self.assertIn('"${ISO_MAX_GB}" "${ISO_COMPRESSION_LEVEL}" <<\'ASSEMBLY\'', script)
        self.assertIn('ISO_COMPRESSION_LEVEL="$9"', script)

    def test_production_boot_args_and_unsupported_paths(self):
        script = (ROOT / "iso/scripts/build-iso.sh").read_text()
        self.assertIn("enforcing=0", script)
        self.assertIn("Documented exception (Issue #22)", script)
        self.assertNotIn("rd.utah.isofile", script)
        self.assertNotIn("loopback.cfg", script)
        self.assertIn("root=live:LABEL=${LABEL}", script)
        self.assertIn("rd.live.image", script)
        self.assertIn("rd.live.overlay.overlayfs=1", script)

    def test_terminal_autostart_forces_software_gl_rendering(self):
        # #183: Ghostty computes GDK_DISABLE from a hardcoded struct and
        # setenv(3)s it with overwrite=1 right before gtk_init (upstream
        # src/apprt/gtk/class/application.zig, gtk_ghostty_application
        # scope), so neither this script nor a `flatpak override --env` can
        # steer it: an upstream build on 2026-09-20 dropped `gles-api` from
        # that struct, and every flavor's terminal exited without ever
        # mapping a window under QEMU's GPU-less VGA device ("MESA: error:
        # ZINK: failed to choose pdev", then "gtk_ghostty_surface: failed to
        # initialize surface"). LIBGL_ALWAYS_SOFTWARE and
        # MESA_LOADER_DRIVER_OVERRIDE are never among the variables Ghostty
        # itself setenv(3)s (only LANG, GDK_DEBUG and GDK_DISABLE are), so
        # they survive into the sandboxed process and force llvmpipe
        # directly -- assert the fix that actually reaches the process, not
        # a GDK_DISABLE override Ghostty is proven to clobber.
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        marker = "cat > ~/.config/autostart/"
        autostart = script[script.index(marker):script.index("\nEOF", script.index(marker))]
        exec_line = next(l for l in autostart.splitlines() if l.startswith("Exec="))
        self.assertNotIn("GDK_DISABLE", exec_line)
        self.assertIn("env LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=llvmpipe ",
                       exec_line)
        self.assertTrue(exec_line.endswith("flatpak --system run ${TERMINAL_APP}"))
        # The disproven `flatpak override --env=GDK_DISABLE=...` route (see
        # comment above) was added and reverted during review; assert it
        # never comes back in the installer script either.
        installer = (ROOT / "iso/live/src/install-flatpaks.sh").read_text()
        self.assertNotIn("GDK_DISABLE", installer)

    def test_desktop_screenshot_precedes_terminal_trigger(self):
        # #240: installed-desktop.png and installed-fastfetch.png must capture
        # distinct states. The clean desktop shot must be taken before touching
        # the trigger that releases Ghostty, and the harness must assert the
        # two resulting screenshots are not byte-identical.
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        desktop_shot = script.index('shot installed-desktop "${MONITOR_INSTALLED}"')
        trigger = script.index('ssh_target "touch /tmp/utah-e2e-open-terminal"')
        fastfetch_shot = script.index('shot installed-fastfetch "${MONITOR_INSTALLED}"')
        self.assertLess(desktop_shot, trigger, "desktop shot must precede terminal launch")
        self.assertLess(trigger, fastfetch_shot, "terminal trigger must precede fastfetch shot")
        self.assertIn("installed-desktop.png and installed-fastfetch.png are byte-identical", script)

    def test_local_fastfetch_capture_retries_on_duplicate(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertIn("retrying capture after 10s", script)

    def test_iso_budget_guard_fails_closed_above_ceiling(self):
        # The budget guard (#128) is the whole point of the size drift this PR
        # closes. Extract the real block and run it with du stubbed so we can
        # drive both the byte count (-b) and the human size (-sh) without a
        # real ISO on disk -- the test exercises the logic, not a copy of it.
        script = (ROOT / "iso/scripts/build-iso.sh").read_text()
        # The guard must receive ISO_MAX_GB the way the real script delivers it:
        # as a positional arg into the <<'ASSEMBLY' heredoc, not from the outer
        # shell's environment. Assert that plumbing exists so a regression back
        # to an unexported, unpassed variable (which dies under set -u inside
        # the assembly) is caught here before it breaks every ISO build.
        self.assertRegex(script, r"podman unshare bash -s -- .*\$\{ISO_MAX_GB\}")
        self.assertIn('ISO_MAX_GB="${UTAH_ISO_MAX_GB:-8}"', script)
        self.assertIn('ISO_MAX_GB="$8"', script)
        start = script.index("iso_max_bytes=$(( ISO_MAX_GB")
        end = script.index("\nfi\n", start) + len("\nfi\n")
        guard = script[start:end]
        run = (
            "du() { if [ \"$1\" = \"-b\" ]; then echo \"$DU_BYTES\"; "
            "else echo \"$DU_HUMAN\"; fi; };\n"
            "OUTPUT_ISO=/tmp/utah-fakeiso\n"
            + guard
        )
        under = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "ISO_MAX_GB": "6", "DU_BYTES": str(5 * 1024 ** 3), "DU_HUMAN": "5.0G"}
        result = subprocess.run(["bash", "-eu", "-c", run], capture_output=True, text=True, env=under)
        self.assertEqual(result.returncode, 0, result.stderr)
        over = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "ISO_MAX_GB": "6", "DU_BYTES": str(6 * 1024 ** 3 + 512 * 1024 ** 2), "DU_HUMAN": "6.5G"}
        result = subprocess.run(["bash", "-eu", "-c", run], capture_output=True, text=True, env=over)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("exceeds 6 GB budget", result.stderr)

    def test_release_gate_runs_the_testsuite_commit_post_testing_validated(self):
        # A gate on a different testsuite commit can pass or fail on tests the
        # promoted image never ran (#773). Bump both refs together.
        import yaml
        e2e = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())
        release = yaml.safe_load((ROOT / ".github/workflows/execute-release.yml").read_text())
        validated = e2e["jobs"]["testsuite"]["with"]["test_ref"]
        gated = release["jobs"]["execute"]["with"]["gate_test_ref"]
        self.assertRegex(validated, r"^[0-9a-f]{40}$")
        self.assertEqual(gated, validated)

    def test_testsuite_checkout_matches_the_pinned_workflow_commit(self):
        # Workflow logic and checked-out tests must come from one testsuite
        # revision (#946); Renovate moves the uses: digest and test refs together.
        import yaml
        e2e = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())
        job = e2e["jobs"]["testsuite"]
        workflow_sha = job["uses"].rpartition("@")[2]
        self.assertEqual(job["with"]["test_ref"], workflow_sha)
        manager = next(m for m in json.loads((ROOT / "renovate.json").read_text())["customManagers"]
                       if m.get("depNameTemplate") == "projectbluefin/testsuite")
        for name in ["post-testing-e2e", "execute-release", "candidate-common"]:
            text = (ROOT / f".github/workflows/{name}.yml").read_text()
            pattern = re.sub(r"\(\?<(\w+)>", "(?P<\\1>", manager["matchStrings"][0])
            self.assertEqual(re.search(pattern, text)["currentDigest"], workflow_sha, name)
            self.assertTrue(any(re.search(p.strip("/"), f".github/workflows/{name}.yml")
                                for p in manager["managerFilePatterns"]), name)
        # The candidate workflow's testsuite job holds packages: write, so it
        # calls the same reviewed testsuite commit, never an unmerged one (#1214).
        candidate = yaml.safe_load((ROOT / ".github/workflows/candidate-common.yml").read_text())
        job = candidate["jobs"]["testsuite"]
        self.assertEqual(job["uses"].rpartition("@")[2], workflow_sha)
        self.assertEqual(job["with"]["test_ref"], workflow_sha)

    def test_build_explicitly_dispatches_iso_after_both_image_jobs(self):
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        job = build["jobs"]["dispatch-iso"]
        self.assertEqual(set(job["needs"]), {"build_main", "build_kernel", "verify_revision"})
        self.assertIn("refs/heads/testing", job["if"])
        self.assertIn("needs.build_main.result == 'success'", job["if"])
        self.assertIn("needs.build_kernel.result == 'success'", job["if"])
        # A pushed image labelled with another commit never reaches E2E (#1468).
        self.assertIn("needs.verify_revision.result == 'success'", job["if"])
        self.assertIn("build_run_id=$GITHUB_RUN_ID", job["steps"][0]["run"])
        workflow = (ROOT / ".github/workflows/post-testing-e2e.yml").read_text()
        self.assertNotIn("workflow_run:", workflow)
        self.assertIn('timeout 300 gh run watch "$BUILD_RUN"', workflow)

    def test_readme_update_is_idempotent_and_preserves_other_text(self):
        update = load("update-e2e-readme").update
        proof = {"source_sha": "a" * 40, "e2e_run": "123"}
        record_commit = "b" * 40
        text = "# Utah\n\nKeep this paragraph.\n"
        result = update(text, proof, record_commit)
        self.assertEqual(update(result, proof, record_commit), result)
        self.assertIn("Keep this paragraph.", result)
        self.assertIn("actions/runs/123", result)
        # The evidence links pin to the record's own commit, not the mutable
        # `verification` branch, so a frozen block stays self-consistent.
        self.assertIn(f"raw/{record_commit}/screenshots", result)
        self.assertIn(f"tree/{record_commit}", result)
        # The record now lives on the `verification` branch, so the block must
        # not embed any local docs/verification/ path.
        self.assertNotIn("docs/verification/", result)
        self.assertIn("independent desktop tests that do not gate this record", result)
        self.assertNotIn("failure", result)
        self.assertIn("the LUKS test passed on the production ISO it retains", result)

    def test_the_retained_production_iso_is_the_media_luks_tested(self):
        # #1448: the luks job boots the DEBUG=0 ISO itself, reaching its live
        # session over vsock, and keeps those bytes -- not a later rebuild.
        import yaml
        jobs = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())["jobs"]
        self.assertNotIn("production-iso", jobs)
        steps = jobs["luks"]["steps"]
        names = [step.get("name") for step in steps]
        compose = steps[names.index("Compose production ISO")]
        self.assertIn('"$PWD/output/utah-live.iso"', compose["run"])
        self.assertIn('"Utah Live" 0 "$IMAGE_REF"', compose["run"])
        test = steps[names.index("Run existing LUKS install and desktop test")]
        self.assertIn('luks-e2e.sh "$PWD/output/utah-live.iso"', test["run"])
        self.assertEqual(test["env"]["UTAH_E2E_LIVE_SSH"], "vsock")
        record = steps[names.index("Record exact test provenance")]
        self.assertIn("cd output && sha256sum utah-live.iso > utah-live.iso.sha256", record["run"])
        artifact = steps[names.index("Retain the tested production ISO")]
        self.assertGreater(names.index("Retain the tested production ISO"), names.index("Run existing LUKS install and desktop test"))
        self.assertNotIn("if", artifact)
        self.assertEqual(artifact["with"]["name"], "production-iso-${{ matrix.image }}")
        self.assertIn("output/utah-live.iso", artifact["with"]["path"])
        self.assertIn("output/utah-live.iso.sha256", artifact["with"]["path"])
        for name in ["promote-to-testing", "documentation"]:
            self.assertIn("luks", jobs[name]["needs"])
            self.assertIn("needs.luks.result == 'success'", jobs[name]["if"])

    def test_e2e_live_isos_are_production_builds_reached_over_vsock(self):
        import yaml
        workflows = {name: yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())["jobs"]
                     for name in ["post-testing-e2e", "candidate-common", "build"]}
        harness_jobs = [(workflows["post-testing-e2e"], "luks"), (workflows["post-testing-e2e"], "lifecycle"),
                        (workflows["candidate-common"], "candidate"), (workflows["build"], "review_vm")]
        for jobs, name in harness_jobs:
            with self.subTest(job=name):
                steps = jobs[name]["steps"]
                runs = [step for step in steps if "-e2e.sh" in step.get("run", "")]
                self.assertTrue(runs)
                for step in runs:
                    self.assertEqual(step["env"]["UTAH_E2E_LIVE_SSH"], "vsock")
                composes = [step["run"] for step in steps if "iso/scripts/build-iso.sh" in step.get("run", "")]
                self.assertTrue(composes)
                for run in composes:
                    self.assertRegex(run, r'build-iso\.sh [^\n]*(\\\n[^\n]*)?" 0 "\$')
                host = "\n".join(step.get("run", "") for step in steps)
                self.assertIn("sudo modprobe vhost_vsock", host)
                self.assertIn("test -w /dev/vhost-vsock", host)

    def test_the_iso_record_pr_is_opened_as_mergeraptor(self):
        # A PR opened with the workflow token gets no pull_request checks and
        # an untrusted author, so it waits for a hand merge. The App token
        # opens it instead; the workflow token keeps only the record push.
        import yaml
        job = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())["jobs"]["documentation"]
        self.assertNotIn("pull-requests", job["permissions"])
        steps = {step.get("name"): step for step in job["steps"]}
        self.assertIn("actions/create-github-app-token@", steps["Get repository-scoped mergeraptor token"]["uses"])
        self.assertEqual(
            steps["Propose updated verification link"]["with"]["token"],
            "${{ steps.app-token.outputs.token }}",
        )
        guard = "github.repository_owner == 'projectbluefin' && github.ref == 'refs/heads/main'"
        for name in ["Get repository-scoped mergeraptor token", "Propose updated verification link"]:
            self.assertEqual(steps[name].get("if"), guard, name)

    def test_test_disks_are_not_uploaded(self):
        import yaml
        jobs = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())["jobs"]
        steps = jobs["luks"]["steps"]
        self.assertFalse(jobs["luks"]["strategy"]["fail-fast"])
        test = next(step for step in steps if "Run existing LUKS" in step.get("name", ""))
        self.assertEqual(test["env"]["UTAH_E2E_REQUIRE_FASTFETCH"], "1")
        uploads = [s for s in steps if "upload-artifact@" in s.get("uses", "")]
        for step in uploads:
            self.assertNotIn("qcow2", step["with"]["path"])
            self.assertNotIn("vsock-key", step["with"]["path"])
            # Only the production ISO leaves output/, and only from a passing leg.
            if "output/" in step["with"]["path"]:
                self.assertEqual(step["name"], "Retain the tested production ISO")
                self.assertNotIn("if", step)
        self.assertTrue(any(s.get("if") == "always()" for s in uploads))

    def test_harness_restricts_both_guests_and_requires_real_screenshots(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertEqual(script.count("restrict=on,hostfwd=tcp:127.0.0.1:"), 2)
        self.assertIn("systemd.wants=sshd.service", script)
        self.assertIn("fastfetch output was not visible", script)
        self.assertIn("missing required screenshot", script)
        # #786: the gate must also reject a blank/frozen frame, not only an
        # empty one. screenshot_not_blank is called for every required label.
        gate = script[script.index("UTAH_E2E_REQUIRE_SCREENSHOTS"):script.index(
            "installed-desktop.png and installed-fastfetch.png")]
        self.assertIn("screenshot_not_blank", gate)
        # Defined once, called once inside the per-label loop.
        self.assertEqual(script.count("screenshot_not_blank()"), 1)
        self.assertEqual(gate.count("screenshot_not_blank"), 1)
        # #901: live-desktop is held to the retake loop's body-mode check.
        self.assertIn('if [[ "${label}" == live-desktop ]]; then mode=body; fi', gate)
        self.assertIn('screenshot_not_blank "${SHOTS}/${label}.png" "${mode}"', gate)

    def _make_png(self, path, solid, top_bar_only=False):
        import struct, zlib
        w, h = 1920, 1080
        if top_bar_only:
            # A busy 32px strip over a flat black body: gnome-shell has drawn
            # its top bar but nothing else yet.
            px = [((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256)
                  for y in range(32) for x in range(w)]
            px += [(0, 0, 0)] * (w * (h - 32))
        elif solid:
            px = [(0, 0, 0)] * (w * h)
        else:
            # A busy gradient: thousands of distinct colours, unlike a frozen
            # frame. The point is that it is nowhere near a single colour.
            px = []
            for y in range(h):
                for x in range(w):
                    px.append(((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256))
        raw = bytearray()
        for y in range(h):
            raw.append(0)
            for x in range(w):
                r, g, b = px[y * w + x]
                raw += bytes((r, g, b))
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        def chunk(ct, data):
            body = ct + data
            return struct.pack(">I", len(data)) + body + struct.pack(
                ">I", zlib.crc32(body) & 0xffffffff)
        blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))
        path.write_bytes(blob)

    def test_a_blank_screenshot_is_rejected_and_a_real_one_passes(self):
        # #786: the screenshot gate only asserted -s (non-empty) per label, so a
        # solid-black capture of a few kilobytes passed the gate and was
        # published as proof a session started. The blank-frame check must fail
        # such a frame. Drive the real helper against generated PNGs.
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        func = "screenshot_not_blank() {" + script.split(
            "screenshot_not_blank() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"

        def drive(path):
            harness = "fail() { echo FAIL: $* >&2; exit 1; }\n" + func + "screenshot_not_blank \"$1\"\n"
            return subprocess.run(["bash", "-eu", "-c", harness, "x", str(path)],
                                  capture_output=True, text=True)

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            blank = td / "blank.png"
            real = td / "real.png"
            garbage = td / "garbage.png"
            self._make_png(blank, True)
            self._make_png(real, False)
            garbage.write_bytes(b"not an image at all")
            self.assertEqual(drive(real).returncode, 0, drive(real).stderr)
            self.assertNotEqual(drive(blank).returncode, 0)
            self.assertIn("blank frame", drive(blank).stderr)
            self.assertNotEqual(drive(garbage).returncode, 0)

    def _encode_truecolor_png(self, pixels, filter_type):
        # pixels: list of rows, each a list of (r, g, b) tuples. Encodes every
        # scanline with the given PNG filter type (0-4) using the spec predictor,
        # so decoding must reproduce the exact input pixels.
        import struct, zlib
        w, h = len(pixels[0]), len(pixels)
        stride = w * 3
        raw = bytearray()
        for y in range(h):
            prev = pixels[y - 1] if y > 0 else None
            line = pixels[y]
            raw.append(filter_type)
            for x in range(stride):
                px = line[x // 3][x % 3]
                a = line[x // 3 - 1][x % 3] if x >= 3 else 0
                b = prev[x // 3][x % 3] if prev else 0
                c = prev[x // 3 - 1][x % 3] if (prev and x >= 3) else 0
                if filter_type == 0:
                    f = px
                elif filter_type == 1:
                    f = (px - a) & 255
                elif filter_type == 2:
                    f = (px - b) & 255
                elif filter_type == 3:
                    f = (px - ((a + b) >> 1)) & 255
                else:
                    # Paeth predictor (RFC 2083 §6.6)
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                    f = (px - pred) & 255
                raw.append(f)
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)

        def chunk(ct, data):
            body = ct + data
            return struct.pack(">I", len(data)) + body + struct.pack(
                ">I", zlib.crc32(body) & 0xffffffff)

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))

    def _decode_via_helper(self, png_bytes):
        # Run the PNG decoder embedded in the real screenshot_not_blank helper
        # (the exact code under test) and return its decoded pixels.
        import json, sys
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        start = script.index("python3 -c '") + len("python3 -c '")
        end = script.index("'", start)
        body = script[start:end]
        # Keep only the decoder definitions (drop the gate logic that exits),
        # then print the decoded pixels for the given file.
        defs = body.split("\nif data[:2] == b\"P6\":", 1)[0]
        program = defs + "\nimport json\nprint(json.dumps(png(data)[2]))\n"
        with tempfile.TemporaryDirectory() as td:
            png = Path(td) / "decode.png"
            png.write_bytes(png_bytes)
            result = subprocess.run([sys.executable, "-c", program, str(png)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return [tuple(px) for px in json.loads(result.stdout)]

    def test_png_filters_0_through_4_round_trip_exact_pixels(self):
        # #796 / hanthor's review: screenshot_not_blank decoded PNG filter type 4
        # (Paeth) with a wrong predictor, so busy frames could be miscounted.
        # Round-trip a small busy image through every filter type 0-4 and assert
        # the helper's decoder reproduces the exact input pixels. A wrong Paeth
        # (e.g. p = b - a, pr = a + c - p) fails this for filter 4.
        pixels = [[((y * 41 + x * 7) & 255, (y * 83 + x * 131) & 255,
                    (y * 17 + x * 227) & 255) for x in range(4)]
                  for y in range(3)]
        expected = [px for row in pixels for px in row]
        for ft in range(5):
            with self.subTest(filter_type=ft):
                encoded = self._encode_truecolor_png(pixels, ft)
                decoded = self._decode_via_helper(encoded)
                self.assertEqual(decoded, expected)

    def test_installed_system_asserts_the_booted_image_is_the_offline_payload(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertIn("bootc status --json", script)
        self.assertIn('["status"]["booted"]["image"]["image"]["image"]', script)
        self.assertIn('"${booted_image}" == "${PAYLOAD_IMAGE}"', script)
        self.assertIn("not the offline payload", script)
        # Must run before the login phase, on a guest that still has no route
        # to a network that could otherwise mask a substituted image.
        payload_at = script.index("bootc status --json")
        graphical_at = script.index('echo "=== Phase 6/7')
        login_at = script.index('echo "=== Phase 7/7')
        self.assertLess(graphical_at, payload_at)
        self.assertLess(payload_at, login_at)

    def test_default_flatpak_check_executes_against_stubbed_ssh(self):
        # Extract the real block and drive it with stubbed ssh_target/sleep/
        # fail, the same way test_iso_budget_guard_fails_closed_above_ceiling
        # drives the ISO size gate -- this is genuinely new set-difference
        # logic, not a string in a shell script.
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        start = script.index('if [[ -n "${UTAH_E2E_FLATPAKS-x}" ]]; then')
        end = script.index("\nfi\n", script.index("present offline", start)) + len("\nfi\n")
        block = script[start:end]

        def run(installed, flatpaks_env):
            harness = (
                "sleep() { :; }\n"
                "ssh_target() { [[ \"$1\" == *'flatpak list'* ]] && printf '%s\\n' \"$INSTALLED\"; }\n"
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                "dump_flatpak_preinstall_diagnostics() { echo \"DIAG: $1\" >&2; }\n"
                + block
            )
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "INSTALLED": installed, "UTAH_E2E_FLATPAKS": flatpaks_env}
            return subprocess.run(["bash", "-eu", "-c", harness],
                                   capture_output=True, text=True, env=env)

        complete = run("org.a\norg.b\norg.c", "org.a\norg.b")
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertIn("org.a, org.b", complete.stdout)

        missing = run("org.a", "org.a\norg.b")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("org.b", missing.stderr)
        self.assertNotIn("org.a\n", missing.stderr.split("missing")[-1])
        self.assertIn("DIAG: first boot", missing.stderr)

        skipped = run("org.a", "")
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertNotIn("present offline", skipped.stdout)

    def test_extension_error_prints_the_shell_reported_error(self):
        # #969: GSConnect intermittently reached state=ERROR at first login
        # and the journal grep printed nothing, so the reason was lost. Drive
        # the real extension check with a stubbed guest and assert the error
        # text the shell recorded is printed, and that the run still fails.
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        helper = "gnome_extension_errors() {" + script.split(
            "gnome_extension_errors() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        start = script.index('EXT_CHECK="${UTAH_E2E_EXTENSIONS')
        end = script.index("\nfi\n", script.index("did not reach ACTIVE", start)) + len("\nfi\n")
        block = script[start:end]

        def run(states, errors_rc=0):
            harness = (
                f"source {ROOT / 'iso/scripts/extension-state.sh'}\n"
                "sleep() { :; }\n"
                "shot() { :; }\n"
                "gnome_extension_info() {\n"
                "  n=$(cat \"$PROBES\"); n=$((n+1)); echo \"$n\" > \"$PROBES\";\n"
                "  total=$(wc -l < \"$STATES\"); if [ $n -gt $total ]; then n=$total; fi;\n"
                "  state=$(sed -n \"${n}p\" \"$STATES\");\n"
                "  if [ -n \"$state\" ]; then printf 'State: %s\\n' \"$state\"; fi;\n"
                "}\n"
                "ssh_target() {\n"
                "  case \"$1\" in\n"
                "    *GetExtensionErrors*) echo \"$1\" >> \"$CALLS\";\n"
                "      printf \"(['TypeError: boom'],)\\n\"; return \"$ERRORS_RC\";;\n"
                "  esac\n"
                "}\n"
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                + helper + block
            )
            with tempfile.TemporaryDirectory() as td:
                calls = Path(td) / "calls"
                calls.touch()
                probes = Path(td) / "probes"
                probes.write_text("0")
                state_file = Path(td) / "states"
                state_file.write_text("\n".join(states) + "\n")
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "STATES": str(state_file), "PROBES": str(probes),
                       "ERRORS_RC": str(errors_rc),
                       "CALLS": str(calls), "MONITOR_INSTALLED": "mon",
                       "UTAH_E2E_EXTENSIONS": "gsconnect@andyholmes.github.io"}
                result = subprocess.run(["bash", "-eu", "-o", "pipefail", "-c", harness],
                                        capture_output=True, text=True, env=env)
                return result, calls.read_text(), int(probes.read_text())

        failed, calls, probes = run(["ERROR"])
        self.assertEqual(probes, 24)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("state=ERROR", failed.stderr)
        self.assertIn("    (['TypeError: boom'],)", failed.stderr)
        self.assertIn("did not reach ACTIVE", failed.stderr)
        # Same session-bus environment as gnome_extension_info, or gdbus
        # cannot reach the shell from an SSH login.
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/", calls)
        self.assertIn("XDG_RUNTIME_DIR=/run/user/", calls)
        self.assertIn("gsconnect@andyholmes.github.io", calls)

        # A failing error query is a diagnostic, not a new verdict: the run
        # still reports the extension's state and fails for it, not for gdbus.
        probe_failed, _, _ = run(["ERROR"], errors_rc=1)
        self.assertIn("FAIL: 1 extension(s) did not reach ACTIVE", probe_failed.stderr)

        active, calls, probes = run(["ACTIVE"])
        self.assertEqual(probes, 1)
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertEqual(calls, "")

        # GNOME answers while initialization/activation is in progress.
        # Probe counts live in a file because each read runs in a subshell.
        for states in (["ERROR", "UNKNOWN", "ACTIVATING", "ACTIVE"],
                       ["", "INITIALIZED", "ENABLED"]):
            settled, calls, probes = run(states)
            self.assertEqual(settled.returncode, 0, settled.stderr)
            self.assertEqual(probes, len(states))
            self.assertEqual(calls, "")
        for state in ("UNKNOWN", "INACTIVE", "OUT_OF_DATE", "ACTIVATING"):
            stuck, calls, probes = run([state])
            self.assertNotEqual(stuck.returncode, 0, state)
            self.assertEqual(probes, 24)
            self.assertIn(f"state={state}", stuck.stderr)
            self.assertIn("GetExtensionErrors", calls)
        unreadable, _, probes = run([""])
        self.assertNotEqual(unreadable.returncode, 0)
        self.assertIn("state could not be read", unreadable.stderr)
        self.assertEqual(probes, 25)  # final raw diagnostic probe

    def test_live_desktop_is_retaken_until_the_frame_is_drawn(self):
        # The live-desktop shot is grabbed as soon as gnome-shell's process
        # exists, which can be before anything is drawn. A flat frame there must
        # be retaken, not left for the end-of-run gate to fail a healthy boot;
        # a frame that never draws must still be left blank for that gate.
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        blank_fn = "screenshot_not_blank() {" + script.split(
            "screenshot_not_blank() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        retake_fn = "shot_until_drawn() {" + script.split(
            "shot_until_drawn() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"

        def drive(blank_shots, ext="png", blank_kind="solid", final_mode=""):
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                if ext == "ppm":
                    # shot() keeps the raw PPM when ffmpeg is unavailable.
                    w, h = 16, 16
                    (td / "blank.ppm").write_bytes(
                        b"P6\n%d %d\n255\n" % (w, h) + bytes(w * h * 3))
                    (td / "real.ppm").write_bytes(
                        b"P6\n%d %d\n255\n" % (w, h)
                        + bytes((i * 7) % 256 for i in range(w * h * 3)))
                else:
                    self._make_png(td / "blank.png", blank_kind == "solid",
                                   top_bar_only=blank_kind == "bar")
                    self._make_png(td / "real.png", False)
                harness = (
                    f"SHOTS={td}\ncalls=0\nsleep() {{ :; }}\n"
                    "fail() { echo FAIL: $* >&2; exit 1; }\n"
                    "shot() { calls=$((calls+1)); "
                    f"if [ $calls -le {blank_shots} ]; then cp {td}/blank.{ext} \"$SHOTS/$1.{ext}\"; "
                    f"else cp {td}/real.{ext} \"$SHOTS/$1.{ext}\"; fi; }}\n"
                    + blank_fn + retake_fn
                    + "shot_until_drawn live-desktop sock\n"
                    "echo calls=$calls\n"
                    f"if ( screenshot_not_blank \"$SHOTS/live-desktop.{ext}\" {final_mode} ) >/dev/null 2>&1; "
                    "then echo final=drawn; else echo final=blank; fi\n")
                return subprocess.run(["bash", "-eu", "-c", harness],
                                      capture_output=True, text=True)

        early = drive(3)
        self.assertEqual(early.returncode, 0, early.stderr)
        self.assertIn("calls=4", early.stdout)
        self.assertIn("final=drawn", early.stdout)
        never = drive(99)
        self.assertEqual(never.returncode, 0, never.stderr)
        self.assertIn("calls=12", never.stdout)
        self.assertIn("final=blank", never.stdout)
        bar = drive(2, "png", "bar")
        self.assertEqual(bar.returncode, 0, bar.stderr)
        self.assertIn("calls=3", bar.stdout)
        self.assertIn("final=drawn", bar.stdout)
        # A frame stuck at top-bar-only must fail the end-of-run gate, which
        # checks live-desktop in body mode like the retake loop does.
        bar_never = drive(99, "png", "bar", "body")
        self.assertEqual(bar_never.returncode, 0, bar_never.stderr)
        self.assertIn("calls=12", bar_never.stdout)
        self.assertIn("final=blank", bar_never.stdout)
        ppm = drive(2, "ppm")
        self.assertEqual(ppm.returncode, 0, ppm.stderr)
        self.assertIn("calls=3", ppm.stdout)
        self.assertIn("final=drawn", ppm.stdout)

    def test_setup_unit_checks_reject_non_success_results(self):
        # systemd's Result= is success/exit-code/signal/timeout/...; it is never
        # "failed" (that is an ActiveState). A setup unit that exited non-zero
        # must fail the gate on both the system and user helper.
        script = (ROOT / "iso/scripts/e2e-services.sh").read_text()
        start = script.index("check_system_unit_state() {")
        end = script.index("\nverify_setup_state() {", start)
        block = script[start:end]

        def run(call, active, result):
            harness = (
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                "ssh_target() {\n"
                "  case \"$1\" in\n"
                "    *is-enabled*) echo enabled ;;\n"
                "    *) printf 'LoadState=loaded\\nActiveState=%s\\nResult=%s\\n"
                "ExecMainStartTimestamp=Tue 2026-10-13 10:00:00 UTC\\n' \"$ACTIVE\" \"$RESULT\" ;;\n"
                "  esac\n"
                "}\n"
                + block + "\n" + call + "\n"
            )
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "TEST_PASSWORD": "pw", "ACTIVE": active, "RESULT": result}
            return subprocess.run(["bash", "-eu", "-c", harness],
                                   capture_output=True, text=True, env=env)

        for call in ("check_system_unit_state ublue-system-setup.service true true first",
                     "check_user_unit_state ublue-user-setup.service true second"):
            ok = run(call, "inactive", "success")
            self.assertEqual(ok.returncode, 0, ok.stderr)
            for active, result in (("inactive", "exit-code"), ("failed", "exit-code"),
                                   ("failed", "success"), ("inactive", "timeout")):
                bad = run(call, active, result)
                self.assertNotEqual(bad.returncode, 0, (call, active, result))
                self.assertIn(f"Result={result}", bad.stderr)

    def test_setup_state_checks_every_unit_in_the_installed_contract(self):
        # #19: the runtime check follows the installed image's own contract,
        # not a list copied into the script, and a failed contract unit fails
        # the gate. brew-preinstall.service needs the network, so in the
        # offline VM only its enablement counts.
        script = (ROOT / "iso/scripts/e2e-services.sh").read_text()
        block = script[script.index("check_system_unit_state() {"):]

        def run(failed_unit):
            harness = (
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                "scp_target() { :; }\n"
                # Real ssh reads stdin; so does the stub, so a loop feeding
                # units on stdin would stop after the first one.
                "ssh_target() {\n"
                "  cat >/dev/null\n"
                "  case \"$1\" in\n"
                "    *\"--services 'enabled'\"*) printf 'gdm.service\\nuupd.timer\\n' ;;\n"
                "    *\"--services 'user_enabled'\"*) "
                "printf 'brew-preinstall.service\\npipewire.socket\\n' ;;\n"
                "    *is-enabled*) echo enabled ;;\n"
                "    *\"'$FAILED_UNIT'\"*) printf 'LoadState=loaded\\nActiveState=failed\\n"
                "Result=exit-code\\nExecMainStartTimestamp=n/a\\n' ;;\n"
                "    *) printf 'LoadState=loaded\\nActiveState=active\\nResult=success\\n"
                "ExecMainStartTimestamp=Tue 2026-10-13 10:00:00 UTC\\n' ;;\n"
                "  esac\n"
                "}\n"
                + block + "\nverify_setup_state first\n"
            )
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ROOT": str(ROOT),
                   "TEST_USER": "u", "TEST_PASSWORD": "pw", "FAILED_UNIT": failed_unit}
            return subprocess.run(["bash", "-eu", "-c", harness],
                                  capture_output=True, text=True, env=env,
                                  # The stub drains stdin; never wait on a terminal.
                                  stdin=subprocess.DEVNULL)

        ok = run("none")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        # Every unit is reported, not only the first of each list.
        for unit in ("gdm.service", "uupd.timer", "pipewire.socket",
                     "brew-preinstall.service"):
            self.assertIn(f"  {unit}: ", ok.stdout)
        offline = run("brew-preinstall.service")
        self.assertEqual(offline.returncode, 0, offline.stderr)
        for unit in ("gdm.service", "pipewire.socket"):
            bad = run(unit)
            self.assertNotEqual(bad.returncode, 0, unit)
            self.assertIn(f"{unit} failed during first boot", bad.stderr)

    def test_setup_state_checks_template_units_by_enablement_and_instances(self):
        # kmsconvt@.service (#1237) is a template: `systemctl show` reports
        # nothing for it, which read as "unavailable" and failed the lifecycle
        # candidate (run 44895186825). It must be enabled, and no instance may
        # have failed.
        script = (ROOT / "iso/scripts/e2e-services.sh").read_text()
        block = script[script.index("check_system_unit_state() {"):]

        def run(enabled, failed_instance, scope):
            system, user = (("kmsconvt@.service", "pipewire.socket") if scope == "system"
                            else ("gdm.service", "kmsconvt@.service"))
            harness = (
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                "scp_target() { :; }\n"
                "ssh_target() {\n"
                "  cat >/dev/null\n"
                "  case \"$1\" in\n"
                f"    *\"--services 'enabled'\"*) echo {system} ;;\n"
                f"    *\"--services 'user_enabled'\"*) echo {user} ;;\n"
                "    *\"is-enabled 'kmsconvt@.service'\"*) echo \"$ENABLED\" ;;\n"
                "    *is-enabled*) echo enabled ;;\n"
                "    *\"list-units --state=failed\"*\"'kmsconvt@*.service'\"*) "
                "[ -z \"$FAILED_INSTANCE\" ] || echo \"$FAILED_INSTANCE loaded failed failed\" ;;\n"
                # Like systemd: a template has no runtime state to show.
                "    *kmsconvt@.service*) ;;\n"
                "    *) printf 'LoadState=loaded\\nActiveState=active\\nResult=success\\n"
                "ExecMainStartTimestamp=Sat 2026-11-21 10:00:00 UTC\\n' ;;\n"
                "  esac\n"
                "}\n"
                + block + "\nverify_setup_state first\n"
            )
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ROOT": str(ROOT),
                   "TEST_USER": "u", "TEST_PASSWORD": "pw", "ENABLED": enabled,
                   "FAILED_INSTANCE": failed_instance}
            return subprocess.run(["bash", "-eu", "-c", harness],
                                  capture_output=True, text=True, env=env,
                                  stdin=subprocess.DEVNULL)

        for scope in ("system", "user"):
            with self.subTest(scope=scope):
                ok = run("enabled", "", scope)
                self.assertEqual(ok.returncode, 0, ok.stderr)
                self.assertIn("  kmsconvt@.service: enabled; no failed instance", ok.stdout)
                disabled = run("disabled", "", scope)
                self.assertNotEqual(disabled.returncode, 0)
                self.assertIn(f"required {scope} unit kmsconvt@.service is disabled",
                              disabled.stderr)
                failed = run("enabled", "kmsconvt@tty2.service", scope)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("kmsconvt@.service has a failed instance during first boot: "
                              "kmsconvt@tty2.service", failed.stderr)

    def test_setup_state_judges_resultless_units_by_active_state(self):
        # A target has no Result= property, so `systemctl show` omits it and
        # nfs-client.target failed the lifecycle candidate as
        # "ActiveState=active Result=unknown" (run 44926686288). A target is
        # judged by ActiveState alone; a service still needs Result=success.
        script = (ROOT / "iso/scripts/e2e-services.sh").read_text()
        start = script.index("check_system_unit_state() {")
        end = script.index("\nverify_setup_state() {", start)
        block = script[start:end]

        def run(call, active, result):
            harness = (
                "fail() { echo \"FAIL: $*\" >&2; exit 1; }\n"
                "ssh_target() {\n"
                "  case \"$1\" in\n"
                "    *is-enabled*) echo enabled ;;\n"
                "    *) printf 'LoadState=loaded\\nActiveState=%s\\n' \"$ACTIVE\"\n"
                "       [ -z \"$RESULT\" ] || printf 'Result=%s\\n' \"$RESULT\"\n"
                "       printf 'ExecMainStartTimestamp=\\n' ;;\n"
                "  esac\n"
                "}\n"
                + block + "\n" + call + "\n"
            )
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "TEST_PASSWORD": "pw", "ACTIVE": active, "RESULT": result}
            return subprocess.run(["bash", "-eu", "-c", harness],
                                  capture_output=True, text=True, env=env)

        for call in ("check_system_unit_state nfs-client.target true false upgraded",
                     "check_user_unit_state default.target false upgraded"):
            with self.subTest(call=call):
                ok = run(call, "active", "")
                self.assertEqual(ok.returncode, 0, ok.stderr)
                self.assertIn(".target: active", ok.stdout)
                bad = run(call, "failed", "")
                self.assertNotEqual(bad.returncode, 0)
                self.assertIn("failed during upgraded boot (ActiveState=failed", bad.stderr)
        missing = run("check_system_unit_state gdm.service true false upgraded", "active", "")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("gdm.service failed during upgraded boot", missing.stderr)

    def test_failure_diagnostics_run_from_the_exit_trap_not_only_fail(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertIn("preserve_failed_disk_diagnostics", script)
        self.assertIn("qemu-img info", script)
        self.assertIn("never the disk itself", script)
        # A bare command failing under `set -e` (an scp step, for one)
        # aborts straight to the EXIT trap without ever calling fail(), so
        # the capture must live in cleanup(), not only in fail()'s body.
        cleanup_at = script.index("cleanup() {")
        trap_at = script.index("trap cleanup EXIT")
        self.assertIn("preserve_failed_disk_diagnostics", script[cleanup_at:trap_at])
        fail_at = script.index('fail() { echo "FAIL: $*"')
        self.assertNotIn("preserve_failed_disk_diagnostics", script[fail_at:fail_at + 80])


class ReviewIsoBudgetMeasurementTests(unittest.TestCase):
    """The review ISO can measure against the production size ceiling."""

    def setUp(self):
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        self.inputs = build[True]["workflow_dispatch"]["inputs"]
        self.step = next(
            step for step in build["jobs"]["review_vm"]["steps"]
            if step.get("name", "").startswith("Compose offline production ISO")
        )

    def test_default_keeps_the_test_only_compression_level(self):
        self.assertEqual(self.inputs["review_iso_compression_level"]["default"], "15")
        self.assertEqual(
            self.step["env"]["UTAH_ISO_COMPRESSION_LEVEL"],
            "${{ inputs.review_iso_compression_level || '15' }}",
        )

    def test_size_is_saved_even_when_budget_guard_fails(self):
        run = self.step["run"]
        record = run.index("evidence/iso-size.json")
        guard = run.index('[ "$rc" -eq 0 ] || exit "$rc"')
        self.assertLess(record, guard)
        self.assertIn("compression_level", run)
        self.assertIn("iso_bytes", run)

    def test_initramfs_builds_use_reproducible_zstd(self):
        # Plain nvidia exceeded the 6 GiB size ceiling (#740) because the base
        # kernel's uncompressed/gzip initramfs was duplicated across pxeboot,
        # efi.img ESP, live squashfs, and offline container store. Both the
        # installed image regeneration and the live ISO rebuild must pass --zstd
        # and --reproducible to stay compact.
        regen = (ROOT / "scripts/regenerate-initramfs.sh").read_text()
        self.assertIn("dracut --no-hostonly --reproducible --zstd", regen)
        live = (ROOT / "iso/live/Containerfile").read_text()
        self.assertIn("dracut --force --reproducible --zstd --no-hostonly", live)


class ReviewComposefsBackendTests(unittest.TestCase):
    """The review VM can install with the composefs backend (#1129)."""

    def test_input_selects_the_e2e_backend(self):
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        inputs = build[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["review_composefs_backend"]["type"], "boolean")
        self.assertIs(inputs["review_composefs_backend"]["default"], False)
        step = next(
            step for step in build["jobs"]["review_vm"]["steps"]
            if "luks-e2e.sh" in step.get("run", "")
        )
        self.assertEqual(
            step["env"]["UTAH_E2E_COMPOSEFS_BACKEND"],
            "${{ inputs.review_composefs_backend && '1' || '0' }}",
        )
