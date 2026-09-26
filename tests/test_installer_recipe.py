"""An interactive install must leave the user a way to create an account.

Bluefin and Dakota create the first account in GNOME Initial Setup, so their
installer recipes skip the user step. Utah does not ship gnome-initial-setup
(Hummingbird and the factory do not build it), so a recipe without the user
step installed a system that booted to GDM with no account to log in to. The
LUKS e2e never noticed: it hands fisherman a recipe with a "user" directly.
"""
import json
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "iso/live/src/etc/bootc-installer/recipe.json"
IMAGES = ROOT / "iso/live/src/etc/bootc-installer/images.json"
CONTRACT = ROOT / "contracts/bluefin-desktop.toml"


PACKAGES = ROOT / "packages/utah.toml"


def ships_initial_setup():
    """Whether the image installs gnome-initial-setup (any utah.toml section)."""
    data = tomllib.loads(PACKAGES.read_text())
    return any("gnome-initial-setup" in section.get("packages", [])
               for section in data.values() if isinstance(section, dict))


class InstallerAccountTests(unittest.TestCase):
    def test_recipe_parses(self):
        json.loads(RECIPE.read_text())
        tomllib.loads(CONTRACT.read_text())

    def test_account_is_created_somewhere(self):
        steps = json.loads(RECIPE.read_text())["steps"]
        templates = {step["template"] for step in steps.values()}
        self.assertTrue(
            "user" in templates or ships_initial_setup(),
            "the installer recipe has no user step and the image does not ship "
            "gnome-initial-setup: an interactive install would have no account",
        )

    def test_images_json_agrees_with_the_recipe(self):
        steps = json.loads(RECIPE.read_text())["steps"]
        has_user_step = any(s["template"] == "user" for s in steps.values())
        image = json.loads(IMAGES.read_text())["images"][0]
        # bootc-installer hides the user step unless the image asks for it, and
        # an image that asks for one with no step to collect it would stall.
        self.assertEqual(image.get("needs_user_creation", True), has_user_step)

    def test_initial_setup_owns_the_first_account(self):
        """Like Bluefin and Dakota: Initial Setup, not the installer, creates it.

        With a user step, the installer creates an account, GDM finds one, and
        gnome-initial-setup never runs on first boot.
        """
        steps = json.loads(RECIPE.read_text())["steps"]
        if ships_initial_setup():
            self.assertNotIn("user", {s["template"] for s in steps.values()})


class InstallerFlatpakPathTests(unittest.TestCase):
    """The installer's flatpaks must land where the installed system reads them.

    fisherman treats flatpak_var_path as the target's *var* directory and
    appends lib/flatpak. Utah set it to "var/lib/flatpak", so a GUI install
    copied every Flatpak to /sysroot/var/lib/flatpak/lib/flatpak: the
    installed system had none, and the dock (Firefox, Bazaar, Files) was
    empty. The e2e sends no such key and so never saw it. Without the key,
    fisherman finds ostree/deploy/default/var itself.
    """

    def test_flatpak_var_path_is_a_var_root_if_set(self):
        for image in json.loads(IMAGES.read_text())["images"]:
            path = image.get("flatpak_var_path")
            if path:
                self.assertFalse(path.rstrip("/").endswith("lib/flatpak"),
                                 f"{path!r} is the flatpak dir; fisherman appends lib/flatpak itself")


if __name__ == "__main__":
    unittest.main()
