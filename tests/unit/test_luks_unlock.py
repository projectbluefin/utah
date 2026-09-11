#!/usr/bin/env python3
"""Unit tests for iso/scripts/luks-unlock.py."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "iso" / "scripts" / "luks-unlock.py"
spec = importlib.util.spec_from_file_location("luks_unlock", SCRIPT_PATH)
luks_unlock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(luks_unlock)


class TestLuksUnlock(unittest.TestCase):
    def test_qemu_check_serial_plymouth(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as f:
            f.write("System booting...\nPlease enter passphrase for disk /dev/vda3:\n")
            f.flush()
            self.assertEqual(luks_unlock.qemu_check_serial(f.name), "plymouth")

    def test_qemu_check_serial_graphical_ok(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as f:
            f.write("Services starting...\nUTAH_INSTALLED_GRAPHICAL_OK\n")
            f.flush()
            self.assertEqual(luks_unlock.qemu_check_serial(f.name), "graphical-ok")

    def test_qemu_check_serial_gdm(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as f:
            f.write("Starting display manager...\n[  OK  ] Started gdm.service - GNOME Display Manager.\n")
            f.flush()
            self.assertEqual(luks_unlock.qemu_check_serial(f.name), "gdm")

    def test_qemu_check_serial_emergency(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as f:
            f.write("Failed to mount /boot.\nYou are in emergency mode. After logging in, type 'journalctl -xb'.\n")
            f.flush()
            self.assertEqual(luks_unlock.qemu_check_serial(f.name), "emergency")

    def test_qemu_check_serial_empty_or_missing(self):
        self.assertEqual(luks_unlock.qemu_check_serial("/nonexistent/file.log"), "")
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as f:
            f.write("Booting Linux...\n")
            f.flush()
            self.assertEqual(luks_unlock.qemu_check_serial(f.name), "")

    def test_qemu_screendump_parsing(self):
        # Create a mock PPM image: P6 header, 2x2 pixels, 255 max val
        header = b"P6\n2 2\n255\n"
        # 4 pixels, 3 bytes each (R,G,B) = 12 bytes
        pixels = bytes([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120])
        ppm_content = header + pixels

        with tempfile.NamedTemporaryFile("wb") as f:
            f.write(ppm_content)
            f.flush()
            with patch.object(luks_unlock, "monitor_command", return_value=""):
                brightness, md5 = luks_unlock.qemu_screendump("/mock/sock", f.name)
                self.assertGreater(brightness, 0)
                self.assertEqual(len(md5), 32)

    def test_qemu_send_passphrase(self):
        sent_commands = []

        def mock_monitor_command(sock, cmd, settle=0.05):
            sent_commands.append(cmd)
            return ""

        with patch.object(luks_unlock, "monitor_command", side_effect=mock_monitor_command), \
             patch("time.sleep", return_value=None):
            luks_unlock.qemu_send_passphrase("/mock/sock", "abc-12")

        expected = [
            "sendkey a",
            "sendkey b",
            "sendkey c",
            "sendkey minus",
            "sendkey 1",
            "sendkey 2",
            "sendkey ret",
        ]
        self.assertEqual(sent_commands, expected)


if __name__ == "__main__":
    unittest.main()
