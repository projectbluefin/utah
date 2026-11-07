"""luks-unlock.py decides whether an encrypted install actually booted.

The LUKS e2e (iso/scripts/luks-e2e.sh, lifecycle-e2e.sh) hands the passphrase
typing and the boot verdict to iso/scripts/luks-unlock.py. CI only ever shows
it the happy path: a stable framebuffer prompt followed by an sshd banner.
Every failure verdict -- emergency shell, a prompt that never appears, a boot
that never finishes -- and the libvirt mode are exercised nowhere else, so a
regression there would turn a broken encrypted boot into a green check.

These tests drive the two state machines with a simulated clock and scripted
monitor, serial and SSH responses. No VM, socket path or sleep is real.
"""
import contextlib
import importlib.util
import io
import socket
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("luks_unlock", ROOT / "iso/scripts/luks-unlock.py")
luks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(luks)

PASSPHRASE = "utah-e2e"


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.start = self.now
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    @property
    def elapsed(self):
        return self.now - self.start


def scripted(values):
    """Return each value in turn, then keep repeating the last one."""
    values = list(values)

    def next_value(*_args, **_kwargs):
        return values.pop(0) if len(values) > 1 else values[0]
    return next_value


class Run:
    """Outcome of one simulated luks-unlock run."""

    def __init__(self, code, sent, dumps_at_send, out, err, clock, copy2):
        self.code = code
        self.sent = sent
        self.dumps_at_send = dumps_at_send
        self.out = out
        self.err = err
        self.clock = clock
        self.copy2 = copy2


class StateMachineCase(unittest.TestCase):
    def run_mode(self, target, patches, sent, dumps, copy2_error=None):
        clock = Clock()
        out, err = io.StringIO(), io.StringIO()
        copy2 = mock.MagicMock(side_effect=copy2_error)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(luks, "time", types.SimpleNamespace(time=clock.time, sleep=clock.sleep)))
            stack.enter_context(mock.patch("shutil.copy2", copy2))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(luks, name, value))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            with self.assertRaises(SystemExit) as raised:
                target()
        return Run(raised.exception.code, sent, dumps, out.getvalue(), err.getvalue(), clock, copy2)

    def run_qemu(self, before, after=((2.5, "after"),), serial_before="", serial_after="",
                 ssh=False, ssh_port=0, copy2_error=None):
        """Simulate run_qemu.

        before/after -- (brightness, hash) screendumps served before and after
        the passphrase is typed; the last one repeats.
        serial_before/serial_after -- qemu_check_serial's verdict in each phase.
        ssh -- ssh_reachable's answer after the passphrase, or a list of answers.
        """
        sent = []
        dumps_at_send = []
        dump_count = [0]
        frames = {"before": scripted(before), "after": scripted(after)}
        ssh_answer = scripted(ssh if isinstance(ssh, list) else [ssh])

        def screendump(_sock, _path):
            dump_count[0] += 1
            return frames["after" if sent else "before"]()

        def send(_sock, passphrase):
            sent.append(passphrase)
            dumps_at_send.append(dump_count[0])

        patches = {
            "qemu_screendump": screendump,
            "qemu_send_passphrase": send,
            "qemu_check_serial": lambda _log: serial_after if sent else serial_before,
            "ssh_reachable": lambda _port: bool(sent) and ssh_answer(),
        }
        return self.run_mode(
            lambda: luks.run_qemu("monitor.sock", PASSPHRASE, "serial.log", ssh_port=ssh_port),
            patches, sent, dumps_at_send, copy2_error)

    def prompt_then(self, *after, **kwargs):
        """A framebuffer prompt (hash 'prompt'), then the given post-unlock frames."""
        before = [(0.69, "boot"), (0.69, "prompt")]
        return self.run_qemu(before, after or ((2.5, "after"),), **kwargs)


class QemuPromptDetectionTests(StateMachineCase):
    def test_serial_prompt_types_the_passphrase_once(self):
        run = self.run_qemu([(0.0, "black")], serial_before="plymouth", ssh=True, ssh_port=2223)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.sent, [PASSPHRASE])
        self.assertIn(luks.PLYMOUTH_WAIT, run.clock.slept)

    def test_framebuffer_prompt_is_typed_on_the_third_identical_frame(self):
        # Unreadable, then a changing frame, then the same frame three times.
        before = [(-1, ""), (0.69, "ovmf"), (0.69, "prompt"), (0.69, "prompt"), (0.69, "prompt")]
        run = self.run_qemu(before, ssh=True, ssh_port=2223)
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.dumps_at_send, [5])

    def test_a_static_black_screen_is_never_taken_for_the_prompt(self):
        # QEMU's framebuffer is all zeros before OVMF draws; it never changes,
        # but typing into it would be typing into nothing.
        run = self.run_qemu([(0.0, "black")])
        self.assertEqual(run.code, 1)
        self.assertEqual(run.sent, [])
        self.assertIn("never detected", run.err)
        self.assertGreaterEqual(run.clock.elapsed, luks.PROMPT_DEADLINE)

    def test_an_unreadable_monitor_never_types_and_fails(self):
        run = self.run_qemu([(-1, "")])
        self.assertEqual(run.code, 1)
        self.assertEqual(run.sent, [])

    def test_a_screen_that_keeps_changing_never_types(self):
        frames = [(0.69, f"frame-{i}") for i in range(luks.PROMPT_DEADLINE)]
        run = self.run_qemu(frames)
        self.assertEqual(run.code, 1)
        self.assertEqual(run.sent, [])


class QemuBootVerdictTests(StateMachineCase):
    def test_sshd_banner_is_success(self):
        run = self.prompt_then((0.3, "plymouth-splash"), ssh=True, ssh_port=2223)
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("sshd banner on port 2223", run.out)

    def test_emergency_shell_on_serial_fails_even_if_sshd_answers(self):
        run = self.prompt_then(serial_after="emergency", ssh=True, ssh_port=2223)
        self.assertEqual(run.code, 2)
        self.assertIn("emergency shell", run.out)

    def test_ssh_port_keeps_waiting_through_a_dark_screen_until_the_deadline(self):
        # With an SSH port the dark-screen shortcut must not declare success:
        # only the banner can.
        run = self.prompt_then((0.69, "prompt"), ssh=False, ssh_port=2223)
        self.assertEqual(run.code, 2)
        self.assertIn("did not complete within timeout", run.err)
        self.assertGreaterEqual(run.clock.elapsed, luks.BOOT_DEADLINE)

    def test_ssh_port_ignores_a_dark_stable_splash(self):
        # Plymouth's post-unlock splash is dark and can repeat between polls;
        # with an SSH port that must not be read as an emergency shell.
        run = self.prompt_then((0.9, "splash"), (0.9, "splash"), (0.9, "splash"),
                               ssh=[False, False, False, False, True], ssh_port=2223)
        self.assertEqual(run.code, 0, run.err)
        self.assertNotIn("emergency", run.out)

    def test_without_ssh_port_an_unchanged_screen_passes_after_the_override(self):
        run = self.prompt_then((0.69, "prompt"))
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("Dark-screen override", run.out)
        self.assertGreater(run.clock.elapsed, luks.DARK_SCREEN_OVERRIDE_S)
        self.assertLess(run.clock.elapsed, luks.BOOT_DEADLINE)

    def test_without_ssh_port_a_bright_settled_screen_is_success(self):
        run = self.prompt_then((2.5, "gdm"), (2.5, "gdm"))
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("framebuffer stable", run.out)

    def test_without_ssh_port_a_dark_settled_screen_is_an_emergency(self):
        run = self.prompt_then((1.0, "shell"), (1.0, "shell"))
        self.assertEqual(run.code, 2)
        self.assertIn("emergency shell suspected", run.out)

    def test_the_brightness_threshold_is_exclusive(self):
        run = self.prompt_then((1.8, "edge"), (1.8, "edge"))
        self.assertEqual(run.code, 2)

    def test_gnome_initial_setup_on_serial_is_success_without_the_gdm_grace(self):
        run = self.prompt_then(serial_after="gnome-initial-setup")
        self.assertEqual(run.code, 0, run.err)
        self.assertNotIn(30, run.clock.slept)

    def test_gdm_on_serial_is_success_after_the_grace_wait(self):
        run = self.prompt_then(serial_after="gdm")
        self.assertEqual(run.code, 0, run.err)
        self.assertIn(30, run.clock.slept)

    def test_a_failed_diagnostic_copy_does_not_change_the_verdict(self):
        for kwargs in ({"serial_after": "gdm"}, {"ssh": True, "ssh_port": 2223}):
            with self.subTest(**kwargs):
                run = self.prompt_then(copy2_error=OSError("disk full"), **kwargs)
                self.assertEqual(run.code, 0, run.err)
                self.assertTrue(run.copy2.called)


class LibvirtTests(StateMachineCase):
    def run_libvirt(self, sizes, leases=("",)):
        sent = []
        patches = {
            "virsh_screenshot_size": scripted(sizes),
            "virsh_send_passphrase": lambda _vm, passphrase: sent.append(passphrase),
            "virsh_dhcp_ip": scripted(leases),
        }
        return self.run_mode(lambda: luks.run_libvirt("utah", PASSPHRASE, "52:54:00:aa:bb:cc"),
                             patches, sent, [])

    def test_blank_after_content_types_the_passphrase_and_a_lease_is_success(self):
        run = self.run_libvirt([100, 50000, 50000, 2000], leases=["", "", "192.168.122.40"])
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.sent, [PASSPHRASE])
        self.assertIn("192.168.122.40", run.out)

    def test_a_blank_screen_before_any_content_is_not_the_prompt(self):
        run = self.run_libvirt([100])
        self.assertEqual(run.code, 1)
        self.assertEqual(run.sent, [])
        self.assertIn("never detected", run.err)

    def test_no_dhcp_lease_by_the_deadline_is_a_failed_boot(self):
        run = self.run_libvirt([50000, 2000])
        self.assertEqual(run.code, 2)
        self.assertIn("no DHCP lease", run.err)
        self.assertGreaterEqual(run.clock.elapsed, luks.BOOT_DEADLINE)

    def test_dhcp_lease_lookup_matches_the_mac_case_insensitively(self):
        leases = (
            " Expiry Time   MAC address   Protocol  IP address   Hostname\n"
            " 2026-11-07 10:00:00  52:54:00:00:00:01  ipv4  192.168.122.9/24  other\n"
            " 2026-11-07 10:00:00  52:54:00:AA:BB:CC  ipv4  192.168.122.40/24  utah\n"
        )
        result = types.SimpleNamespace(stdout=leases, returncode=0)
        with mock.patch.object(luks.subprocess, "run", return_value=result):
            self.assertEqual(luks.virsh_dhcp_ip("52:54:00:aa:bb:cc"), "192.168.122.40")
            self.assertEqual(luks.virsh_dhcp_ip("52:54:00:ff:ff:ff"), "")


class VirshHelperTests(unittest.TestCase):
    def test_screenshot_size_is_zero_when_virsh_fails_or_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.png"
            failed = types.SimpleNamespace(returncode=1)
            ok = types.SimpleNamespace(returncode=0)
            with mock.patch.object(luks.subprocess, "run", return_value=failed):
                self.assertEqual(luks.virsh_screenshot_size("utah", str(path)), 0)
            with mock.patch.object(luks.subprocess, "run", return_value=ok):
                self.assertEqual(luks.virsh_screenshot_size("utah", str(path)), 0)
                path.write_bytes(b"x" * 5000)
                self.assertEqual(luks.virsh_screenshot_size("utah", str(path)), 5000)

    def test_passphrase_keys_map_to_linux_keycodes_and_end_with_enter(self):
        with mock.patch.object(luks.subprocess, "run") as run, \
                mock.patch.object(luks.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            luks.virsh_send_passphrase("utah", "a_-1 ?")
        keys = [call.args[0][-1] for call in run.call_args_list]
        self.assertEqual(keys, ["KEY_A", "KEY_MINUS", "KEY_MINUS", "KEY_1", "KEY_SPACE", "KEY_ENTER"])
        self.assertIn("no key mapping for '?'", err.getvalue())


class SerialMarkerTests(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "serial.log"
            log.write_text(text)
            return luks.qemu_check_serial(str(log))

    def test_markers(self):
        cases = {
            "Please enter passphrase for disk luks-1234:": "plymouth",
            "[  OK  ] Started gdm.service - GNOME Display Manager.": "gdm",
            "[  OK  ] Started \x1b[0;1;39mgdm.service\x1b[0m\n - GNOME Display Manager.": "gdm",
            "[  OK  ] Started gnome-initial-setup-first-login.service": "gnome-initial-setup",
            "You are in emergency mode. After logging in, type ...": "emergency",
            "Started gdm.service\nGive root password for maintenance\nemergency shell": "emergency",
            "[  OK  ] Reached target basic.target": "",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.check(text), expected)

    def test_a_missing_log_has_no_marker(self):
        self.assertEqual(luks.qemu_check_serial("/nonexistent/serial.log"), "")

    def test_undecodable_bytes_do_not_hide_a_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "serial.log"
            log.write_bytes(b"\xff\xfe garbage\nYou are in emergency mode.\n")
            self.assertEqual(luks.qemu_check_serial(str(log)), "emergency")


class MonitorHelperTests(unittest.TestCase):
    def screendump(self, content, error=None):
        commands = []

        def hmp(_sock, command, **_kwargs):
            commands.append(command)
            if error:
                raise error
            if content is not None:
                Path(command.split(" ", 1)[1]).write_bytes(content)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(luks, "run_hmp_command", hmp), \
                mock.patch.object(luks.time, "sleep"):
            path = str(Path(tmp) / "snap.ppm")
            return luks.qemu_screendump("monitor.sock", path), commands, path

    def test_screendump_reports_mean_sampled_brightness_and_hash(self):
        (brightness, md5), commands, path = self.screendump(b"P6\n10 10\n255\n" + bytes([40]) * 300)
        self.assertEqual(commands, [f"screendump {path}"])
        self.assertEqual(brightness, 40.0)
        self.assertEqual(len(md5), 32)

    def test_screendump_failures_are_unreadable_not_dark(self):
        for name, content, error in (
            ("monitor refused", None, OSError("connection refused")),
            ("file never written", None, None),
            ("no PPM header", b"garbage", None),
            ("no pixels", b"P6\n0 0\n255\n", None),
        ):
            with self.subTest(name):
                self.assertEqual(self.screendump(content, error)[0], (-1, ""))

    def test_passphrase_keys_map_to_hmp_sendkey_and_end_with_return(self):
        commands = []
        with mock.patch.object(luks, "run_hmp_command", lambda _s, c, **_k: commands.append(c)), \
                mock.patch.object(luks.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            luks.qemu_send_passphrase("monitor.sock", "a_b-1 ?")
        self.assertEqual(commands, [
            "sendkey a", "sendkey shift-minus", "sendkey b", "sendkey minus",
            "sendkey 1", "sendkey spc", "sendkey ret",
        ])
        self.assertIn("no key mapping for '?'", err.getvalue())


class SshReachableTests(unittest.TestCase):
    def serve(self, banner):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def answer():
            conn, _ = server.accept()
            with conn:
                conn.sendall(banner)

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return server.getsockname()[1]

    def test_an_sshd_banner_is_reachable(self):
        self.assertTrue(luks.ssh_reachable(self.serve(b"SSH-2.0-OpenSSH_9.9\r\n")))

    def test_another_service_on_the_port_is_not_sshd(self):
        self.assertFalse(luks.ssh_reachable(self.serve(b"HTTP/1.1 400 Bad Request\r\n")))

    def test_a_closed_port_is_not_reachable(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.assertFalse(luks.ssh_reachable(port, timeout=1))


class EntryPointTests(unittest.TestCase):
    def main(self, *argv):
        with mock.patch.object(sys, "argv", ["luks-unlock.py", *argv]), \
                contextlib.redirect_stderr(io.StringIO()):
            luks.main()

    def test_bad_invocations_exit_1(self):
        for argv in ((), ("qemu", "monitor.sock", PASSPHRASE), ("libvirt", "utah"), ("wait-live",)):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                self.main(*argv)
            self.assertEqual(raised.exception.code, 1)

    def test_qemu_mode_passes_the_optional_ssh_port_as_an_int(self):
        with mock.patch.object(luks, "run_qemu") as run_qemu:
            self.main("qemu", "monitor.sock", PASSPHRASE, "serial.log", "2223")
            self.main("qemu", "monitor.sock", PASSPHRASE, "serial.log")
        self.assertEqual(run_qemu.call_args_list, [
            mock.call("monitor.sock", PASSPHRASE, "serial.log", ssh_port=2223),
            mock.call("monitor.sock", PASSPHRASE, "serial.log", ssh_port=0),
        ])

    def test_libvirt_mode_dispatches(self):
        with mock.patch.object(luks, "run_libvirt") as run_libvirt:
            self.main("libvirt", "utah", PASSPHRASE, "52:54:00:aa:bb:cc")
        run_libvirt.assert_called_once_with("utah", PASSPHRASE, "52:54:00:aa:bb:cc")


if __name__ == "__main__":
    unittest.main()
