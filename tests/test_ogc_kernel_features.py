"""Cached gaming kernels must support desktop sandboxes and VPN devices."""
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts/install-ogc-kernel.sh").read_text()
DECLARATION = SOURCE[SOURCE.index("required_config=("):SOURCE.index("\nverify_config()")]
VERIFY = re.search(r"^verify_config\(\) \{.*?^\}", SOURCE, re.M | re.S).group()
SYMBOLS = shlex.split(DECLARATION.removeprefix("required_config=(").rstrip().removesuffix(")"))


class GamingKernelFeaturesTests(unittest.TestCase):
    def verify(self, missing=None):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "kernel.config"
            config.write_text("".join(f"CONFIG_{s}={'m' if s == 'TUN' else 'y'}\n"
                                      for s in SYMBOLS if s != missing))
            return subprocess.run(["bash", "-eu", "-c", DECLARATION + "\n" + VERIFY +
                                   '\nverify_config "$1"', "verify", str(config)],
                                  text=True, capture_output=True)

    def test_complete_config_accepts_modular_tun(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cached_config_rejects_each_missing_sandbox_or_vpn_feature(self):
        for symbol in ("NAMESPACES", "USER_NS", "MEMCG", "SECCOMP", "SECCOMP_FILTER", "TUN"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, SYMBOLS)
                result = self.verify(symbol)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CONFIG_" + symbol, result.stderr)


if __name__ == "__main__":
    unittest.main()
