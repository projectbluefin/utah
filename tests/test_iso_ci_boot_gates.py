"""The ISO boot gates: fastfetch OCR, OGC kernel config, and tacklebox boot."""
import os
import subprocess
from pathlib import Path
import tempfile
import time
import unittest

from test_iso_ci import ROOT


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

    # Verbatim excerpt from fastfetch-ocr.txt in the iso-diagnostics-utah-nvidia
    # artifact of run 44741850768: the kernel line came back garbled. The
    # screenshot showed all of fastfetch.
    GARBLED_KERNEL_TRANSCRIPT = """ujust bluefin-c
brey
ujust
+ DReport an issue
+ B Documentation h
UTAH-E2E-FASTFETCH
[utahtest@utah-luk
FriNov 20 1:52PM
Q Typeto search
utahtest@utah-luks-test:~
» Bluefin (Version:
> Linu 200.1.utah1.fc4:
Forged on Nov 20 2026
KVM/QEMU Standard PC (Q35 + ICH9
» GNOME 51.1
» Mutter (Wa
» bash
» ghostty 1.3.2-HEAD-+ef7edc4
"""

    # Verbatim excerpt from fastfetch-ocr.txt in the
    # iso-diagnostics-utah-nvidia-gaming artifact of run 44719832822: no
    # kernel line at all, on a screenshot that showed "Linux 7.1.8-ogc1".
    MISSING_KERNEL_TRANSCRIPT = """UTAH-E2E-FASTFETCH
[utahtest@utah-luks-test ~
Forged on Nov
KVM/QEMU
AMD EPYC 7763 64-Core (2) @ 2.
» GNOME 51.1
» Mutter (Wa
» ghostty 1.
» 44 (Flatpa
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

    def test_accepts_the_sentinel_with_a_garbled_kernel_line(self):
        self.assertTrue(self.matches(self.GARBLED_KERNEL_TRANSCRIPT))

    def test_accepts_the_sentinel_with_no_kernel_line(self):
        self.assertTrue(self.matches(self.MISSING_KERNEL_TRANSCRIPT))

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

    def test_zswap_and_zstd_are_required_for_the_zswap_kargs(self):
        # kargs.d/20-zswap.toml ships on every image (#1224); without these the
        # gaming kernel ignores zswap.* and the swapfile runs uncompressed.
        symbols = self.required_symbols()
        for symbol in ("ZSWAP", "CRYPTO_ZSTD"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, symbols)
                body = "".join(f"CONFIG_{s}=y\n" for s in symbols if s != symbol)
                result = self.gate(body)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"missing CONFIG_{symbol}", result.stderr)
        self.assertIn("--enable ZSWAP --enable CRYPTO_ZSTD", self.SCRIPT.read_text())

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

class TackleboxBootGateTests(unittest.TestCase):
    """iso/scripts/tacklebox-boot-gate.sh against a stub QEMU."""

    GATE = ROOT / "iso/scripts/tacklebox-boot-gate.sh"
    # A stand-in for qemu-system-x86_64 -daemonize: it leaves a "VM" process
    # behind, records its pid in -pidfile, and writes STUB_SERIAL_TEXT to the
    # -serial file after STUB_SERIAL_DELAY seconds. STUB_VM_EXITS makes the
    # "VM" die at once, as a QEMU crash would.
    STUB = r"""#!/usr/bin/env bash
serial="" pidfile=""
while (($#)); do
  case "$1" in
    -serial) serial="${2#file:}"; shift ;;
    -pidfile) pidfile="$2"; shift ;;
  esac
  shift
done
if [[ -n "${STUB_VM_EXITS:-}" ]]; then
  true & echo $! > "$pidfile"; wait
  exit 0
fi
( sleep "${STUB_SERIAL_DELAY:-0}"; printf '%s\n' "${STUB_SERIAL_TEXT:-}" >> "$serial"; exec sleep 300 ) \
  </dev/null >/dev/null 2>&1 &
echo $! > "$pidfile"
"""

    def run_gate(self, timeout=60, **stub_env):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        bindir = tmp / "bin"
        bindir.mkdir()
        qemu = bindir / "qemu-system-x86_64"
        qemu.write_text(self.STUB)
        qemu.chmod(0o755)
        for name in ("code.fd", "vars.fd", "live.iso"):
            (tmp / name).write_bytes(b"")
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   UTAH_ISO_BOOT_WORK=str(tmp / "work"),
                   UTAH_ISO_BOOT_OVMF_CODE=str(tmp / "code.fd"),
                   UTAH_ISO_BOOT_OVMF_VARS=str(tmp / "vars.fd"),
                   UTAH_ISO_BOOT_TIMEOUT=str(timeout),
                   UTAH_ISO_BOOT_POLL="1", **stub_env)
        start = time.monotonic()
        result = subprocess.run(["bash", str(self.GATE), str(tmp / "live.iso")],
                                capture_output=True, text=True, env=env, timeout=timeout + 30)
        elapsed = time.monotonic() - start
        pid = (tmp / "work/boot.pid").read_text().strip()
        return result, elapsed, pid

    def assert_vm_stopped(self, pid):
        for _ in range(50):
            if subprocess.run(["kill", "-0", pid], capture_output=True).returncode != 0:
                return
            time.sleep(0.1)
        subprocess.run(["kill", pid])
        self.fail(f"the gate left the VM (pid {pid}) running")

    def test_returns_as_soon_as_the_marker_reaches_serial(self):
        # Runs 44538634534 and 44604822618 waited the full 1800 s timeout
        # although the marker was there: the loop only left on a systemd
        # "Reached target" line that never reached serial.
        result, elapsed, pid = self.run_gate(timeout=60, STUB_SERIAL_DELAY="2",
                                             STUB_SERIAL_TEXT="UTAH_LIVE_READY")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:", result.stdout)
        self.assertLess(elapsed, 20, "the gate waited out its timeout after the marker appeared")
        self.assert_vm_stopped(pid)

    def test_a_vm_that_exits_fails_without_waiting_out_the_timeout(self):
        result, elapsed, pid = self.run_gate(timeout=60, STUB_VM_EXITS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("QEMU exited", result.stderr)
        self.assertLess(elapsed, 20)

    def test_kernel_panic_fails_fast(self):
        result, elapsed, pid = self.run_gate(timeout=60, STUB_SERIAL_TEXT="Kernel panic - not syncing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("kernel panic", result.stderr)
        self.assertLess(elapsed, 20)
        self.assert_vm_stopped(pid)

    def test_a_missing_marker_still_fails_at_the_timeout(self):
        result, elapsed, pid = self.run_gate(timeout=3, STUB_SERIAL_TEXT="Reached target graphical.target")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("never appeared", result.stderr)
        self.assert_vm_stopped(pid)

    def test_every_qemu_job_grants_kvm_before_booting(self):
        # tacklebox-iso lacked the grant and its boot ran under TCG.
        import yaml
        jobs = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())["jobs"]
        for name in ("luks", "lifecycle", "tacklebox-iso"):
            prepare = next(s for s in jobs[name]["steps"] if s.get("name") == "Prepare ISO and QEMU host")
            self.assertIn("sudo chmod a+rw /dev/kvm", prepare["run"], name)
            self.assertIn("test -r /dev/kvm && test -w /dev/kvm", prepare["run"], name)
