#!/usr/bin/env python3
"""
Automate LUKS passphrase entry for a VM booting with Plymouth.

Adapted from projectbluefin/iso (src/luks-unlock.py, Apache-2.0) for Utah's
encrypted offline installer validation pipeline. Uses only Python standard
library modules so no external utilities (e.g. socat) are required.

Plymouth renders the passphrase prompt on the EFI framebuffer and serial console.
This tool detects the prompt via serial output or screendump hash stabilization,
injects the passphrase keystrokes via QEMU HMP sendkey, and monitors boot
progression to graphical session.

Usage:
  qemu mode:     luks-unlock.py qemu    <monitor-sock> <passphrase> <serial-log> [ssh-port]
  libvirt mode:  luks-unlock.py libvirt <vm-name> <passphrase> <mac-address>

Exit codes:
  0 — passphrase sent and boot succeeded (display re-stabilised after unlock or serial confirmed)
  1 — error (timed out, passphrase prompt never appeared, etc.)
  2 — passphrase sent but boot resulted in emergency shell
"""

import hashlib
import os
import re
import socket
import subprocess
import sys
import time

POLL_INTERVAL = 3
PLYMOUTH_WAIT = 10
PROMPT_DEADLINE = 300
BOOT_DEADLINE = 900
DARK_SCREEN_OVERRIDE_S = 90


def monitor_command(sock_path: str, command: str, settle: float = 0.5) -> str:
    """Send one HMP command to a QEMU monitor socket and return the response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(sock_path)
        client.sendall(command.encode() + b"\n")
        time.sleep(settle)
        chunks = []
        client.settimeout(1)
        try:
            while True:
                data = client.recv(4096)
                if not data:
                    break
                chunks.append(data)
        except (socket.timeout, TimeoutError):
            pass
    return b"".join(chunks).decode(errors="replace")


def qemu_screendump(sock: str, path: str) -> tuple:
    """Return (brightness, md5_hash) for the screendump, or (-1, '') on error."""
    try:
        monitor_command(sock, f"screendump {path}")
    except OSError:
        return -1, ""
    time.sleep(0.5)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return -1, ""
    md5 = hashlib.md5(data).hexdigest()
    try:
        header_end = data.index(b"255\n") + 4
    except ValueError:
        return -1, ""
    pixel_data = data[header_end:]
    if not pixel_data:
        return -1, ""
    sampled = pixel_data[::100]
    return sum(sampled) / len(sampled), md5


def qemu_send_passphrase(sock: str, passphrase: str):
    """Send passphrase keystrokes followed by Enter over QEMU monitor."""
    key_map = {c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"}
    key_map["-"] = "minus"
    key_map["_"] = "shift-minus"
    key_map[" "] = "spc"

    def _sendkey(key: str):
        monitor_command(sock, f"sendkey {key}", settle=0.05)

    for ch in passphrase:
        key = key_map.get(ch)
        if key is None:
            print(f"[luks-unlock] WARNING: no key mapping for {ch!r}", file=sys.stderr)
            continue
        _sendkey(key)
        time.sleep(0.1)
    _sendkey("ret")


def qemu_check_serial(serial_log: str) -> str:
    """Return 'plymouth', 'gdm', 'graphical-ok', 'emergency', or ''."""
    try:
        with open(serial_log, errors="replace") as f:
            raw = f.read()
    except OSError:
        return ""
    content = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)
    content_flat = " ".join(content.split())

    if "emergency mode" in content or "emergency shell" in content:
        return "emergency"
    if "UTAH_INSTALLED_GRAPHICAL_OK" in raw or "UTAH_INSTALLED_ENCRYPTED_OK" in raw:
        return "graphical-ok"
    if "Started gnome-initial-setup" in content_flat:
        return "gnome-initial-setup"
    if "Started gdm.service" in content_flat or "Started GNOME Display Manager" in content_flat:
        return "gdm"
    if "Reached target Graphical Interface" in content_flat or "Reached target graphical.target" in content_flat:
        return "graphical-target"
    if "Please enter passphrase for disk" in raw or "Passphrase:" in raw:
        return "plymouth"
    return ""


def ssh_reachable(port: int, timeout: int = 5) -> bool:
    """Return True if sshd on localhost:port responds with an SSH banner."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            banner = s.recv(64)
            return banner.startswith(b"SSH-")
    except OSError:
        return False


def run_qemu(monitor_sock: str, passphrase: str, serial_log: str, ssh_port: int = 0):
    snap = "/tmp/luks-unlock-snap.ppm"
    CONTENT_THRESHOLD = 0.5
    STABLE_POLLS = 2

    print(f"[luks-unlock] qemu mode — watching monitor {monitor_sock}...", flush=True)
    deadline = time.time() + PROMPT_DEADLINE

    had_content = False
    stable_count = 0
    prev_hash = ""

    while time.time() < deadline:
        serial_result = qemu_check_serial(serial_log)
        if serial_result == "plymouth":
            print("[luks-unlock] Plymouth passphrase prompt detected via serial", flush=True)
            brightness, md5 = qemu_screendump(monitor_sock, snap)
            prev_hash = md5
            print(f"[luks-unlock] Waiting {PLYMOUTH_WAIT}s for Plymouth to settle...", flush=True)
            time.sleep(PLYMOUTH_WAIT)
            print("[luks-unlock] Sending passphrase via QEMU monitor sendkey...", flush=True)
            qemu_send_passphrase(monitor_sock, passphrase)
            print("[luks-unlock] Passphrase sent — watching for boot...", flush=True)
            break

        brightness, md5 = qemu_screendump(monitor_sock, snap)
        print(f"[luks-unlock] screendump brightness={brightness:.2f} hash={md5[:8]}", flush=True)

        if brightness < 0:
            stable_count = 0
            time.sleep(POLL_INTERVAL)
            continue

        if not had_content and brightness > CONTENT_THRESHOLD:
            had_content = True
            print(f"[luks-unlock] VM is rendering (brightness {brightness:.2f})", flush=True)

        if had_content:
            if md5 == prev_hash:
                stable_count += 1
            else:
                stable_count = 0
            prev_hash = md5

        if had_content and stable_count >= STABLE_POLLS:
            print(
                f"[luks-unlock] Plymouth prompt stable"
                f" (brightness={brightness:.2f}, {stable_count} identical polls)",
                flush=True,
            )
            print(f"[luks-unlock] Waiting {PLYMOUTH_WAIT}s for Plymouth to settle...", flush=True)
            time.sleep(PLYMOUTH_WAIT)
            print("[luks-unlock] Sending passphrase via QEMU monitor sendkey...", flush=True)
            qemu_send_passphrase(monitor_sock, passphrase)
            print("[luks-unlock] Passphrase sent — watching for boot...", flush=True)
            break

        time.sleep(POLL_INTERVAL)
    else:
        print("[luks-unlock] ERROR: Plymouth takeover never detected", file=sys.stderr)
        sys.exit(1)

    deadline = time.time() + BOOT_DEADLINE
    passphrase_time = time.time()
    passphrase_hash = prev_hash
    screen_changed = False
    gnome_stable_count = 0

    while time.time() < deadline:
        result = qemu_check_serial(serial_log)
        if result == "emergency":
            print("[luks-unlock] RESULT: emergency shell reached", flush=True)
            sys.exit(2)
        if result in ("graphical-ok", "graphical-target", "gnome-initial-setup", "gdm"):
            print(f"[luks-unlock] RESULT: boot succeeded ({result} confirmed via serial)", flush=True)
            sys.exit(0)

        if ssh_port and ssh_reachable(ssh_port):
            print(f"[luks-unlock] RESULT: boot succeeded (sshd banner on port {ssh_port})", flush=True)
            sys.exit(0)

        elapsed = time.time() - passphrase_time
        if not screen_changed and elapsed > DARK_SCREEN_OVERRIDE_S:
            if not ssh_port:
                print(
                    f"[luks-unlock] Dark-screen override: screen unchanged for "
                    f"{int(elapsed)}s and no SSH port configured — assuming boot proceeded",
                    flush=True,
                )
                sys.exit(0)

        brightness, md5 = qemu_screendump(monitor_sock, snap)
        if md5 != passphrase_hash and not screen_changed:
            screen_changed = True
            print("[luks-unlock] Screen changed after passphrase — LUKS unlocked, boot proceeding", flush=True)

        GNOME_THRESHOLD = 1.8
        if md5 == prev_hash:
            gnome_stable_count += 1
        else:
            gnome_stable_count = 0
        prev_hash = md5

        if screen_changed and gnome_stable_count >= 1:
            if brightness > GNOME_THRESHOLD:
                print(
                    f"[luks-unlock] RESULT: boot succeeded (framebuffer stable, brightness={brightness:.2f})",
                    flush=True,
                )
                sys.exit(0)

        time.sleep(POLL_INTERVAL)

    print("[luks-unlock] ERROR: boot timed out after passphrase", file=sys.stderr)
    sys.exit(1)


def virsh_screenshot_size(vm: str, path: str) -> int:
    r = subprocess.run(["virsh", "screenshot", vm, path], capture_output=True)
    if r.returncode != 0:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def virsh_send_passphrase(vm: str, passphrase: str):
    key_map = {c: f"KEY_{c.upper()}" for c in "abcdefghijklmnopqrstuvwxyz"}
    key_map.update({str(i): f"KEY_{i}" for i in range(10)})
    key_map["-"] = "KEY_MINUS"
    key_map["_"] = "KEY_MINUS"
    key_map[" "] = "KEY_SPACE"

    for ch in passphrase:
        key = key_map.get(ch)
        if key is None:
            continue
        subprocess.run(["virsh", "send-key", vm, "--codeset", "linux", key], capture_output=True)
        time.sleep(0.08)
    subprocess.run(["virsh", "send-key", vm, "--codeset", "linux", "KEY_ENTER"], capture_output=True)


def virsh_dhcp_ip(mac: str) -> str:
    r = subprocess.run(["virsh", "net-dhcp-leases", "default"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if mac.lower() in line.lower():
            for part in line.split():
                if "/" in part and "." in part:
                    return part.split("/")[0]
    return ""


def run_libvirt(vm: str, passphrase: str, mac: str):
    snap = "/tmp/luks-unlock-snap.png"
    seen_content = False

    print(f"[luks-unlock] libvirt mode — watching {vm} for Plymouth takeover...", flush=True)
    deadline = time.time() + PROMPT_DEADLINE

    while time.time() < deadline:
        size = virsh_screenshot_size(vm, snap)
        if not seen_content and size > 4096:
            seen_content = True
            print("[luks-unlock] Boot content visible (OVMF/bootloader)", flush=True)
        if seen_content and size <= 4096:
            print(f"[luks-unlock] Plymouth has display — waiting {PLYMOUTH_WAIT}s...", flush=True)
            time.sleep(PLYMOUTH_WAIT)
            virsh_send_passphrase(vm, passphrase)
            print("[luks-unlock] Passphrase sent — waiting for boot...", flush=True)
            break
        time.sleep(POLL_INTERVAL)
    else:
        print("[luks-unlock] ERROR: Plymouth takeover never detected", file=sys.stderr)
        sys.exit(1)

    deadline = time.time() + BOOT_DEADLINE
    while time.time() < deadline:
        ip = virsh_dhcp_ip(mac)
        if ip:
            print(f"[luks-unlock] RESULT: boot succeeded — guest IP {ip}", flush=True)
            sys.exit(0)
        time.sleep(5)

    print("[luks-unlock] WARNING: passphrase sent but no DHCP lease within timeout", file=sys.stderr)
    sys.exit(2)


def main():
    if len(sys.argv) < 2:
        print("Usage: luks-unlock.py [qemu|libvirt] ...", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "qemu":
        if len(sys.argv) < 5:
            print("Usage: luks-unlock.py qemu <monitor-sock> <passphrase> <serial-log> [ssh-port]", file=sys.stderr)
            sys.exit(1)
        monitor_sock = sys.argv[2]
        passphrase = sys.argv[3]
        serial_log = sys.argv[4]
        ssh_port = int(sys.argv[5]) if len(sys.argv) > 5 else 0
        run_qemu(monitor_sock, passphrase, serial_log, ssh_port)
    elif mode == "libvirt":
        if len(sys.argv) < 5:
            print("Usage: luks-unlock.py libvirt <vm-name> <passphrase> <mac-address>", file=sys.stderr)
            sys.exit(1)
        run_libvirt(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
