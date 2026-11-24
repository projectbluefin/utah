"""GNOME Shell extension and logo coverage for the desktop contract.

Split out of tests/test_desktop_contract.py (#1373): the GNOME extension
contract gate, the GDM greeter logo and the About-panel logos.
"""

import json
import tomllib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_desktop_contract import ROOT, extensions


class GnomeExtensionTests(unittest.TestCase):
    def shipped_tree(self, tmp, versions="51", uuids=None):
        # Modes as the build leaves them, independent of the caller's umask.
        base = Path(tmp) / "usr/share/gnome-shell/extensions"
        for uuid in uuids if uuids is not None else extensions.SOURCE_EXTENSIONS:
            path = base / uuid / "metadata.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"shell-version": [versions]}))
            path.chmod(0o644)
            path.parent.chmod(0o755)
        base.chmod(0o755)
        return Path(tmp)

    def run_main(self, root):
        with patch.object(extensions.sys, "argv", ["verify", "--root", str(root)]):
            return extensions.main()

    def test_complete_tree_declaring_gnome_51_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_main(self.shipped_tree(tmp)), 0)

    def test_metadata_the_user_cannot_read_fails(self):
        # Release zips kept 0600 metadata: gnome-shell, running as the user,
        # could not load those extensions ("Permission denied").
        with tempfile.TemporaryDirectory() as tmp:
            root = self.shipped_tree(tmp)
            uuid = next(iter(extensions.SOURCE_EXTENSIONS))
            (root / "usr/share/gnome-shell/extensions" / uuid / "metadata.json").chmod(0o600)
            self.assertEqual(self.run_main(root), 1)

    def test_untraversable_extension_directory_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.shipped_tree(tmp)
            uuid = next(iter(extensions.SOURCE_EXTENSIONS))
            directory = root / "usr/share/gnome-shell/extensions" / uuid
            directory.chmod(0o744)
            try:
                self.assertEqual(self.run_main(root), 1)
            finally:
                directory.chmod(0o755)

    def test_world_writable_extension_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.shipped_tree(tmp)
            uuid = next(iter(extensions.SOURCE_EXTENSIONS))
            (root / "usr/share/gnome-shell/extensions" / uuid / "metadata.json").chmod(0o666)
            self.assertEqual(self.run_main(root), 1)

    def test_missing_extension_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            uuids = list(extensions.SOURCE_EXTENSIONS)[1:]
            self.assertEqual(self.run_main(self.shipped_tree(tmp, uuids=uuids)), 1)

    def test_extension_without_gnome_51_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_main(self.shipped_tree(tmp, versions="50")), 1)

    def test_invalid_metadata_fails_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.shipped_tree(tmp)
            uuid = next(iter(extensions.SOURCE_EXTENSIONS))
            (root / f"usr/share/gnome-shell/extensions/{uuid}/metadata.json").write_text("{")
            self.assertEqual(self.run_main(root), 1)

    def test_shell_versions_rejects_metadata_without_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.json"
            path.write_text(json.dumps({"name": "x"}))
            with self.assertRaises(ValueError):
                extensions.shell_versions(path)

    def test_shell_versions_substitutes_the_gsconnect_version_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.json.in"
            path.write_text('{"version": @PACKAGE_VERSION@, "shell-version": ["51"]}')
            self.assertEqual(extensions.shell_versions(path), ["51"])

    def test_source_mode_matches_the_checked_in_submodule_paths(self):
        for uuid, relative in extensions.SOURCE_EXTENSIONS.items():
            self.assertTrue(
                relative == uuid + "/metadata.json" or relative.endswith("metadata.json")
                or relative.endswith("metadata.json.in"),
                f"{uuid} source path does not point at extension metadata: {relative}",
            )

    def test_enabled_extensions_override_matches_contract_and_ships(self):
        # The in-image gsettings check pins the effective value; this keeps
        # the source override in step with it and every enabled UUID shipped.
        override = (
            ROOT / "system_files/shared/usr/share/glib-2.0/schemas/zz3-bluefin-unsupported-stuff.gschema.override"
        ).read_text()
        line = next(line for line in override.splitlines() if line.startswith("enabled-extensions="))
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        expected = contract["gsettings"]["values"]["org.gnome.shell enabled-extensions"]
        self.assertEqual(line.removeprefix("enabled-extensions="), expected)
        for uuid in json.loads(expected.replace("'", '"')):
            with self.subTest(uuid=uuid):
                self.assertIn(uuid, extensions.SOURCE_EXTENSIONS)

    def test_bling_sync_folder_is_the_only_syncthing_toggle(self):
        # Dakota ships only bling's syncthing-toggle@projectbluefin.io (#1357);
        # it keeps its own schema, which the contract asserts in the image.
        self.assertNotIn("rehhouari", (ROOT / ".gitmodules").read_text())
        self.assertEqual(
            [uuid for uuid in extensions.SOURCE_EXTENSIONS if uuid.startswith("syncthing-toggle@")],
            ["syncthing-toggle@projectbluefin.io"],
        )
        self.assertNotIn("rehhouari", (ROOT / "scripts/build-gnome-extensions.sh").read_text())
        schema = "org.gnome.shell.extensions.syncthing-toggle.gschema.xml"
        bling = ROOT / "system_files/shared/usr/share/gnome-shell/extensions/tmp/bluefin-bling/extensions/syncthing-toggle"
        self.assertTrue((bling / "schemas" / schema).is_file())
        files = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())["configuration"]["files"]
        self.assertIn(f"/usr/share/glib-2.0/schemas/{schema}", files)
        self.assertIn("/usr/share/gnome-shell/extensions/syncthing-toggle@projectbluefin.io/metadata.json", files)


class GdmGreeterLogoTests(unittest.TestCase):
    """Issue #378: the GDM greeter logo must point at the Bluefin mark.

    Without an ``org.gnome.login-screen.logo`` override in the ``gdm`` dconf
    profile, gnome-shell falls back to ``/usr/share/pixmaps/fedora-gdm-logo.png``
    from ``fedora-logos``, and the login screen shows the Fedora wordmark
    instead of Bluefin. The fix is a keyfile under
    ``/etc/dconf/db/gdm.d/`` plus a desktop-contract assertion that catches a
    regression at build time rather than at the post-install E2E.
    """

    CONTRACT_PATH = ROOT / "contracts/bluefin-desktop.toml"
    KEYFILE_PATH = ROOT / "system_files/shared/etc/dconf/db/gdm.d/01-bluefin-gdm-logo"
    BLUEFIN_LOGO = "/usr/share/pixmaps/bluefin-gdm-logo.png"

    def test_shipped_contract_declares_gdm_keyfile(self):
        import tomllib
        contract = tomllib.loads(self.CONTRACT_PATH.read_text())
        config_files = contract.get("configuration", {}).get("files", [])
        self.assertIn("/etc/dconf/db/gdm.d/01-bluefin-gdm-logo", config_files)

    def test_shipped_contract_asserts_bluefin_logo_in_gdm_keyfile(self):
        import tomllib
        contract = tomllib.loads(self.CONTRACT_PATH.read_text())
        file_contains = (
            contract.get("configuration", {}).get("file_contains", {})
        )
        gdm_expectations = file_contains.get(
            "/etc/dconf/db/gdm.d/01-bluefin-gdm-logo"
        )
        # The contract must require both the schema header and the logo path:
        # losing either is the exact regression that lets the Fedora fallback
        # resurface (the schema without the path, or the path without the
        # schema).
        self.assertIsNotNone(gdm_expectations)
        self.assertIn("[org/gnome/login-screen]", gdm_expectations)
        self.assertIn(f"logo='{self.BLUEFIN_LOGO}'", gdm_expectations)

    def test_keyfile_points_at_shipped_bluefin_logo(self):
        """The on-disk keyfile must reference the asset the contract asserts."""
        content = self.KEYFILE_PATH.read_text()
        self.assertIn("[org/gnome/login-screen]", content)
        self.assertIn(f"logo='{self.BLUEFIN_LOGO}'", content)

    def test_logo_is_a_small_wordmark_rendered_from_pinned_artwork(self):
        """gnome-shell draws the greeter logo at natural size.

        The full-size ``bluefin.png`` (372x493) fills the login screen, so the
        logo must stay a small wordmark. The PNG is no longer committed: the
        branding script rasterizes artwork's pinned wordmark SVG into a box,
        and that box is what is capped here.
        """
        import re
        script = (ROOT / "scripts/configure-branding.sh").read_text()
        width = int(re.search(r"^GDM_LOGO_MAX_WIDTH=(\d+)$", script, re.M).group(1))
        height = int(re.search(r"^GDM_LOGO_MAX_HEIGHT=(\d+)$", script, re.M).group(1))
        self.assertLessEqual(width, 256)
        self.assertLessEqual(height, 128)
        self.assertIn(self.BLUEFIN_LOGO, script)
        self.assertIn("/tmp/utah-artwork-branding/bluefin-logo-dark.svg", script)
        self.assertFalse(
            (ROOT / "system_files/shared" / self.BLUEFIN_LOGO.lstrip("/")).exists(),
            "the greeter logo is rendered at build time; a committed copy would be shadowed or stale",
        )

    def test_keyfile_does_not_reference_fedora_fallback(self):
        """A regression to the Fedora wordmark must be caught at the source.

        Only the active dconf lines matter: ``dconf update`` ignores
        everything after a leading ``#``. Allowing the path in a comment keeps
        the file's preamble informative without weakening the assertion that
        the live override points at Bluefin.
        """
        active_lines = [
            line for line in self.KEYFILE_PATH.read_text().splitlines()
            if line and not line.lstrip().startswith("#")
        ]
        active = "\n".join(active_lines)
        self.assertNotIn("fedora-gdm-logo", active)
        self.assertNotIn("/usr/share/pixmaps/fedora", active)
        self.assertEqual(len(active_lines), 2, f"unexpected active lines: {active_lines}")


class AboutLogoContractTests(unittest.TestCase):
    OLD = ("fedora_logo_med.png", "fedora_whitelogo_med.png",
           "fedora-logo.png", "system-logo-white.png")

    def test_contract_asserts_bluefin_artwork_logos(self):
        data = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        files = data["branding"]["files"]
        for name in ("bluefin-logo-light.png", "bluefin-logo-dark.png"):
            self.assertIn(f"/usr/share/bluefin-artwork-logos/{name}", files)
        for name in self.OLD:
            self.assertNotIn(f"/usr/share/pixmaps/{name}", files)

    def test_branding_does_not_restore_unread_fedora_pixmaps(self):
        script = (ROOT / "scripts/configure-branding.sh").read_text()
        containerfile = (ROOT / "Containerfile").read_text()
        self.assertNotIn("utah-bluefin-pixmaps", script)
        self.assertNotIn("utah-bluefin-pixmaps", containerfile)
        for name in self.OLD:
            self.assertNotIn(name, script)


if __name__ == "__main__":
    unittest.main()
