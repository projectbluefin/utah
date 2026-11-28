"""Image-installer track tests: Installed-system check and kernel verity selector.

Split from the former single tests/test_image_installer.py; see docs/adr/0001 and iso/raw/.
"""

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "iso/raw"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


raw_e2e = load("raw_e2e", RAW / "raw_e2e.py")
kernel_verity = load("kernel_verity", ROOT / "scripts/kernel_verity.py")


class InstalledSystemCheckTests(unittest.TestCase):
    """The installed-system checks raw_e2e.py took over from the fisherman harness (luks-e2e.sh)."""

    def test_image_name_is_the_repository_without_registry_tag_or_digest(self):
        for ref in ("ghcr.io/projectbluefin/utah@sha256:" + "a" * 64, "ghcr.io/projectbluefin/utah:testing",
                    "localhost:5000/utah:testing", "utah"):
            with self.subTest(ref=ref):
                self.assertEqual(raw_e2e.image_name(ref), "utah")

    def test_every_configured_flavor_is_found_from_its_image(self):
        flavors = json.loads((ROOT / "config/flavors.json").read_text())["flavors"]
        for flavor in flavors:
            image = subprocess.run([sys.executable, str(ROOT / "scripts/flavors.py"), "image", flavor],
                                   capture_output=True, text=True, check=True).stdout.strip()
            with self.subTest(flavor=flavor):
                self.assertEqual(raw_e2e.flavor_of(f"ghcr.io/projectbluefin/{image}@sha256:{'0' * 64}"), flavor)
        with self.assertRaises(SystemExit):
            raw_e2e.flavor_of("ghcr.io/projectbluefin/dakota:testing")

    def test_an_emergency_this_boot_is_seen_although_the_boot_carried_on(self):
        never = "InactiveExitTimestampMonotonic=0\nExecMainStartTimestamp=\n"
        self.assertFalse(raw_e2e.emergency_entered(never))
        self.assertFalse(raw_e2e.emergency_entered("InactiveExitTimestampMonotonic=0\nExecMainStartTimestamp=n/a\n"))
        self.assertTrue(raw_e2e.emergency_entered("InactiveExitTimestampMonotonic=8123456\nExecMainStartTimestamp=\n"))
        self.assertTrue(raw_e2e.emergency_entered(
            "InactiveExitTimestampMonotonic=0\nExecMainStartTimestamp=Thu 2026-11-26 10:00:01 UTC\n"))

    def test_only_a_denied_bootc_or_chcon_probe_fails_the_boot(self):
        denial = ('type=AVC msg=audit(1795247042.592:77): avc:  denied  { mac_admin } for  pid=2690 '
                  'comm="chcon" capability=33  scontext=system_u:system_r:initrc_t:s0 tclass=capability2 permissive=0')
        other = denial.replace('comm="chcon"', 'comm="logind"')
        self.assertEqual(raw_e2e.bootc_probe_denials(f"{other}\n{denial}\n"), [denial])
        self.assertEqual(raw_e2e.bootc_probe_denials(denial.replace("chcon", "bootc")), [denial.replace("chcon", "bootc")])
        self.assertEqual(raw_e2e.bootc_probe_denials(other), [])

    def test_the_flathub_remote_must_be_listed(self):
        self.assertTrue(raw_e2e.flathub_configured("fedora\nflathub\n"))
        self.assertFalse(raw_e2e.flathub_configured("flathub-beta\nfedora\n"))
        self.assertFalse(raw_e2e.flathub_configured(""))

    def test_rounded_blur_runtime_present_and_devel_absent(self):
        self.assertEqual(raw_e2e.rounded_blur_problems(0, 1), [])
        self.assertEqual(raw_e2e.rounded_blur_problems(1, 1),
                         ["installed image lacks the gnome-rounded-blur runtime package"])
        self.assertEqual(raw_e2e.rounded_blur_problems(0, 0),
                         ["build-only gnome-rounded-blur-devel leaked into the installed image"])
        self.assertEqual(raw_e2e.rounded_blur_problems(0, 2), ["could not verify gnome-rounded-blur-devel is absent"])

    def test_extension_state_is_decided_by_extension_state_sh(self):
        for info, expected in (("Name: GSConnect\n  State: ACTIVE\n", ("ACTIVE", True)),
                               ("State: ENABLED\n", ("ENABLED", True)),
                               ("State: ACTIVE\nState: ERROR\n", ("ERROR", False)),
                               ("State: OUT_OF_DATE\n", ("OUT_OF_DATE", False)),
                               ("ssh root: failed (1)\n", ("", False))):
            with self.subTest(info=info):
                self.assertEqual(raw_e2e.extension_state(info), expected)

    def test_fastfetch_is_read_back_by_fastfetch_ocr_match_sh(self):
        with tempfile.TemporaryDirectory() as d:
            transcript = Path(d) / "ocr.txt"
            transcript.write_text("root@utah:~# fastfetch && echo UTAH-E2E-FASTFETCH\n"
                                  "Utah (Version: testing-20261126)\nLinux 7.3.5-200.1.utah1.fc44.x86_64\n"
                                  "GNOME 51.1\nUTAH-E2E-FASTFETCH\n")
            self.assertTrue(raw_e2e.fastfetch_seen(transcript))
            # The sentinel in scrollback alone is not fastfetch on screen.
            transcript.write_text("UTAH-E2E-FASTFETCH\n[tester@utah ~]$\n")
            self.assertFalse(raw_e2e.fastfetch_seen(transcript))
            transcript.write_text("")
            self.assertFalse(raw_e2e.fastfetch_seen(transcript))
        self.assertTrue(raw_e2e.welcome_dialog_showing("Welcome to Bluefin\nSkip  Take Tour\n"))
        self.assertFalse(raw_e2e.welcome_dialog_showing("Linux 7.3"))

    def test_a_frame_with_only_the_top_bar_drawn_is_not_drawn_below_it(self):
        w, h = 200, 100
        top = bytes(b for i in range(w * (h // 10)) for b in (i % 251, (i * 7) % 253, (i * 13) % 255))
        frame = b"P6\n%d %d\n255\n" % (w, h) + top + bytes(3 * w * (h - h // 10))
        with tempfile.TemporaryDirectory() as d:
            ppm = Path(d) / "f.ppm"
            ppm.write_bytes(frame)
            self.assertTrue(raw_e2e.drawn(ppm))
            self.assertFalse(raw_e2e.drawn(ppm, below_top_bar=True))
            ppm.write_bytes(b"P6\n%d %d\n255\n" % (w, h) + top * 10)
            self.assertTrue(raw_e2e.drawn(ppm, below_top_bar=True))

    def run_setup_state(self, failed_unit, disabled="kanata.service"):
        """setup_state_script against e2e-services.sh, with systemctl, sudo and the contract parser stubbed."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "scripts").mkdir()
            (d / "bin").mkdir()
            shutil.copy(ROOT / "iso/scripts/e2e-services.sh", d / "e2e-services.sh")
            (d / "scripts/verify-desktop-contract.py").write_text(
                'import sys\nprint({"enabled": "gdm.service", "user_enabled": "pipewire.socket", '
                f'"disabled": {disabled!r}}}[sys.argv[2]])\n')
            (d / "bin/sudo").write_text('#!/bin/bash\nwhile [[ $1 == -* ]]; do [[ $1 == -p ]] && shift; shift; done\n'
                                        'cat >/dev/null\nexec "$@"\n')
            (d / "bin/systemctl").write_text(
                '#!/bin/bash\ncase "$*" in\n  *is-enabled*) echo enabled ;;\n  *list-units*) ;;\n'
                '  *"$FAILED_UNIT"*) printf "LoadState=loaded\\nActiveState=failed\\nResult=exit-code\\n'
                'ExecMainStartTimestamp=n/a\\n" ;;\n'
                '  *) printf "LoadState=loaded\\nActiveState=active\\nResult=success\\n'
                'ExecMainStartTimestamp=Thu 2026-11-26 10:00:00 UTC\\n" ;;\nesac\n')
            for stub in ("sudo", "systemctl"):
                (d / "bin" / stub).chmod(0o755)
            self.addCleanup(Path("/tmp/utah-e2e-verify-desktop-contract.py").unlink, missing_ok=True)
            original = raw_e2e.GUEST_SCRIPTS
            raw_e2e.GUEST_SCRIPTS = str(d)
            try:
                script = raw_e2e.setup_state_script("repeat")
            finally:
                raw_e2e.GUEST_SCRIPTS = original
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  env={"PATH": f"{d / 'bin'}:/usr/bin:/bin", "FAILED_UNIT": failed_unit})

    def test_the_contract_units_are_checked_by_e2e_services_sh_in_the_guest(self):
        ok = self.run_setup_state("none")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        for unit in ("flatpak-preinstall.service", "ublue-system-setup.service", "ublue-user-setup.service",
                     "gdm.service", "pipewire.socket", "kanata.service"):
            self.assertIn(unit, ok.stdout)
        failed = self.run_setup_state("gdm.service")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("FAIL: system unit gdm.service failed during repeat boot", failed.stderr)

    def test_kanata_is_checked_only_when_the_installed_contract_lists_it(self):
        # A :stable built before kanata (#1297) has no unit and no contract
        # entry; the publish run of that image must not fail on it.
        old = self.run_setup_state("none", disabled="")
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertNotIn("kanata.service", old.stdout)
        broken = self.run_setup_state("kanata.service")
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("FAIL: system unit kanata.service failed during repeat boot", broken.stderr)

    def test_the_reused_fisherman_checks_exist_where_the_harness_reads_them(self):
        # raw_e2e.py runs these from iso/scripts/: removing the ISO path must keep them.
        for script in ("e2e-services.sh", "verify-installed-flavor.py", "verify-countme-runtime.sh",
                       "extension-state.sh", "fastfetch-ocr-match.sh"):
            self.assertTrue((raw_e2e.SCRIPTS / script).is_file(), script)
        self.assertTrue((raw_e2e.REPO / "scripts/verify-desktop-contract.py").is_file())


class KernelVeritySelectorTests(unittest.TestCase):
    """scripts/kernel_verity.py: composefs only on a kernel with the btrfs fs-verity fix."""

    def decide(self, release, changelog="", config=""):
        return kernel_verity.decide(release, changelog, config)[0]

    def test_fixed_kernels(self):
        self.assertTrue(self.decide("7.3.3-200.1.utah1.fc44.x86_64"))  # Utah's rebuild (main, nvidia)
        self.assertTrue(self.decide("7.1.8-ogc1"))  # gaming: before large data folios
        self.assertTrue(self.decide("7.3.9-200.fc44.x86_64", "- btrfs: fix the fsverity callback where "
                                    "unexpected range is verified (Qu Wenruo)"))
        self.assertTrue(self.decide("7.3.9-200.fc44.x86_64", "- Backport 254e15591ee4"))
        self.assertTrue(self.decide("7.4.0-0.rc2.24.fc45.x86_64"))
        self.assertTrue(self.decide("7.4.1-100.fc45.x86_64"))
        self.assertTrue(self.decide("7.5.0-1.fc45.x86_64"))

    def test_unfixed_kernels(self):
        self.assertFalse(self.decide("7.3.3-200.fc44.x86_64"))  # Fedora's signed kernel today (#1379)
        self.assertFalse(self.decide("7.2.15-200.fc44.x86_64", "- btrfs: something else"))
        self.assertFalse(self.decide("7.4.0-0.rc1.20261012git1.fc45.x86_64"))
        self.assertFalse(self.decide("7.4.0-rc1"))
        self.assertFalse(self.decide("7.1.8-ogc1", config="CONFIG_BTRFS_FS=m\nCONFIG_BTRFS_EXPERIMENTAL=y\n"))
        self.assertFalse(self.decide("weird"))

    def test_auto_picks_composefs_only_on_a_fixed_kernel(self):
        fixed = kernel_verity.select("auto", "7.3.3-200.1.utah1.fc44.x86_64", True, "patched")
        self.assertEqual((fixed["backend"], fixed["requested"]), ("composefs", "auto"))
        unfixed = kernel_verity.select("auto", "7.3.3-200.fc44.x86_64", False, "unpatched")
        self.assertEqual(unfixed["backend"], "ostree")
        self.assertEqual(kernel_verity.select("ostree", "7.3.3-200.1.utah1.fc44.x86_64", True, "")["backend"], "ostree")
        self.assertEqual(kernel_verity.select("composefs", "7.1.8-ogc1", True, "")["backend"], "composefs")
        with self.assertRaises(kernel_verity.Unfixed):
            kernel_verity.select("composefs", "7.3.3-200.fc44.x86_64", False, "unpatched")

    def image_root(self, d, release, config=None, ogc=False):
        tree = Path(d) / "usr/lib/modules" / release
        tree.mkdir(parents=True)
        (tree / "vmlinuz").write_bytes(b"\0")
        if config is not None:
            target = Path(d) / "usr/lib/utah/ogc-kernel.config" if ogc else tree / "config"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(config)
        if ogc:
            (Path(d) / "usr/lib/utah").mkdir(parents=True, exist_ok=True)
            (Path(d) / "usr/lib/utah/ogc-kernel-release").write_text(release + "\n")
        return d

    def run_select(self, root, backend):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = kernel_verity.main(["select", "--backend", backend, "--root", root])
        return code, json.loads(out.getvalue()) if out.getvalue() else None

    def test_the_cli_reads_the_image_kernel_and_config(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.image_root(d, "7.1.8-ogc1", "CONFIG_BTRFS_EXPERIMENTAL=y\n", ogc=True)
            self.assertEqual(self.run_select(root, "auto")[1]["backend"], "ostree")
            self.assertEqual(self.run_select(root, "composefs"), (kernel_verity.EXIT_UNFIXED, None))
        with tempfile.TemporaryDirectory() as d:
            root = self.image_root(d, "7.3.3-200.1.utah1.fc44.x86_64", "CONFIG_BTRFS_FS=y\n")
            code, decision = self.run_select(root, "auto")
            self.assertEqual((code, decision["backend"], decision["fixed"]), (0, "composefs", True))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "usr/lib/modules/7.3.3-200.fc44.x86_64").mkdir(parents=True)
            self.image_root(d, "7.1.8-ogc1", ogc=True)
            # Only the tree with a vmlinuz is the kernel bootc deploys.
            self.assertEqual(kernel_verity.booted_kernel(Path(d)), "7.1.8-ogc1")

    def test_the_medium_build_consults_it(self):
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertIn('<"${HERE}/../../scripts/kernel_verity.py"', build)
