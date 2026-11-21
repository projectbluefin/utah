"""utah_bootc_probe.cil must dontaudit exactly bootc's mac_admin probe (#1198).

It also carries one rule for systemd_nsresourced_t dac_read_search (#1329).

`bootc status` probes for mac_admin with a chcon to a label the policy does
not define. Run from a plain service (common's projectbluefin-countme) the
probe is denied in unconfined_service_t and logs two AVC denials every boot,
which fail the testsuite's SELinux gate. A service that runs a script with
/bin/sh (the installer's fisherman-boot-receipt.service, #1129) is in
initrc_t and logs the same pair there. The module hides that one check in
those two domains and grants nothing: an allow would let every such service
write labels the policy does not know and would change what bootc's probe
decides.

The image ships the module from the build (the store lives under /etc/selinux
since #551), and the desktop contract's [selinux] list makes the build fail if it is
not loaded (`semodule -l`, tests/test_desktop_contract_verify.py).
"""
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE_PATH = "/usr/share/selinux/packages/utah/utah_bootc_probe.cil"
MODULE = ROOT / "system_files/shared" / IMAGE_PATH.lstrip("/")
CONTAINERFILE = ROOT / "Containerfile"
CONTRACT = ROOT / "contracts/bluefin-desktop.toml"
RULES = [
    "(dontaudit unconfined_service_t self (capability2 (mac_admin)))",
    "(dontaudit initrc_t self (capability2 (mac_admin)))",
    # systemd-nsresourced's own dac_read_search check, denied at every boot and
    # counted by the testsuite's SELinux gate (#1329).
    "(dontaudit systemd_nsresourced_t self (capability (dac_read_search)))",
]


def statements(text: str) -> list[str]:
    """The module's CIL with `;` comments and blank lines removed."""
    lines = (line.split(";", 1)[0].strip() for line in text.splitlines())
    return [line for line in lines if line]


class BootcProbeModuleTests(unittest.TestCase):
    def test_the_module_is_exactly_the_probe_dontaudit_rules(self):
        self.assertEqual(statements(MODULE.read_text()), RULES)

    def test_the_module_grants_nothing(self):
        # Belt and braces for the exact-match test: a later edit that adds a
        # rule must not add an allow alongside the dontaudit.
        body = " ".join(statements(MODULE.read_text()))
        self.assertNotRegex(body, r"\(\s*allow")

    def test_the_build_loads_the_module_before_the_contract_checks_it(self):
        text = CONTAINERFILE.read_text()
        install = f"semodule -n -i {IMAGE_PATH} && \\"
        self.assertIn(install, text)
        verify = text.index("/usr/local/libexec/utah-verify-desktop-contract /usr/share/utah/bluefin-desktop.toml")
        self.assertLess(text.index(install), verify)
        # Same RUN step: no instruction starts between the two.
        between = text[text.index(install):verify]
        self.assertIsNone(re.search(r"^(RUN|COPY|FROM|ARG)\b", between, re.M))

    def test_the_contract_requires_the_module(self):
        contract = tomllib.loads(CONTRACT.read_text())
        self.assertIn(MODULE.stem, contract["selinux"]["modules"])


if __name__ == "__main__":
    unittest.main()
