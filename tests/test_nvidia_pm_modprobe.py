"""Exercise Utah's runtime-PM policy with real kmod, never loading a module.

These checks prove parameter composition, not NVIDIA suspend/resume efficacy.
The install trap lets --show-depends resolve nvidia without a driver artifact;
executing it would leave a sentinel and fail the safety invariant.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PM_CONF = ROOT / "system_files/shared/usr/lib/modprobe.d/zz-nvidia-pm.conf"
PARAMETER = "NVreg_DynamicPowerManagement"


class NvidiaPmModprobeTests(unittest.TestCase):
    def setUp(self):
        self.modprobe = shutil.which("modprobe")
        if self.modprobe is None:
            self.fail("kmod's modprobe is required for runtime-PM consumer tests")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / "modprobe.d"
        self.config.mkdir()
        self.sentinel = self.root / "unexpected-execution"
        self.trap = self.root / "install-trap"
        self.trap.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(self.sentinel))}\n")
        self.trap.chmod(0o700)
        (self.config / "00-consumer.conf").write_text(f"install nvidia {self.trap}\n")
        shutil.copy2(PM_CONF, self.config / PM_CONF.name)

    def options(self, *load_options):
        env = os.environ.copy()
        env.pop("MODPROBE_OPTIONS", None)
        result = subprocess.run(
            [
                self.modprobe,
                "--config", str(self.config),
                "--dirname", str(self.root),
                "--set-version", "utah-pm-consumer-test",
                "--show-depends", "nvidia", *load_options,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertFalse(self.sentinel.exists(), "modprobe executed an install command")
        fields = shlex.split(result.stdout)
        self.assertEqual(fields[:2], ["install", str(self.trap)], result.stdout)
        return fields[2:]

    def test_selects_coarse_grained_runtime_pm(self):
        options = self.options()
        # Compare the parameter value, not its spelling or comment wording.
        self.assertEqual([opt.partition("=")[0] for opt in options], [PARAMETER])
        self.assertEqual(int(options[0].partition("=")[2], 0), 1)

    def test_package_duplicate_precedes_utah_policy(self):
        (self.config / "nvidia.conf").write_text(f"options nvidia {PARAMETER}=0x02\n")
        values = [int(opt.partition("=")[2], 0) for opt in self.options()]
        # kmod accumulates duplicates; scalar parameters are applied in order.
        self.assertEqual(values, [2, 1])

    def test_preserves_common_suspend_parameters(self):
        (self.config / "zz-nvidia-suspend.conf").write_text(
            "options nvidia NVreg_UseKernelSuspendNotifiers=1 NVreg_TemporaryFilePath=/var/tmp\n"
        )
        options = self.options()
        self.assertEqual(
            [opt.partition("=")[0] for opt in options],
            [PARAMETER, "NVreg_UseKernelSuspendNotifiers", "NVreg_TemporaryFilePath"],
        )
        parameters = dict(opt.split("=", 1) for opt in options)
        self.assertEqual(int(parameters.pop(PARAMETER), 0), 1)
        self.assertEqual(parameters, {
            "NVreg_UseKernelSuspendNotifiers": "1",
            "NVreg_TemporaryFilePath": "/var/tmp",
        })

    def test_explicit_load_option_remains_last(self):
        options = self.options(f"{PARAMETER}=0x00")
        self.assertEqual([int(opt.partition("=")[2], 0) for opt in options], [1, 0])


if __name__ == "__main__":
    unittest.main()

