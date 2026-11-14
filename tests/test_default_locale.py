"""The image must ship a default system locale (#1211).

Without /etc/locale.conf the first-boot GNOME Initial Setup session runs in
the C locale. Its keyboard page preselects a layout from either the
system-wide keyboard layout (localed's X11Layout) or gnome-desktop's default
input source for the current language, and utah sets neither: C maps to no
input source, so the page opened with nothing selected and Next disabled.

en_US.UTF-8 maps to the `us` layout. It is a locale rather than a
vconsole.conf XKBLAYOUT on purpose: a system-wide layout outranks the
language lookup on that page, so a default `us` there would override the
layout of a language the user picks on the page before it, and GIS would
also add it silently to every user's input sources. The language page
rewrites locale.conf through localed, and an installer or `localectl
set-locale` replaces the file; bootc's /etc merge keeps that local copy on
update, so the image default never overrides a user's or installer's choice.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALE_CONF = ROOT / "system_files/shared/etc/locale.conf"
UTAH_TOML = ROOT / "packages/utah.toml"
CONTAINERFILE = ROOT / "Containerfile"


def assignments(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return values


class DefaultLocaleTests(unittest.TestCase):
    def test_locale_conf_sets_lang(self):
        """locale.conf must set LANG to en_US.UTF-8."""
        self.assertTrue(LOCALE_CONF.is_file(), "etc/locale.conf is missing")
        self.assertEqual(assignments(LOCALE_CONF).get("LANG"), "en_US.UTF-8")

    def test_locale_conf_sets_only_lang(self):
        """No LC_* overrides: the language page replaces LANG alone."""
        self.assertEqual(set(assignments(LOCALE_CONF)), {"LANG"})

    def test_locale_data_is_installed(self):
        """en_US needs glibc-all-langpacks; C/C.UTF-8 is all the base has."""
        self.assertIn('"glibc-all-langpacks"', UTAH_TOML.read_text())

    def test_system_files_are_copied_into_the_image(self):
        """system_files/shared lands at / via the utah-local stage."""
        text = CONTAINERFILE.read_text()
        self.assertIn("COPY system_files/shared /tmp/utah-local", text)
        self.assertIn("utah-local/. /", text)

    def test_no_system_keyboard_layout_is_shipped(self):
        """A shipped XKBLAYOUT would outrank the user's language choice."""
        etc = ROOT / "system_files/shared/etc"
        self.assertFalse((etc / "vconsole.conf").exists())
        self.assertFalse((etc / "X11/xorg.conf.d/00-keyboard.conf").exists())


if __name__ == "__main__":
    unittest.main()
