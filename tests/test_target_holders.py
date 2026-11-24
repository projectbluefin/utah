"""What still holds the installer's target after a failed install (#1467).

fisherman's finalize could not remount the installed root read-only and then
could not close its LUKS mapping, and the live VM was stopped before anyone
looked at what held the filesystem. luks-e2e.sh now runs
iso/scripts/target-holders.sh in the live guest first. By then the
installer's teardown has lazily unmounted the target, so the report has to
find holders that are in no host mount table: a process with a file open on
the detached filesystem, and a mount namespace that still has it mounted.
These run the real script against real holders; tmpfs stands in for btrfs.
"""
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "iso/scripts/target-holders.sh"


def report(fstype):
    return subprocess.run(["bash", str(SCRIPT), f"@{int(time.time())}", fstype],
                          capture_output=True, text=True, timeout=120)


class TargetHoldersTests(unittest.TestCase):
    def test_a_deleted_file_held_open_for_writing_is_reported(self):
        # An open writer, or an open file already deleted, is what makes
        # `mount -o remount,ro` fail with "mount point is busy".
        with tempfile.TemporaryDirectory() as tmp:
            fstype = subprocess.run(["stat", "-f", "-c", "%T", tmp], capture_output=True,
                                    text=True, check=True).stdout.strip()
            held = Path(tmp) / "held"
            child = subprocess.Popen(
                ["bash", "-c", 'exec 3>"$1"; rm "$1"; echo ready; exec sleep 60', "holder", str(held)],
                stdout=subprocess.PIPE, text=True)
            self.addCleanup(child.stdout.close)
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)
            self.assertEqual(child.stdout.readline().strip(), "ready")
            result = report(fstype)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"=== processes with a file, working directory, root or mapping on {fstype} ===",
                      result.stdout)
        self.assertRegex(result.stdout,
                         rf"pid {child.pid} \(sleep\) .*: fd/3 \(write\) -> {re.escape(str(held))} \(deleted\)")

    def test_a_lazily_detached_target_is_still_traced(self):
        unshare = shutil.which("unshare")
        if not unshare or subprocess.run(
                [unshare, "-rmpf", "--mount-proc", "--propagation", "private",
                 "sh", "-c", "mount -t tmpfs probe /mnt"],
                capture_output=True).returncode != 0:
            self.skipTest("unprivileged mount and PID namespaces are unavailable")
        # In a private user, mount and PID namespace: mount the stand-in
        # target, start a "service" in a mount namespace of its own with its
        # working directory on it, and a writer holding a file on it that is
        # then deleted; then detach the target lazily, as the installer's
        # teardown does, and run the report.
        scenario = r'''
            target="$1" script="$2"
            mount -t tmpfs fake-target "${target}"
            unshare -m --propagation private sh -c 'cd "$1" && exec sleep 60' service "${target}" &
            service=$!
            sh -c 'exec 3>"$1/held"; exec sleep 60' writer "${target}" &
            writer=$!
            until [ "$(readlink "/proc/${service}/cwd")" = "${target}" ] && [ -e "${target}/held" ]; do
                sleep 0.05
            done
            rm "${target}/held"
            umount -l "${target}"
            echo "service=${service} writer=${writer}"
            bash "${script}" "@$(date +%s)" tmpfs
            kill "${service}" "${writer}"
        '''
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [unshare, "-rmpf", "--mount-proc", "--propagation", "private",
                 "bash", "-c", scenario, "scenario", tmp, str(SCRIPT)],
                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        first, out = result.stdout.split("\n", 1)
        pids = dict(item.split("=") for item in first.split())
        namespaces = out.split("=== mount namespaces that still mount tmpfs ===")[1].split("\n=== ")[0]
        # The service's namespace still mounts the target after the lazy
        # unmount, and the report names the process that keeps it alive.
        self.assertRegex(namespaces, rf"mnt:\[\d+\]: 1 process\(es\)\n  pid {pids['service']} \(sleep\) ")
        self.assertIn(f" {tmp} rw,", namespaces)
        holders = out.split("=== processes with a file, working directory, root or mapping on tmpfs ===")[1]
        self.assertRegex(holders, rf"pid {pids['service']} \(sleep\) .*: cwd -> {re.escape(tmp)}\n")
        # The writer's file is on the detached mount, in no mount table.
        self.assertRegex(holders, rf"pid {pids['writer']} \(sleep\) .*: fd/3 \(write\) -> /held \(deleted\)")


if __name__ == "__main__":
    unittest.main()
