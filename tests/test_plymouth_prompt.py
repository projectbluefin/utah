"""plymouth-prompt.py must tell a branded graphical LUKS prompt from everything else.

The e2e's verdict on #461 rests on this decision, and the only frames CI will
ever show it are passing ones. fixtures/plymouth/branded-prompt.png is a real
screendump (converted to PNG with ffmpeg, as the harness does) of Utah's
rebuilt initramfs at the passphrase prompt under QEMU stdvga at 1920x1080,
with the bgrt theme and common's watermark, which is fixtures/plymouth/
watermark.png (projectbluefin/common, Apache-2.0). The negatives are that same
frame with the mark removed or the expected art changed, so they differ from
the positive in exactly the property under test.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/plymouth"
spec = importlib.util.spec_from_file_location("plymouth_prompt", ROOT / "iso/scripts/plymouth-prompt.py")
prompt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prompt)

# The alignment keys of the image's bgrt.plymouth [two-step] section.
BGRT = "[two-step]\nWatermarkHorizontalAlignment=.5\nWatermarkVerticalAlignment=.96\n"


class WatermarkMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frame = prompt.read_image(str(FIXTURES / "branded-prompt.png"))
        cls.watermark = prompt.read_image(str(FIXTURES / "watermark.png"))

    def without_region(self, x0, y0, w, h):
        fw, fh, channels, pixels = self.frame
        out = bytearray(pixels)
        for y in range(y0, y0 + h):
            start = (y * fw + x0) * channels
            out[start:start + w * channels] = bytes(w * channels)
        return fw, fh, channels, bytes(out)

    def test_real_branded_prompt_matches_at_the_themes_position(self):
        matched, best, _, where = prompt.watermark_match(self.frame, self.watermark, BGRT)
        self.assertTrue(matched)
        # two-step: alignment * (screen - image) = .5*(1920-149), .96*(1080-43)
        self.assertEqual(where, (885, 995))
        self.assertLess(best, 1.0)

    def test_prompt_with_the_mark_removed_is_unbranded(self):
        frame = self.without_region(885, 995, 149, 43)
        matched, best, contrast, _ = prompt.watermark_match(frame, self.watermark, BGRT)
        self.assertFalse(matched)
        self.assertAlmostEqual(best, contrast, places=3)

    def test_a_different_mark_in_the_same_place_does_not_match(self):
        # Mirrored art: same size, colours and position, wrong picture.
        w, h, channels, pixels = self.watermark
        rows = [pixels[y * w * channels:(y + 1) * w * channels] for y in range(h)]
        mirrored = b"".join(
            b"".join(row[x * channels:(x + 1) * channels] for x in reversed(range(w)))
            for row in rows)
        matched, *_ = prompt.watermark_match(self.frame, (w, h, channels, mirrored), BGRT)
        self.assertFalse(matched)

    def test_the_mark_away_from_the_themes_position_does_not_match(self):
        theme = BGRT.replace("WatermarkVerticalAlignment=.96", "WatermarkVerticalAlignment=.5")
        matched, *_ = prompt.watermark_match(self.frame, self.watermark, theme)
        self.assertFalse(matched)

    def test_theme_without_watermark_alignment_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "WatermarkVerticalAlignment"):
            prompt.watermark_match(self.frame, self.watermark,
                                   "[two-step]\nWatermarkHorizontalAlignment=.5\n")


class TextFallbackTests(unittest.TestCase):
    def test_prompt_on_the_serial_console_fails_as_text_fallback(self):
        # Plymouth's details plugin is the only mode that writes the prompt to
        # a serial console; the shipped image did exactly this under QEMU.
        with tempfile.TemporaryDirectory() as tmp:
            serial = Path(tmp) / "serial.log"
            serial.write_text("[  OK  ] Started plymouth-start.service\n"
                              "Please enter passphrase for disk root::")
            theme = Path(tmp) / "bgrt.plymouth"
            theme.write_text(BGRT)
            # No monitor socket: the serial verdict must come before any capture.
            status = prompt.wait(str(Path(tmp) / "absent.sock"), str(serial), str(theme),
                                 str(FIXTURES / "watermark.png"), str(Path(tmp) / "frame.ppm"), 5)
        self.assertEqual(status, 3)


if __name__ == "__main__":
    unittest.main()
