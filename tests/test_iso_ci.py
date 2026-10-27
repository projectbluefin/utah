"""CI must test the complete exact-digest set before publication."""
import hashlib
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
        script = (ROOT / "iso/scripts/build-iso.sh").read_text()
        self.assertNotIn("oci-archive:", script)
        self.assertEqual(script.count("--preserve-digests"), 2)
        self.assertIn('"dir:${PAYLOAD_EXPORT}"', script)
        self.assertIn('dir:/payload "containers-storage:$1"', script)

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

    def test_build_explicitly_dispatches_iso_after_both_image_jobs(self):
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        job = build["jobs"]["dispatch-iso"]
        self.assertEqual(set(job["needs"]), {"build_main", "build_kernel"})
        self.assertIn("refs/heads/testing", job["if"])
        self.assertIn("needs.build_main.result == 'success'", job["if"])
        self.assertIn("needs.build_kernel.result == 'success'", job["if"])
        self.assertIn("build_run_id=$GITHUB_RUN_ID", job["steps"][0]["run"])
        workflow = (ROOT / ".github/workflows/post-testing-e2e.yml").read_text()
        self.assertNotIn("workflow_run:", workflow)
        self.assertIn('timeout 300 gh run watch "$BUILD_RUN"', workflow)

    def test_readme_update_is_idempotent_and_preserves_other_text(self):
        update = load("update-e2e-readme").update
        proof = {"source_sha": "a" * 40, "e2e_run": "123"}
        text = "# Utah\n\nKeep this paragraph.\n"
        result = update(text, proof)
        self.assertEqual(update(result, proof), result)
        self.assertIn("Keep this paragraph.", result)
        self.assertIn("actions/runs/123", result)
        self.assertIn("may show a failure in independent tests", result)
        self.assertIn("the LUKS and production ISO jobs passed", result)

    def test_production_iso_artifacts_follow_the_full_luks_matrix(self):
        import yaml
        jobs = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())["jobs"]
        production = jobs["production-iso"]
        self.assertIn("luks", production["needs"])
        self.assertFalse(production["strategy"]["fail-fast"])
        compose = next(step for step in production["steps"]
                       if step.get("name") == "Compose production ISO and checksum")
        self.assertIn('"Utah Live" 0 "$IMAGE_REF"', compose["run"])
        self.assertIn("cd output && sha256sum utah-live.iso", compose["run"])
        artifact = next(step for step in production["steps"]
                        if step.get("name") == "Retain production ISO")
        self.assertIn("output/utah-live.iso", artifact["with"]["path"])
        self.assertIn("output/utah-live.iso.sha256", artifact["with"]["path"])
        for name in ["promote-to-testing", "documentation"]:
            self.assertIn("production-iso", jobs[name]["needs"])
            self.assertNotIn("if", jobs[name])

    def test_debug_images_and_test_disks_are_not_uploaded(self):
        import yaml
        jobs = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())["jobs"]
        steps = jobs["luks"]["steps"]
        self.assertFalse(jobs["luks"]["strategy"]["fail-fast"])
        test = next(step for step in steps if "Run existing LUKS" in step.get("name", ""))
        self.assertEqual(test["env"]["UTAH_E2E_REQUIRE_FASTFETCH"], "1")
        uploads = [s for s in steps if "upload-artifact@" in s.get("uses", "")]
        for step in uploads:
            self.assertNotIn("output/", step["with"]["path"])
            self.assertNotIn("qcow2", step["with"]["path"])
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

        def run(state, errors_rc=0):
            harness = (
                f"source {ROOT / 'iso/scripts/extension-state.sh'}\n"
                "sleep() { :; }\n"
                "shot() { :; }\n"
                "gnome_extension_info() { printf 'State: %s\\n' \"$STATE\"; }\n"
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
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "STATE": state, "ERRORS_RC": str(errors_rc),
                       "CALLS": str(calls), "MONITOR_INSTALLED": "mon",
                       "UTAH_E2E_EXTENSIONS": "gsconnect@andyholmes.github.io"}
                result = subprocess.run(["bash", "-eu", "-o", "pipefail", "-c", harness],
                                        capture_output=True, text=True, env=env)
                return result, calls.read_text()

        failed, calls = run("ERROR")
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
        probe_failed, _ = run("ERROR", errors_rc=1)
        self.assertIn("FAIL: 1 extension(s) did not reach ACTIVE", probe_failed.stderr)

        active, calls = run("ACTIVE")
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertEqual(calls, "")

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
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
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
                     "check_user_setup_unit second"):
            ok = run(call, "inactive", "success")
            self.assertEqual(ok.returncode, 0, ok.stderr)
            for active, result in (("inactive", "exit-code"), ("failed", "exit-code"),
                                   ("failed", "success"), ("inactive", "timeout")):
                bad = run(call, active, result)
                self.assertNotEqual(bad.returncode, 0, (call, active, result))
                self.assertIn(f"Result={result}", bad.stderr)

    def test_failure_diagnostics_run_from_the_exit_trap_not_only_fail(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertIn("preserve_failed_disk_diagnostics", script)
        self.assertIn("qemu-img info", script)
        self.assertIn("never the disk itself", script)
        # A bare command failing under `set -e` (the installer step, for one)
        # aborts straight to the EXIT trap without ever calling fail(), so
        # the capture must live in cleanup(), not only in fail()'s body.
        cleanup_at = script.index("cleanup() {")
        trap_at = script.index("trap cleanup EXIT")
        self.assertIn("preserve_failed_disk_diagnostics", script[cleanup_at:trap_at])
        fail_at = script.index('fail() { echo "FAIL: $*"')
        self.assertNotIn("preserve_failed_disk_diagnostics", script[fail_at:fail_at + 80])


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


class FastfetchOcrGateTests(unittest.TestCase):
    """The gate runs against tesseract output, which drops and mangles glyphs."""

    GATE = ROOT / "iso/scripts/fastfetch-ocr-match.sh"

    # Verbatim from _temp/utah-luks-e2e/fastfetch-ocr.txt in the
    # iso-diagnostics-utah artifact of run 35374557822, whose screenshot showed
    # fastfetch but which the previous gate rejected.
    REAL_TRANSCRIPT = """TAH-E2E-FASTFETCH
[utahtest@utah-luks-test ~]$

utah: testing-20260918-2216657 &
Utah (Version: testing-20260918-2216657)
Linux 7.1.8-100.fc43.x86_64

2 mins

KVM/QEMU Standard PC (Q35 + ICH9, 2009) (pc-q35-10.2)
GNOME 51.beta
Mutter (Wayland)
"""

    # Verbatim excerpt from fastfetch-ocr.txt in the iso-diagnostics-utah-nvidia
    # artifact of run 36769782907. Tesseract substituted inside the sentinel
    # fragment itself ("E2E" read as "£26") while reading the "-FASTFETCH"
    # suffix and the whole fastfetch body cleanly -- the same misread failed
    # four consecutive flavors and runs (#375), and identical sentinel pixels
    # read clean the rest of the time.
    SUBSTITUTED_TRANSCRIPT = """\\‘uran-£26-FASTFETCH
[utahtest@utah-luks-test ~]$

Utah (Version: testing-20260930-9f3baa2)
Linux 7.2.7-200.fc44.x86_64

GNOME 51.0
Mutter (Wayland)
"""

    def matches(self, transcript):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fastfetch-ocr.txt"
            path.write_text(transcript)
            return subprocess.run(["bash", str(self.GATE), str(path)]).returncode == 0

    def test_accepts_the_transcript_that_previously_failed_a_good_screenshot(self):
        self.assertTrue(self.matches(self.REAL_TRANSCRIPT))

    def test_accepts_a_clean_transcript_with_readable_field_labels(self):
        self.assertTrue(self.matches("UTAH-E2E-FASTFETCH\nKernel: 7.1.8-100.fc43.x86_64\n"))

    def test_rejects_a_desktop_with_no_terminal_on_it(self):
        self.assertFalse(self.matches("Activities\nSep 18  19:04\nutahtest\n"))

    def test_rejects_the_sentinel_without_any_fastfetch_body(self):
        self.assertFalse(self.matches("UTAH-E2E-FASTFETCH\n[utahtest@utah-luks-test ~]$\n"))

    def test_rejects_fastfetch_body_without_the_sentinel(self):
        self.assertFalse(self.matches("Kernel: 7.1.8-100.fc43.x86_64\nGNOME 51.beta\n"))

    def test_accepts_transcript_with_substituted_sentinel_fragment(self):
        self.assertTrue(self.matches(self.SUBSTITUTED_TRANSCRIPT))

    def test_rejects_suffix_with_kernel_but_no_second_body_token(self):
        self.assertFalse(self.matches(
            "uran-£26-FASTFETCH\nLinux 7.2.7-200.fc44.x86_64\n2 mins\n"))

    def test_rejects_suffix_with_body_but_no_kernel_token(self):
        self.assertFalse(self.matches(
            "uran-£26-FASTFETCH\nGNOME 51.0\nMutter (Wayland)\n"))

    def test_harness_rereads_the_same_screenshot_before_reshooting(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertIn("for _ocr in 1 2 3", script)
        self.assertIn("break 2", script)

    def test_rejects_an_empty_or_missing_transcript(self):
        self.assertFalse(self.matches(""))
        self.assertFalse(
            subprocess.run(["bash", str(self.GATE), "/nonexistent/ocr.txt"]).returncode == 0)

    def test_harness_delegates_the_decision_and_prints_the_transcript_on_failure(self):
        script = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
        self.assertIn("fastfetch-ocr-match.sh", script)
        self.assertNotIn("grep -qi 'UTAH.E2E.FASTFETCH'", script)
        self.assertIn("last OCR transcript", script)


class FlatpakRetryTests(unittest.TestCase):
    """A Flathub timeout must not fail a whole flavor's end-to-end run.

    Run 35432516418 lost utah-gaming to a single timed-out object while pulling
    Firefox, three minutes into composing the ISO, with nothing wrong in the
    image. The installs are the largest network operation in the build and had
    no retry, while the curl beside them has had one all along.
    """

    SCRIPT = ROOT / "iso/live/src/install-flatpaks.sh"

    def drive(self, stub: str) -> subprocess.CompletedProcess:
        """Run the real retry_flatpak against a stub flatpak, with sleep off."""
        harness = f"""
        set -uo pipefail
        eval "$(sed -n '/^retry_flatpak() {{/,/^}}/p' {self.SCRIPT})"
        sleep() {{ :; }}
        attempts=0
        {stub}
        retry_flatpak install org.example.App >/dev/null 2>&1
        echo "rc=$? attempts=$attempts"
        """
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                              cwd=ROOT)

    def test_a_transient_failure_is_retried_and_succeeds(self):
        result = self.drive('flatpak() { attempts=$((attempts+1)); [ "$attempts" -ge 3 ]; }')
        self.assertEqual(result.stdout.strip(), "rc=0 attempts=3", result.stderr)

    def test_a_longer_outage_is_retried_and_succeeds_on_the_fifth_attempt(self):
        # Run 36230660725 lost utah to dl.flathub.org timeouts on attempts
        # 1-3 spread over ~20 minutes: the retry budget is five attempts.
        result = self.drive('flatpak() { attempts=$((attempts+1)); [ "$attempts" -ge 5 ]; }')
        self.assertEqual(result.stdout.strip(), "rc=0 attempts=5", result.stderr)

    def test_a_persistent_failure_still_fails_after_five_attempts(self):
        # The point is resilience, not swallowing errors: a repository that is
        # genuinely gone must still fail the build.
        result = self.drive("flatpak() { attempts=$((attempts+1)); return 1; }")
        self.assertEqual(result.stdout.strip(), "rc=1 attempts=5", result.stderr)

    def test_every_network_install_goes_through_the_retry(self):
        script = self.SCRIPT.read_text()
        installs = [line for line in script.splitlines()
                    if line.startswith("flatpak install")
                    or line.startswith("retry_flatpak install")]
        self.assertTrue(installs)
        for line in installs:
            with self.subTest(line=line):
                self.assertTrue(line.startswith("retry_flatpak install"),
                                f"unretried network install: {line}")

    def test_every_retried_install_is_idempotent(self):
        # The retry is only safe if re-running it is a no-op for a ref that
        # already completed. Without --or-update, an attempt that installed the
        # app but still exited nonzero makes the next attempt fail with
        # "already installed" -- the retry would turn a flaky success into a
        # hard failure, which is the opposite of why it was added.
        script = self.SCRIPT.read_text()
        calls = re.findall(r"^retry_flatpak install.*?(?=\n\S|\Z)", script,
                           re.MULTILINE | re.DOTALL)
        self.assertTrue(calls)
        for call in calls:
            with self.subTest(call=call.splitlines()[0]):
                self.assertIn("--or-update", call)


class OgcKernelConfigGateTests(unittest.TestCase):
    """The OGC kernel must be rejected if it cannot mount Utah's root filesystem.

    Utah installs to btrfs on LUKS, and x86_64_defconfig has no BTRFS_FS -- I
    checked upstream's own defconfig, where DM_CRYPT is likewise absent and
    VFAT_FS is present. The gaming flavors therefore formatted a root volume and
    then failed to mount it, in run 35432516418:

        mkfs.btrfs -f -L root /dev/mapper/fisherman-root       (ok)
        mount -t btrfs /dev/mapper/fisherman-root /mnt/...
        mount: unknown filesystem type 'btrfs'

    A full kernel build is the only complete proof, and it takes 45 minutes. The
    gate itself is a shell function, so its contract can be tested in
    milliseconds: it must reject a config missing any required symbol, and name
    the one it rejected.
    """

    SCRIPT = ROOT / "scripts/install-ogc-kernel.sh"

    def gate(self, config_body: str) -> subprocess.CompletedProcess:
        """Run the real required_config/verify_config against a fake .config."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".config"
            config.write_text(config_body)
            harness = f"""
            set -uo pipefail
            eval "$(sed -n '/^required_config=(/,/)$/p' {self.SCRIPT})"
            eval "$(sed -n '/^verify_config() {{/,/^}}/p' {self.SCRIPT})"
            verify_config "{config}"
            """
            return subprocess.run(["bash", "-c", harness],
                                  capture_output=True, text=True)

    def required_symbols(self) -> list[str]:
        block = self.SCRIPT.read_text().split("required_config=(", 1)[1]
        block = block.split(")", 1)[0]
        return block.split()

    def test_btrfs_is_required(self):
        self.assertIn("BTRFS_FS", self.required_symbols())

    def test_flatpak_user_namespaces_and_tailscale_tun_are_required(self):
        symbols = self.required_symbols()
        for symbol in ("USER_NS", "TUN"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, symbols)
                body = "".join(f"CONFIG_{item}=y\n" for item in symbols
                               if item != symbol)
                result = self.gate(body)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"missing CONFIG_{symbol}", result.stderr)

    def test_uinput_is_required_for_testsuite_key_combos(self):
        # smoke-b "Settings closes cleanly via Ctrl+Q" errors on the gaming
        # flavors because the OGC guest has no /dev/uinput even after
        # `modprobe uinput` (#741). The gate must reject a config without it.
        symbols = self.required_symbols()
        self.assertIn("INPUT_UINPUT", symbols)
        body = "".join(f"CONFIG_{s}=y\n" for s in symbols
                       if s != "INPUT_UINPUT")
        result = self.gate(body)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing CONFIG_INPUT_UINPUT", result.stderr)
        self.assertRegex(
            self.SCRIPT.read_text(), r"--module INPUT_UINPUT(?:\s|$)")

    def test_zram_is_required_so_boot_does_not_wait_for_zram0(self):
        # zram-generator asks for /dev/zram0; without CONFIG_ZRAM systemd waits
        # 50s for it and geoclue times out on the gaming flavors (#741).
        symbols = self.required_symbols()
        self.assertIn("ZRAM", symbols)
        body = "".join(f"CONFIG_{s}=y\n" for s in symbols if s != "ZRAM")
        result = self.gate(body)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing CONFIG_ZRAM", result.stderr)
        self.assertRegex(self.SCRIPT.read_text(), r"--module ZRAM(?:\s|$)")

    def test_a_config_missing_btrfs_is_rejected_by_name(self):
        body = "".join(f"CONFIG_{s}=y\n" for s in self.required_symbols()
                       if s != "BTRFS_FS")
        result = self.gate(body)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing CONFIG_BTRFS_FS", result.stderr)

    def test_a_complete_config_passes(self):
        body = "".join(f"CONFIG_{s}=y\n" for s in self.required_symbols())
        self.assertEqual(self.gate(body).returncode, 0, self.gate(body).stderr)

    def test_a_module_satisfies_the_gate_as_well_as_builtin(self):
        # BTRFS_FS is enabled with --module, so =m has to count.
        body = "".join(f"CONFIG_{s}=m\n" for s in self.required_symbols())
        self.assertEqual(self.gate(body).returncode, 0, self.gate(body).stderr)

    def test_every_required_symbol_is_actually_checked(self):
        # A symbol in the list that verify_config never looks at would be
        # documentation pretending to be a gate.
        for symbol in self.required_symbols():
            with self.subTest(symbol=symbol):
                body = "".join(f"CONFIG_{s}=y\n" for s in self.required_symbols()
                               if s != symbol)
                result = self.gate(body)
                self.assertEqual(result.returncode, 1, f"{symbol} is not gated")
                self.assertIn(f"missing CONFIG_{symbol}", result.stderr)

class ForkPullRequestKernelCacheTests(unittest.TestCase):
    """A fork PR cannot publish the kernel cache, and must not go red for it.

    #154, #157 and #158 were all red on the same thing: the kernel compiled and
    the push then failed with "denied: installation not allowed to Write
    organization package", twenty minutes in, on a permission no contributor can
    be granted from a fork. Three PRs blocked by CI plumbing rather than by
    anything in their diffs.
    """

    WORKFLOW = ROOT / ".github/workflows/build.yml"

    def setUp(self):
        import yaml
        self.text = self.WORKFLOW.read_text()
        self.jobs = yaml.safe_load(self.text)["jobs"]

    def test_the_push_is_conditional_but_the_build_is_not(self):
        step = next(s for s in self.jobs["kernel_cache"]["steps"]
                    if s.get("id") == "cache")
        run = step["run"]
        # The compile is what validates a kernel change, so it always runs.
        self.assertIn("podman build --tag", run)
        build = run.index("podman build --tag")
        guard = run.index('if [ "${CAN_PUBLISH}" != "true" ]')
        push = run.index("podman push")
        self.assertLess(build, guard, "the build must not be behind the guard")
        self.assertLess(guard, push, "the push must be behind the guard")

    def test_can_publish_is_true_for_everything_that_is_not_a_fork_pr(self):
        step = next(s for s in self.jobs["kernel_cache"]["steps"]
                    if s.get("id") == "cache")
        env = step["env"]["CAN_PUBLISH"]
        # An empty head repository is a push, a schedule or a dispatch.
        self.assertIn("github.event.pull_request.head.repo.full_name == ''", env)
        self.assertIn("== github.repository", env)

    def test_the_flavored_builds_are_skipped_rather_than_left_to_fail(self):
        # Without this they would try to pull an image that was never pushed.
        self.assertIn("needs.kernel_cache.outputs.available == 'true'",
                      self.jobs["build_kernel"]["if"])
        self.assertEqual(self.jobs["kernel_cache"]["outputs"]["available"],
                         "${{ steps.cache.outputs.available }}")

    def test_a_cache_hit_still_reports_the_image_as_available(self):
        step = next(s for s in self.jobs["kernel_cache"]["steps"]
                    if s.get("id") == "cache")
        hit = step["run"].index("Cache hit")
        self.assertIn("available=true", step["run"][hit:hit + 200])

class ReviewIsoBudgetMeasurementTests(unittest.TestCase):
    """The review ISO can measure against the production size ceiling."""

    def setUp(self):
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        self.inputs = build[True]["workflow_dispatch"]["inputs"]
        self.step = next(
            step for step in build["jobs"]["review_vm"]["steps"]
            if step.get("name", "").startswith("Compose offline debug ISO")
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


class ConcurrencyTests(unittest.TestCase):
    """A testing build must not be cancelled by the next one.

    build.yml on `testing` is what produces the images and dispatches the ISO
    end-to-end validation the release gate reads. Cancelling it throws away the
    evidence: on 2026-09-19 two testing builds were cancelled within two minutes
    (11:09 and 11:11) and the promotion that followed failed with
    E2E_LAST_STATUS: skipped.
    """

    def test_only_pull_requests_are_cancelled(self):
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        self.assertEqual(build["concurrency"]["cancel-in-progress"],
                         "${{ github.event_name == 'pull_request' }}")

    def test_the_group_still_separates_refs(self):
        # Sharing one group across refs would make a testing build queue behind
        # an unrelated PR's.
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        self.assertIn("github.ref", build["concurrency"]["group"])


class BuildToolingRemovalTests(unittest.TestCase):
    """The [build] toolchain must not ship (D1, docs/bluefin-package-gaps.md).

    configure-services.sh removes the extension build tooling after the build.
    Passing --no-autoremove kept the dependency closure (ninja-build,
    meson-srpm-macros, libsass, *-devel chains) in the image; the default
    remove cleans up dependencies orphaned by the transaction.
    """

    def setUp(self):
        import tomllib
        manifest = tomllib.loads((ROOT / "packages" / "utah.toml").read_text())
        self.build = manifest["build"]["packages"]
        self.script = (ROOT / "scripts" / "configure-services.sh").read_text()

    def remove_line(self):
        lines = [line for line in self.script.splitlines()
                 if "remove" in line and "dbus-devel" in line]
        self.assertEqual(len(lines), 1,
                         "expected exactly one build-tooling removal command")
        return lines[0]

    def test_every_build_package_but_unzip_is_removed(self):
        # unzip is in [parity] as well as [build]: Bluefin ships it to users.
        line = self.remove_line()
        for pkg in self.build:
            if pkg == "unzip":
                continue
            self.assertIn(pkg, line, f"{pkg} from [build] is not removed")

    def test_removal_cleans_the_dependency_closure(self):
        self.assertNotIn("--no-autoremove", self.remove_line(),
                         "removal must let dnf clean the orphaned build closure")


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
