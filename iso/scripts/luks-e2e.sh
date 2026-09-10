#!/usr/bin/env bash
# Install Utah from its live ISO onto an encrypted disk, then log in to it.
#
# Six phases, each of which can fail the test on its own:
#   1. boot the live ISO in QEMU with one blank disk attached
#   2. prove the live session reached a graphical desktop, not just a shell
#   3. run the installer with a LUKS passphrase and a user account
#   4. boot the installed disk with no ISO, so it must come up on its own
#   5. answer Plymouth's passphrase prompt
#   6. log in at the GDM greeter and prove a GNOME session is running
#
# Phases 2 and 6 are the point. An image can install perfectly, unlock
# perfectly, and still be useless if the desktop never starts -- and a boot
# that stops at a text console looks identical to a working one if all you
# check is that the machine came up.
#
# Every phase writes a PNG screenshot under the work directory, so a failure
# can be looked at rather than guessed at.
#
# The live ISO must be a debug build (`just iso testing 1`): phases 2 and 3
# drive the live environment over SSH, which only a debug ISO enables.
set -euo pipefail

ISO="${1:?live ISO path is required}"
PAYLOAD_IMAGE="${2:?installed image reference is required}"
PASSPHRASE="${3:-testpassphrase}"
WORK="${UTAH_E2E_WORK:-/var/tmp/utah-luks-e2e}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# The account the installer creates, and the one phase 6 logs in as.
TEST_USER="${UTAH_E2E_USER:-utahtest}"
TEST_PASSWORD="${UTAH_E2E_PASSWORD:-utahtest}"
# The terminal the last screenshot opens. --system matters: flatpak otherwise
# consults a user installation that does not exist and aborts instead of
# falling back.
TERMINAL_APP="${UTAH_E2E_TERMINAL:-com.mitchellh.ghostty}"

ISO="$(realpath "${ISO}")"
mkdir -p "${WORK}"
SHOTS="${WORK}/screenshots"
mkdir -p "${SHOTS}"
INSTALL_DISK="${WORK}/install.qcow2"
MONITOR_LIVE="${WORK}/live-monitor.sock"
MONITOR_INSTALLED="${WORK}/installed-monitor.sock"
SERIAL_LIVE="${WORK}/live-serial.log"
SERIAL_INSTALLED="${WORK}/installed-serial.log"
SSH_PORT="${UTAH_E2E_SSH_PORT:-2222}"
SSH_PORT_INSTALLED=$((SSH_PORT + 1))
# Both VMs keep a VNC display open so a run can be watched rather than waited
# on. Screendumps come from the monitor either way, so this costs nothing and
# turns "it hung somewhere in phase 5" into something you can just look at.
VNC_LIVE="${UTAH_E2E_VNC_LIVE:-2}"
VNC_INSTALLED="${UTAH_E2E_VNC_INSTALLED:-3}"
VARS="${WORK}/ovmf-vars.fd"

QEMU="$(command -v qemu-system-x86_64 /usr/libexec/qemu-kvm 2>/dev/null | head -1)"
[[ -n "${QEMU}" ]] || { echo "qemu-system-x86_64 not found" >&2; exit 1; }

OVMF_CODE=""
for f in /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/OVMF/OVMF_CODE_4M.fd \
         /usr/share/OVMF/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd \
         /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd; do
    [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
done
[[ -n "${OVMF_CODE}" ]] || { echo "OVMF firmware not found" >&2; exit 1; }
OVMF_VARS_SRC=""
for f in /usr/share/edk2/ovmf/OVMF_VARS.fd /usr/share/OVMF/OVMF_VARS_4M.fd \
         /usr/share/OVMF/OVMF_VARS.fd \
         /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
    [[ -f "$f" ]] && { OVMF_VARS_SRC="$f"; break; }
done
[[ -n "${OVMF_VARS_SRC}" ]] || { echo "OVMF variable store not found" >&2; exit 1; }

ACCEL="-accel kvm"
test -r /dev/kvm || { echo "No /dev/kvm; falling back to TCG (much slower)"; ACCEL="-accel tcg,thread=multi"; }

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o ConnectTimeout=5
          -o PreferredAuthentications=password
          -o ServerAliveInterval=30 -o ServerAliveCountMax=20)
ssh_live() { sshpass -p live ssh "${SSH_OPTS[@]}" -p "${SSH_PORT}" liveuser@127.0.0.1 "$@"; }
scp_live() { sshpass -p live scp "${SSH_OPTS[@]}" -P "${SSH_PORT}" "$@"; }
ssh_target() {
    sshpass -p "${TEST_PASSWORD}" ssh "${SSH_OPTS[@]}" \
        -p "${SSH_PORT_INSTALLED}" "${TEST_USER}@127.0.0.1" "$@"
}

monitor() {
    python3 - "$1" "$2" <<'PY'
import socket, sys, time
sock, command = sys.argv[1], sys.argv[2]
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(10)
    client.connect(sock)
    client.sendall(command.encode() + b"\n")
    time.sleep(0.3)
PY
}

# A screenshot nobody can open is not evidence. QEMU writes PPM, so convert to
# PNG when an encoder is available and keep the PPM otherwise, rather than
# failing a passing test over a missing tool.
shot() {
    local label="$1" sock="$2"
    local ppm="${SHOTS}/${label}.ppm" png="${SHOTS}/${label}.png"
    monitor "${sock}" "screendump ${ppm}" || return 0
    sleep 1
    if command -v ffmpeg >/dev/null 2>&1 && [[ -s "${ppm}" ]]; then
        if ffmpeg -y -loglevel error -i "${ppm}" "${png}" 2>/dev/null; then
            rm -f "${ppm}"
            echo "  screenshot: ${png}"
            return 0
        fi
    fi
    echo "  screenshot: ${ppm}"
}

# Type a string at whatever has focus, then Enter. Same mechanism the LUKS
# unlock uses, because the GDM greeter is equally invisible to the serial port.
send_keys() {
    local sock="$1" text="$2" ch key i
    for (( i=0; i<${#text}; i++ )); do
        ch="${text:i:1}"
        case "${ch}" in
            [a-z0-9]) key="${ch}" ;;
            [A-Z]) key="shift-$(printf '%s' "${ch}" | tr '[:upper:]' '[:lower:]')" ;;
            "-") key="minus" ;;
            "_") key="shift-minus" ;;
            ".") key="dot" ;;
            " ") key="spc" ;;
            *) echo "  no key mapping for '${ch}', skipping" >&2; continue ;;
        esac
        monitor "${sock}" "sendkey ${key}" || true
        sleep 0.05
    done
    monitor "${sock}" "sendkey ret" || true
}

qemu_pids=()
cleanup() {
    for pid in "${qemu_pids[@]:-}"; do
        [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
    done
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# A failed boot is diagnosed from the units that failed, not from whatever
# happened to scroll past last. The console keeps printing after a unit fails
# -- when root is locked, sulogin exits immediately and the boot carries on --
# so the last 40 lines are usually udev noise rather than the cause. Keep a
# copy per attempt too: each run overwrites the serial log, so every failure
# used to destroy the evidence for the one before it.
diagnose_boot() {
    # Two statements, not one: bash expands every assignment word of a single
    # `local` before it assigns any of them, so `${log%.log}` in the same
    # `local` reads an unset log and trips set -u.
    local log="$1"
    local keep="${log%.log}-$(date +%Y%m%d-%H%M%S).log"
    cp -f "${log}" "${keep}" 2>/dev/null && echo "  serial log kept: ${keep}" >&2
    echo "--- units that failed ---" >&2
    sed 's/\x1b\[[0-9;:]*m//g' "${log}" 2>/dev/null \
        | grep -aE 'Dependency failed|Failed to start|Failed to mount|Timed out waiting|job .* timed out|Started .*emergency' \
        | tail -30 >&2 || echo "  (none recorded)" >&2
    echo "--- last 120 console lines ---" >&2
    tail -120 "${log}" >&2 || true
}


# `just try-installed` boots an overlay backed by the very disk this test is
# about to recreate, out of the same work directory. Leaving it running while
# phase 3 rewrites install.qcow2 corrupts what phase 4 then boots, and the
# result looks like an image bug: the installed system comes up without its
# /boot and drops to an emergency shell. Refuse to start instead.
if [[ -f "${WORK}/try.pid" ]] && kill -0 "$(cat "${WORK}/try.pid" 2>/dev/null)" 2>/dev/null; then
    fail "a VM from 'just try-installed' is running on this work directory (pid $(cat "${WORK}/try.pid")); stop it first: kill \$(cat ${WORK}/try.pid)"
fi

echo "=== Phase 1/6: boot the live ISO ==="
rm -f "${INSTALL_DISK}" "${MONITOR_LIVE}" "${MONITOR_INSTALLED}" \
      "${SERIAL_LIVE}" "${SERIAL_INSTALLED}"
qemu-img create -f qcow2 "${INSTALL_DISK}" 64G >/dev/null
cp -f "${OVMF_VARS_SRC}" "${VARS}"

"${QEMU}" \
    -machine q35 -cpu host -m 8192 -smp 4 ${ACCEL} \
    -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
    -drive "if=pflash,format=raw,file=${VARS}" \
    -drive "if=none,id=iso,file=${ISO},media=cdrom,readonly=on,format=raw" \
    -device virtio-scsi-pci,id=scsi \
    -device scsi-cd,drive=iso \
    -drive "if=none,id=disk,file=${INSTALL_DISK},format=qcow2" \
    -device virtio-blk-pci,drive=disk \
    -netdev "user,id=net0,hostfwd=tcp::${SSH_PORT}-:22" \
    -device virtio-net-pci,netdev=net0 \
    -monitor "unix:${MONITOR_LIVE},server,nowait" \
    -serial "file:${SERIAL_LIVE}" \
    -vnc "127.0.0.1:${VNC_LIVE}" \
    -daemonize -pidfile "${WORK}/live.pid"
qemu_pids+=("$(cat "${WORK}/live.pid")")
echo "  watch the live VM: vnc://127.0.0.1:$((5900 + VNC_LIVE))"

echo "Waiting for the live environment to accept SSH..."
for i in $(seq 1 90); do
    if ssh_live true 2>/dev/null; then echo "Live environment is up."; break; fi
    if [[ "$i" -eq 90 ]]; then
        tail -40 "${SERIAL_LIVE}" >&2 || true
        # Much the commonest cause, and it looks nothing like itself: a
        # non-debug ISO boots perfectly and even starts sshd, but the
        # liveuser password, PasswordAuthentication and root login are all
        # gated behind DEBUG=1 in configure-live.sh, so every login is
        # refused and the symptom is silence.
        if grep -qa "Reached target.*Graphical" "${SERIAL_LIVE}" 2>/dev/null; then
            echo "The live system reached its graphical target, so it booted fine" >&2
            echo "and this is a login failure. Was the ISO built with debug on?" >&2
            echo "  just iso testing 1     (plain 'just iso testing' has no ssh login)" >&2
        fi
        fail "no SSH from the live ISO after 7m30s"
    fi
    sleep 5
done

echo "=== Phase 2/6: prove the live session reached a desktop ==="
# The live ISO autologins liveuser into GNOME. If that silently degraded to a
# text console the installer would still be reachable over SSH and every other
# check here would pass, so assert the session explicitly.
for i in $(seq 1 60); do
    if ssh_live 'systemctl is-active graphical.target' 2>/dev/null | grep -qx active; then break; fi
    [[ "$i" -eq 60 ]] && { shot live-no-desktop "${MONITOR_LIVE}" || true; fail "live session never reached graphical.target"; }
    sleep 5
done
echo "  graphical.target: active"

ssh_live 'systemctl is-active gdm.service' 2>/dev/null | grep -qx active \
    || fail "gdm is not running in the live session"
echo "  gdm.service: active"

for i in $(seq 1 30); do
    if ssh_live 'pgrep -u liveuser -x gnome-shell >/dev/null' 2>/dev/null; then break; fi
    [[ "$i" -eq 30 ]] && { shot live-no-shell "${MONITOR_LIVE}" || true; fail "no gnome-shell running for liveuser"; }
    sleep 5
done
echo "  gnome-shell: running as liveuser"

live_session_type="$(ssh_live "loginctl show-session \$(loginctl show-user liveuser -p Display --value) -p Type --value" 2>/dev/null || true)"
echo "  live session type: ${live_session_type:-unknown}"
shot live-desktop "${MONITOR_LIVE}"

echo "=== Phase 3/6: install onto an encrypted disk ==="
# An empty "image" with a "targetImgref" is the offline shape: fisherman then
# installs from containers-storage rather than pulling, which is the only way
# the ISO's embedded payload gets used. Naming the image directly makes it
# pull, and the image this ISO carries was never published to a registry.
#
# The user block is what phase 6 logs in as. Without it the installed system
# has no account at all and the greeter has nobody to offer.
#
# Deliberately no scratch disk mounted at /var/lib/containers: Bluefin's ISO
# pulls the payload from a registry and needs real space for it; Utah's ISO
# carries the image in the squashfs, and mounting over that path hides it.
cat > "${WORK}/recipe.json" <<EOF
{
  "disk": "/dev/vda",
  "filesystem": "btrfs",
  "image": "",
  "targetImgref": "${PAYLOAD_IMAGE}",
  "composeFsBackend": false,
  "bootloader": "grub2",
  "hostname": "utah-luks-test",
  "encryption": {"type": "luks-passphrase", "passphrase": "${PASSPHRASE}"},
  "user": {
    "username": "${TEST_USER}",
    "fullname": "Utah End To End",
    "password": "${TEST_PASSWORD}",
    "groups": ["wheel"]
  },
  "flatpaks": []
}
EOF
scp_live "${WORK}/recipe.json" liveuser@127.0.0.1:/tmp/luks-recipe.json

echo "Running the installer from the ISO's embedded store..."
ssh_live 'sudo /usr/local/bin/fisherman /tmp/luks-recipe.json'

# The installed system already boots with console=ttyS0, but also with
# "rhgb quiet", which suppresses exactly the systemd messages phase 6 needs to
# see. Drop those and forward the journal to the console, so "Reached target
# Graphical Interface" actually reaches the serial log and the test can assert
# on it rather than on pixel colour. Dakota patches the same entries for the
# same reason; it only has to add the console because its entries lack one.
echo "Making the installed system boot verbosely on the serial console..."
ssh_live 'sudo bash -euc "
    tmp=\$(mktemp -d)
    trap \"umount \$tmp 2>/dev/null || true; rmdir \$tmp\" EXIT
    seen=0
    for part in /dev/vda2 /dev/vda1; do
        mount \$part \$tmp 2>/dev/null || continue
        for entry in \$tmp/loader/entries/*.conf \$tmp/EFI/*/loader/entries/*.conf; do
            [ -f \"\$entry\" ] || continue
            grep -q \"^options \" \"\$entry\" || continue
            seen=\$((seen+1))
            sed -i \"s/ rhgb//; s/ quiet//\" \"\$entry\"
            grep -q \"console=ttyS0\" \"\$entry\" || \
                sed -i \"s|^options .*|& console=tty0 console=ttyS0|\" \"\$entry\"
            grep -q \"forward_to_console\" \"\$entry\" || \
                sed -i \"s|^options .*|& systemd.journald.forward_to_console=yes|\" \"\$entry\"
            # Unlocking this LUKS2 volume costs ~30s of argon2 before userspace
            # starts, and on a loaded host udev can still be settling when
            # systemd gives up on /boot at its 45s default -- the installed
            # system then drops to an emergency shell over a disk that is
            # provably intact and boots fine when the host is quieter. Give the
            # device jobs room rather than let host load decide the verdict.
            grep -q \"default_device_timeout_sec\" \"\$entry\" || \
                sed -i \"s|^options .*|& systemd.default_device_timeout_sec=180|\" \"\$entry\"
            echo \"  patched \$(basename \$entry)\"
        done
        umount \$tmp || { echo \"could not unmount \$part\" >&2; exit 1; }
    done
    # Push it all the way to the image file while a working guest is still
    # here to do it. Everything after this point is a race against a guest
    # that never powers down on its own, and an unflushed /boot is exactly
    # the ext4 the installed system cannot probe.
    sync
    blockdev --flushbufs /dev/vda2 2>/dev/null || true
    blockdev --flushbufs /dev/vda 2>/dev/null || true
    echo \"boot entries seen: \$seen\"
    [ \$seen -gt 0 ]
"' || fail "found no boot entry to make verbose -- the install may not have written one"

echo "Install complete. Powering the live VM down..."
# Wait for it to actually exit, rather than assuming a few seconds is enough.
# The next phase boots the disk this VM has just written; starting while the
# guest is still flushing gives the installed system a /boot whose ext4 the
# kernel cannot probe, and the boot dies waiting for a device that is on the
# disk but not yet consistent:
#   Timed out waiting for device /dev/disk/by-uuid/... - Dependency failed for
#   boot.mount
# ACPI alone has never once stopped this guest: every run so far hit the
# deadline below and was force-killed, the passing ones included. The live
# session simply does not act on the power button, so ask the guest directly
# and keep ACPI only as a fallback.
ssh_live 'sudo systemctl poweroff --no-block' 2>/dev/null \
    || monitor "${MONITOR_LIVE}" "system_powerdown" || true
live_pid="$(cat "${WORK}/live.pid" 2>/dev/null || true)"
# Ten minutes, not one. This guest genuinely takes minutes to stop: it unmounts
# /boot at around t+425s, and forcing it at 60s cut the flush in half, leaving
# an ext4 the installed system could not probe --
#   Timed out waiting for device /dev/disk/by-uuid/... ; boot.mount failed
# which is the emergency shell this test kept hitting. Waiting is cheap; a
# corrupted disk costs a whole run and looks like an image bug.
shutdown_deadline=600
for ((i = 0; i < shutdown_deadline; i++)); do
    kill -0 "${live_pid}" 2>/dev/null || break
    if (( i > 0 && i % 60 == 0 )); then
        echo "  still shutting down (${i}s)..."
    fi
    sleep 1
done
if kill -0 "${live_pid}" 2>/dev/null; then
    echo "  live VM did not shut down in ${shutdown_deadline}s; forcing it" >&2
    monitor "${MONITOR_LIVE}" "quit" || true
fi
for i in $(seq 1 15); do
    kill -0 "${live_pid}" 2>/dev/null || break
    sleep 1
done
kill -0 "${live_pid}" 2>/dev/null && fail "the live VM would not exit; refusing to boot a disk it may still be writing"
echo "  live VM exited cleanly"
sync

echo "=== Phase 4/6: boot the installed disk ==="
# Carry the live VM's firmware variables over rather than starting from a
# pristine copy. The installer writes an NVRAM boot entry pointing at
# \EFI\fedora\shimx64.efi, and this ESP has no \EFI\BOOT\BOOTX64.EFI
# fallback, so a reset NVRAM leaves nothing bootable: OVMF walks its default
# list, finds no disk entry, and drops to the EFI Internal Shell. That looks
# exactly like a Plymouth prompt to a screenshot -- a dark, static screen --
# which is how an earlier version of this test reported a pass while typing
# the passphrase at a firmware shell. Real hardware keeps its NVRAM; so do we.
cp -f "${VARS}" "${WORK}/ovmf-vars-installed.fd"
"${QEMU}" \
    -machine q35 -cpu host -m 8192 -smp 4 ${ACCEL} \
    -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
    -drive "if=pflash,format=raw,file=${WORK}/ovmf-vars-installed.fd" \
    -drive "if=none,id=disk,file=${INSTALL_DISK},format=qcow2" \
    -device virtio-blk-pci,drive=disk \
    -netdev "user,id=net0,hostfwd=tcp::${SSH_PORT_INSTALLED}-:22" \
    -device virtio-net-pci,netdev=net0 \
    -monitor "unix:${MONITOR_INSTALLED},server,nowait" \
    -serial "file:${SERIAL_INSTALLED}" \
    -vnc "127.0.0.1:${VNC_INSTALLED}" \
    -daemonize -pidfile "${WORK}/installed.pid"
qemu_pids+=("$(cat "${WORK}/installed.pid")")
echo "  watch the installed VM: vnc://127.0.0.1:$((5900 + VNC_INSTALLED))"
sleep 5

echo "=== Phase 5/6: answer the passphrase prompt ==="
status=0
python3 "${ROOT}/iso/scripts/luks-unlock.py" qemu \
    "${MONITOR_INSTALLED}" "${PASSPHRASE}" "${SERIAL_INSTALLED}" || status=$?
if [[ ${status} -ne 0 ]]; then
    shot luks-failed "${MONITOR_INSTALLED}" || true
    tail -60 "${SERIAL_INSTALLED}" >&2 || true
    fail "LUKS unlock or post-unlock boot failed (exit ${status})"
fi

echo "=== Phase 6/6: log in and prove the desktop starts ==="
echo "Waiting for the installed system to reach the graphical target..."
emergency_seen=""
for i in $(seq 1 90); do
    if grep -qa "Reached target.*Graphical" "${SERIAL_INSTALLED}" 2>/dev/null; then
        echo "  serial: reached the graphical target"; break
    fi

    if grep -qa "Kernel panic" "${SERIAL_INSTALLED}" 2>/dev/null; then
        shot installed-panic "${MONITOR_INSTALLED}" || true
        diagnose_boot "${SERIAL_INSTALLED}"
        fail "the installed system panicked"
    fi
    if [[ -z "${emergency_seen}" ]] \
       && grep -qaE "Emergency mode|You are in emergency mode" "${SERIAL_INSTALLED}" 2>/dev/null; then
        # Do not give up here. emergency.service runs sulogin, and this image
        # locks root, so sulogin exits at once and the boot continues -- it may
        # still reach the graphical target. Record the evidence and keep
        # watching; the verdict comes from whether a unit failed, below.
        emergency_seen=1
        shot installed-emergency "${MONITOR_INSTALLED}" || true
    fi
    [[ "$i" -eq 90 ]] && { diagnose_boot "${SERIAL_INSTALLED}"; fail "no graphical target after 7m30s"; }
    sleep 5
done

if [[ -n "${emergency_seen}" ]]; then
    diagnose_boot "${SERIAL_INSTALLED}"
    fail "the installed system entered emergency mode during boot (it carried on, because root is locked and sulogin exits immediately -- see the failed units above)"
fi

echo "Waiting for sshd on the installed system..."
for i in $(seq 1 60); do
    if ssh_target true 2>/dev/null; then break; fi
    [[ "$i" -eq 60 ]] && fail "cannot log in as ${TEST_USER} over SSH"
    sleep 5
done
echo "  ssh: logged in as ${TEST_USER}"

ssh_target 'systemctl is-active gdm.service' 2>/dev/null | grep -qx active \
    || fail "gdm is not running on the installed system"
echo "  gdm.service: active"
shot installed-greeter "${MONITOR_INSTALLED}"

# Arrange for a terminal to open as part of the session that is about to
# start. Launching a GTK application over SSH does not work and cannot be made
# to: the session bus refuses to let it register --
#   error registering application: GDBus.Error:...AccessDenied
#   error: ApplicationRegisterFailed
# -- so it never maps a window, and the attempt leaves a half-made user flatpak
# repo behind that breaks every later flatpak call for that account. An
# autostart entry is how a session is meant to be told to run something, and it
# runs inside the session with a bus and a display of its own.
echo "Arranging for a terminal to open in the session..."
ssh_target "
    grep -q 'utah-e2e fastfetch' ~/.bashrc 2>/dev/null || printf '%s\n' '[[ \$- == *i* ]] && fastfetch # utah-e2e fastfetch' >> ~/.bashrc
    mkdir -p ~/.config/autostart
    cat > ~/.config/autostart/${TERMINAL_APP}.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Terminal
Exec=flatpak --system run ${TERMINAL_APP}
X-GNOME-Autostart-enabled=true
EOF
" 2>/dev/null || echo "  could not write the autostart entry" >&2

# Type the password at the greeter. GDM offers the single account already
# selected, so Enter opens the password field and the password submits it.
echo "Logging in at the greeter as ${TEST_USER}..."
monitor "${MONITOR_INSTALLED}" "sendkey ret" || true
sleep 3
send_keys "${MONITOR_INSTALLED}" "${TEST_PASSWORD}"

echo "Waiting for a GNOME session..."
logged_in=0
for i in $(seq 1 48); do
    if ssh_target "pgrep -u ${TEST_USER} -x gnome-shell >/dev/null" 2>/dev/null; then
        logged_in=1; break
    fi
    sleep 5
done
if (( ! logged_in )); then
    shot installed-login-failed "${MONITOR_INSTALLED}" || true
    echo "--- sessions ---" >&2
    ssh_target 'loginctl list-sessions --no-legend' >&2 2>/dev/null || true
    fail "no gnome-shell for ${TEST_USER} after logging in at the greeter"
fi
echo "  gnome-shell: running as ${TEST_USER}"

# A shell session that started is not the same as one whose extensions loaded.
# Assert the enabled extensions raised no load-time error this boot -- the
# GNOME-51 breaks this test exists to catch (GSConnect's clipboard final-type,
# Search Light's dropped shader API) surface here as "Error"/"TypeError" lines
# against the extension uuid. UTAH_E2E_EXTENSIONS lists the ones that must load
# clean; empty to skip.
EXT_CHECK="${UTAH_E2E_EXTENSIONS-gsconnect@andyholmes.github.io search-light@icedman.github.com}"
if [[ -n "${EXT_CHECK}" ]]; then
    for uuid in ${EXT_CHECK}; do
        # State is the authoritative signal: an extension that threw at enable
        # is ERROR/OUT_OF_DATE, one that loaded is ACTIVE. Grepping the journal
        # for "Error" also catches an extension's own deliberate warnings (the
        # GSConnect guard logs one when it degrades the clipboard portal), so
        # trust the state and only surface journal lines as diagnostics.
        # gnome-extensions asks the shell over D-Bus, and the shell answers
        # only once it has finished loading extensions -- which is strictly
        # after the gnome-shell process appears. Polling here rather than
        # reading once is the difference between a real verdict and a blank.
        state=""
        for _ in $(seq 1 24); do
            state="$(ssh_target "env BASH_ENV=/dev/null bash --noprofile --norc -c \"gnome-extensions info '${uuid}' 2>/dev/null\"" 2>/dev/null | grep -aE '^[[:space:]]*State:' | tail -1 | awk '{print $NF}' | tr -d '[:space:]' || true)"
            [[ -n "${state}" ]] && break
            sleep 5
        done
        if [[ -z "${state}" ]]; then
            # Two minutes of polling a shell that is already up and still no
            # state is not an inconclusive probe -- it means the assertion is
            # not running, which is worse than a red run because it reads as
            # green. (It did exactly that once: an over-escaped awk sent
            # `\$NF' to awk, every read came back empty, and the check passed
            # while asserting nothing.) Fail, and print the raw output.
            echo "  extension ${uuid}: state could not be read" >&2
            ssh_target "env BASH_ENV=/dev/null bash --noprofile --norc -c \"gnome-extensions info '${uuid}' 2>&1 | head -20\"" >&2 2>/dev/null || true
            shot installed-ext-unreadable "${MONITOR_INSTALLED}" || true
            fail "could not read the state of extension ${uuid}"
        fi
        if [[ "${state}" != "ACTIVE" && "${state}" != "ENABLED" ]]; then
            echo "  extension ${uuid}: state=${state}" >&2
            ssh_target "env BASH_ENV=/dev/null bash --noprofile --norc -c \"journalctl --user -b --no-pager 2>/dev/null | grep -F '${uuid}' | grep -iE 'Error|TypeError|Exception|not a function' | tail -5\"" >&2 2>/dev/null || true
            shot installed-ext-error "${MONITOR_INSTALLED}" || true
            fail "extension ${uuid} did not reach ACTIVE on GNOME 51 (state=${state})"
        fi
        echo "  extension ${uuid}: ${state}"
    done
fi

# Ask for the user's *graphical* session by id rather than taking the first
# session that mentions them: the SSH login this test is using is also a
# session, it is also theirs, and it sorts first -- which reported "tty" for a
# desktop the screenshot plainly shows.
session_type="$(ssh_target "bash --norc --noprofile -c 'loginctl show-session \$(loginctl show-user ${TEST_USER} -p Display --value) -p Type --value'" 2>/dev/null | tail -1 || true)"
echo "  session type: ${session_type:-unknown}"
sleep 10   # let the shell finish drawing before the screenshot
shot installed-desktop "${MONITOR_INSTALLED}"

# Open a terminal on the desktop and leave fastfetch on screen. This is the
# shot a human actually reads: it names the OS, the kernel and the desktop
# from inside the installed system, so one image carries what half a dozen
# assertions above prove separately.
sleep 20
shot installed-fastfetch "${MONITOR_INSTALLED}"

echo
echo "PASS: Utah installed to an encrypted disk, unlocked, and ${TEST_USER} logged"
echo "      in to a GNOME session on it."
echo "Screenshots: ${SHOTS}"

# Publish the screenshots as the record of what passed. A run that only prints
# "PASS" is a claim; the same run with the greeter and the desktop it produced
# is evidence, and it is reviewable in a pull request without a QEMU host.
DOCS="${UTAH_E2E_DOCS:-${ROOT}/docs/verification}"
if [[ -n "${DOCS}" && "${DOCS}" != "none" ]]; then
    mkdir -p "${DOCS}/screenshots"
    # Only the shots this record actually shows. The work directory also
    # accumulates failure shots (installed-emergency, luks-failed, ...) from
    # earlier attempts, and a glob swept those into the verification record --
    # which is meant to be the evidence a run passed, not a pile of the ways
    # previous ones did not.
    for _s in live-desktop installed-greeter installed-desktop installed-fastfetch; do
        cp -f "${SHOTS}/${_s}.png" "${DOCS}/screenshots/" 2>/dev/null || true
    done
    captured="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    iso_size="$(du -h "${ISO}" | cut -f1)"
    cat > "${DOCS}/README.md" <<EOF
# Verification

Written by \`just luks-test\` (iso/scripts/luks-e2e.sh). Do not edit by hand:
the next passing run overwrites it.

Every image below is a QEMU screendump taken during that run, at the moment
the check beside it passed.

| | |
| --- | --- |
| Captured | ${captured} |
| Live ISO | \`$(basename "${ISO}")\`, ${iso_size} |
| Installed image | \`${PAYLOAD_IMAGE}\` |
| Root filesystem | btrfs on LUKS2, passphrase unlock |
| Live session | GNOME, ${live_session_type:-unknown} |
| Installed session | GNOME, ${session_type:-unknown}, user \`${TEST_USER}\` |

## What passed

1. The live ISO boots and its session reaches \`graphical.target\` with
   \`gdm.service\` active and \`gnome-shell\` running — not a text console
   that merely answers SSH.
2. The installer creates a LUKS2 volume and installs from the ISO's embedded
   container store, with no network.
3. The installed disk boots on its own, with no ISO attached.
4. Plymouth's passphrase prompt is answered and the root volume opens.
5. The system reaches the graphical target rather than an emergency shell.
6. The user logs in at the GDM greeter and gets a GNOME session.

## Screenshots

### The installed system, running
![fastfetch](screenshots/installed-fastfetch.png)

A terminal on the desktop this test built, reporting the OS, kernel and
desktop from inside it. Everything below is how it got there.

### Live session
![Live session](screenshots/live-desktop.png)

The desktop the ISO boots into, with the installer available.

### GDM greeter on the installed system
![Greeter](screenshots/installed-greeter.png)

After the encrypted root has been unlocked and the system has reached the
graphical target. This is what proves the boot did not stop at a console.

### Logged in
![Desktop](screenshots/installed-desktop.png)

\`${TEST_USER}\`'s GNOME session, entered by typing the password at the
greeter above.

EOF
    echo "Verification record: ${DOCS}/README.md"

    # Surface the proof on the repo front page: keep the latest fastfetch shot,
    # from inside the booted encrypted install, at the top of README.md. The
    # block is delimited so each passing run refreshes it in place rather than
    # stacking. Only a green run reaches here, so the badge on the front page
    # always reflects a real end-to-end pass. UTAH_E2E_README=none to skip.
    README="${UTAH_E2E_README:-${ROOT}/README.md}"
    shot_rel="docs/verification/screenshots/installed-fastfetch.png"
    if [[ "${README}" != "none" && -f "${README}" && -f "${DOCS}/screenshots/installed-fastfetch.png" ]]; then
        python3 - "${README}" "${shot_rel}" "${captured}" <<'PYEMBED'
import sys, re
readme, shot, captured = sys.argv[1], sys.argv[2], sys.argv[3]
begin, end = "<!-- BEGIN E2E VERIFICATION -->", "<!-- END E2E VERIFICATION -->"
block = (
    f"{begin}\n"
    f"[![Verified end to end]({shot})](docs/verification/README.md)\n\n"
    f"*Verified end to end on {captured}: installed to a LUKS2-encrypted disk, "
    f"unlocked at the Plymouth prompt, and logged in to a GNOME session — "
    f"the shot above is fastfetch inside that booted install. "
    f"Full record and more screenshots in "
    f"[docs/verification](docs/verification/README.md), refreshed by "
    f"`just luks-test`.*\n"
    f"{end}"
)
text = open(readme, encoding="utf-8").read()
if begin in text and end in text:
    text = re.sub(re.escape(begin) + r".*?" + re.escape(end), block, text, count=1, flags=re.S)
else:
    # Insert right after the first heading line, so the title stays on top.
    lines = text.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            insert_at = i + 1
            break
    lines.insert(insert_at, "\n" + block + "\n\n")
    text = "".join(lines)
open(readme, "w", encoding="utf-8").write(text)
print(f"updated {readme} with the latest verification screenshot")
PYEMBED
    fi
fi
