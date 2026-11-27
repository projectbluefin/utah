"""The boot gates kept from the fisherman harness: fastfetch OCR and OGC kernel config."""
import subprocess
from pathlib import Path
import tempfile
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

    # Verbatim from fastfetch-ocr.txt in the iso-diagnostics-utah-nvidia artifact
    # of run 45636142672: the kernel line is garbled and no desktop, terminal or
    # shell field was read. The screenshot showed all of fastfetch.
    TRUNCATED_BODY_TRANSCRIPT = """ujust bluefin-c
brey
ujust
+ DReport an issue
+ B Documentation h
UTAH-E2E-FASTFETCH
[utahtest@utah-luk
Wed Nov 25 3:35PM
Q Type to search
utahtest@utah-luks-test:~
Enable terminal bling
Manage command line packages
Report an issue to the contri
> uti
> Bluefin (Version: testing-
> Linu 200.1.utah1.fc4:
nin
Forged on Nov 25 2026
KVM/QEMU Standard PC (Q35 + ICH9,
Intel(R) Xeon(R) 6973P-C (2) @ 2
troll
60
e95e91)
109) (pe
GHz
2
” oO
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

    def test_accepts_the_sentinel_with_only_the_hardware_block(self):
        self.assertTrue(self.matches(self.TRUNCATED_BODY_TRANSCRIPT))

    def test_rejects_suffix_with_kernel_but_no_second_body_token(self):
        self.assertFalse(self.matches(
            "uran-£26-FASTFETCH\nLinux 7.2.7-200.fc44.x86_64\n2 mins\n"))

    def test_rejects_suffix_with_body_but_no_kernel_token(self):
        self.assertFalse(self.matches(
            "uran-£26-FASTFETCH\nGNOME 51.0\nMutter (Wayland)\n"))

    def test_rejects_an_empty_or_missing_transcript(self):
        self.assertFalse(self.matches(""))
        self.assertFalse(
            subprocess.run(["bash", str(self.GATE), "/nonexistent/ocr.txt"]).returncode == 0)


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

    def test_the_tpm_drivers_are_required_and_built_in(self):
        # Without them the gaming kernel sees no TPM 2.0: gnome-setup asks for
        # a passphrase and TPM2 unlock cannot work (hanthor/utah run
        # 45742479307). Built in, so the initrd needs no module for it.
        symbols = self.required_symbols()
        for symbol in ("TCG_TPM", "TCG_TIS", "TCG_CRB"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, symbols)
                self.assertRegex(self.SCRIPT.read_text(), rf"--enable {symbol}(?:\s|$)")

    def test_guest_vsock_is_required_and_built_in(self):
        # The E2E harnesses reach the live session over AF_VSOCK (#1508); on a
        # kernel without it the gaming legs never get SSH (run 45858480299).
        symbols = self.required_symbols()
        for symbol in ("VSOCKETS", "VIRTIO_VSOCKETS"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, symbols)
                self.assertRegex(self.SCRIPT.read_text(), rf"--enable {symbol}(?:\s|$)")

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
