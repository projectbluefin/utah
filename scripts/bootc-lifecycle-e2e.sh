#!/usr/bin/env bash
# Validate bootc upgrade and rollback lifecycle on Utah in QEMU.
#
# Exercises the complete atomic update journey:
#   Phase 1: Boot initial Utah deployment -> assert identity, graphical target, and capture baseline digest.
#   Phase 2: Upgrade to candidate digest using bootc/uupd policy -> assert staged deployment.
#   Phase 3: Reboot into upgraded deployment -> assert candidate digest, graphical target, and desktop smoke.
#   Phase 4: Rollback to previous deployment -> assert rollback staged.
#   Phase 5: Reboot into rolled back deployment -> assert baseline digest and graphical desktop restored.
#
# Usage:
#   bootc-lifecycle-e2e.sh [BASE_IMAGE_OR_DISK] [CANDIDATE_IMAGE_OR_DIGEST]
#
# Environment variables:
#   UTAH_LIFECYCLE_WORK   Working directory (default: /var/tmp/utah-lifecycle-e2e)
#   SSH_PORT              Host port for guest SSH (default: 2222)
#   BOOT_TIMEOUT          Maximum seconds to wait for boot / SSH (default: 600)
#   UPGRADE_TIMEOUT       Maximum seconds to wait for bootc upgrade (default: 900)
#   QEMU_MEMORY           VM memory in MB (default: 8192)
#   QEMU_CPUS             VM vCPUs (default: 4)
#   QEMU_BINARY           QEMU binary (default: qemu-system-x86_64)
#   QEMU_IMG_BINARY       qemu-img binary (default: qemu-img)

set -euo pipefail

BASE_TARGET="${1:-localhost/utah:testing}"
CANDIDATE_TARGET="${2:-}"
WORK="${UTAH_LIFECYCLE_WORK:-/var/tmp/utah-lifecycle-e2e}"
SSH_PORT="${SSH_PORT:-2222}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-600}"
UPGRADE_TIMEOUT="${UPGRADE_TIMEOUT:-900}"
QEMU_MEMORY="${QEMU_MEMORY:-8192}"
QEMU_CPUS="${QEMU_CPUS:-4}"

CURRENT_PHASE="init"
QEMU_PID=""

mkdir -p "${WORK}/screenshots" "${WORK}/diagnostics"
WORK="$(realpath "${WORK}")"
SHOTS="${WORK}/screenshots"
DIAGS="${WORK}/diagnostics"
DISK="${WORK}/bootable.raw"
VARS="${WORK}/ovmf-vars.fd"
MONITOR="${WORK}/monitor.sock"
SERIAL="${WORK}/serial.log"
PID_FILE="${WORK}/qemu.pid"
KEY_FILE="${WORK}/id_ed25519"

fail() {
    echo "FAIL [${CURRENT_PHASE}]: $*" >&2
    exit 1
}

# Find QEMU, qemu-img, and python3
QEMU="${QEMU_BINARY:-$(command -v qemu-system-x86_64 || true)}"
QEMU_IMG="${QEMU_IMG_BINARY:-$(command -v qemu-img || true)}"
PYTHON="$(command -v python3 || true)"
SSH="$(command -v ssh || true)"

[[ -n "${PYTHON}" ]] || fail "python3 is not installed"
[[ -n "${SSH}" ]] || fail "ssh is not installed"
[[ -n "${QEMU}" ]] || fail "qemu-system-x86_64 is not installed"
[[ -n "${QEMU_IMG}" ]] || fail "qemu-img is not installed"

# Resolve OVMF firmware
OVMF_CODE=""
for candidate in \
    /usr/share/OVMF/OVMF_CODE_4M.fd \
    /usr/share/OVMF/OVMF_CODE.fd \
    /usr/share/edk2/ovmf/OVMF_CODE.fd \
    /usr/share/edk2/ovmf/OVMF_CODE_4M.fd; do
    if [[ -f "${candidate}" ]]; then
        OVMF_CODE="${candidate}"
        break
    fi
done

OVMF_VARS_SOURCE=""
for candidate in \
    /usr/share/OVMF/OVMF_VARS_4M.fd \
    /usr/share/OVMF/OVMF_VARS.fd \
    /usr/share/edk2/ovmf/OVMF_VARS.fd \
    /usr/share/edk2/ovmf/OVMF_VARS_4M.fd; do
    if [[ -f "${candidate}" ]]; then
        OVMF_VARS_SOURCE="${candidate}"
        break
    fi
done

[[ -n "${OVMF_CODE}" ]] || fail "OVMF code firmware is not installed"
[[ -n "${OVMF_VARS_SOURCE}" ]] || fail "OVMF variable store is not installed"

# Check acceleration
if [[ -r /dev/kvm ]]; then
    ACCEL=(-accel kvm)
    echo "QEMU acceleration: KVM"
else
    ACCEL=(-accel tcg,thread=multi)
    echo "QEMU acceleration: TCG (no /dev/kvm)"
fi

# Generate ephemeral SSH key if not already present
if [[ ! -f "${KEY_FILE}" ]]; then
    ssh-keygen -t ed25519 -N "" -f "${KEY_FILE}" >/dev/null 2>&1
    chmod 0600 "${KEY_FILE}"
fi

# SSH command options
SSH_OPTS=(
    -i "${KEY_FILE}"
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o ConnectTimeout=5
    -o BatchMode=yes
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=4
    -p "${SSH_PORT}"
)

guest_ssh() {
    "${SSH}" "${SSH_OPTS[@]}" root@127.0.0.1 "$@"
}

monitor_cmd() {
    "${PYTHON}" - "${MONITOR}" "$1" <<'PY'
import socket
import sys
import time

path, command = sys.argv[1:]
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(10)
    client.connect(path)
    client.sendall(command.encode() + b"\n")
    time.sleep(0.4)
PY
}

capture_screen() {
    local label="$1"
    local ppm="${SHOTS}/${label}.ppm"
    local png="${SHOTS}/${label}.png"

    [[ -S "${MONITOR}" ]] || return 0
    monitor_cmd "screendump ${ppm}" >/dev/null 2>&1 || return 0
    for _ in $(seq 1 10); do
        [[ -s "${ppm}" ]] && break
        sleep 0.5
    done
    [[ -s "${ppm}" ]] || return 0
    if command -v ffmpeg >/dev/null 2>&1 && \
        ffmpeg -y -loglevel error -i "${ppm}" "${png}" >/dev/null 2>&1; then
        rm -f "${ppm}"
        echo "Saved screenshot: ${png}"
    else
        echo "Saved framebuffer: ${ppm}"
    fi
}

kill_qemu() {
    local pid="$1"
    [[ -n "${pid}" ]] || return 0
    if kill -0 "${pid}" 2>/dev/null; then
        kill "${pid}" 2>/dev/null || true
        for _ in $(seq 1 15); do
            kill -0 "${pid}" 2>/dev/null || return 0
            sleep 1
        done
        kill -KILL "${pid}" 2>/dev/null || true
    fi
}

cleanup() {
    local status=$?
    set +e
    if [[ "${status}" -ne 0 ]]; then
        echo "=== LIFECYCLE TEST FAILED AT PHASE '${CURRENT_PHASE}' ===" >&2
        capture_screen "failure-${CURRENT_PHASE}"
        if [[ -f "${WORK}/${CURRENT_PHASE}-status.json" ]]; then
            "${PYTHON}" scripts/verify-bootc-lifecycle.py record-phase \
                --work-dir "${WORK}" \
                --phase "${CURRENT_PHASE}" \
                --status-file "${WORK}/${CURRENT_PHASE}-status.json" \
                --verdict FAIL \
                --notes "Failure trapped during phase execution" || true
        fi
        "${PYTHON}" scripts/verify-bootc-lifecycle.py generate-report --work-dir "${WORK}" || true
        if [[ -f "${WORK}/lifecycle-report.md" ]]; then
            cat "${WORK}/lifecycle-report.md" >&2 || true
        fi
        echo "--- Serial console excerpt ---" >&2
        tail -n 80 "${SERIAL}" >&2 2>/dev/null || true
    fi

    if [[ -n "${QEMU_PID}" ]]; then
        kill_qemu "${QEMU_PID}"
    elif [[ -f "${PID_FILE}" ]]; then
        kill_qemu "$(cat "${PID_FILE}")"
    fi
    echo "Lifecycle artifacts retained at ${WORK}"
    exit "${status}"
}
trap cleanup EXIT

# Start QEMU instance
start_qemu() {
    local name="$1"
    rm -f "${MONITOR}" "${PID_FILE}"
    echo "Starting QEMU VM [${name}]..."
    "${QEMU}" \
        -name "utah-lifecycle-${name}" \
        -machine q35 \
        "${ACCEL[@]}" \
        -cpu max \
        -m "${QEMU_MEMORY}" \
        -smp "${QEMU_CPUS}" \
        -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
        -drive "if=pflash,format=raw,file=${VARS}" \
        -drive "if=none,id=disk,file=${DISK},format=raw" \
        -device virtio-blk-pci,drive=disk \
        -netdev "user,id=net0,hostfwd=tcp::${SSH_PORT}-:22" \
        -device virtio-net-pci,netdev=net0 \
        -display none \
        -vnc 127.0.0.1:2 \
        -monitor "unix:${MONITOR},server,nowait" \
        -serial "file:${SERIAL}" \
        -no-reboot \
        -daemonize \
        -pidfile "${PID_FILE}"

    QEMU_PID="$(cat "${PID_FILE}")"
    echo "QEMU running with PID ${QEMU_PID}"
}

wait_for_ssh() {
    local timeout="${1:-${BOOT_TIMEOUT}}"
    local label="$2"
    echo "Waiting up to ${timeout}s for SSH on port ${SSH_PORT} [${label}]..."
    local ready=0
    for _ in $(seq 1 "${timeout}"); do
        if guest_ssh true >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 1
    done
    (( ready == 1 )) || fail "VM failed to respond on SSH within ${timeout}s [${label}]"
    echo "SSH control channel ready [${label}]."
}

stop_qemu() {
    local label="$1"
    echo "Stopping QEMU [${label}]..."
    guest_ssh 'systemctl poweroff --no-block' >/dev/null 2>&1 || true
    if [[ -n "${QEMU_PID}" ]]; then
        for _ in $(seq 1 60); do
            kill -0 "${QEMU_PID}" 2>/dev/null || break
            sleep 1
        done
        kill_qemu "${QEMU_PID}"
        QEMU_PID=""
    fi
}

reboot_guest() {
    local label="$1"
    echo "Rebooting guest VM [${label}]..."
    guest_ssh 'systemctl reboot --no-block' >/dev/null 2>&1 || true
    if [[ -n "${QEMU_PID}" ]]; then
        for _ in $(seq 1 60); do
            kill -0 "${QEMU_PID}" 2>/dev/null || break
            sleep 1
        done
        kill_qemu "${QEMU_PID}"
        QEMU_PID=""
    fi
    start_qemu "${label}"
    wait_for_ssh "${BOOT_TIMEOUT}" "${label}"
}

verify_desktop_smoke() {
    local label="$1"
    echo "Verifying graphical target, GDM, and desktop health [${label}]..."
    local desktop_active=0
    for _ in $(seq 1 120); do
        if guest_ssh 'systemctl is-active graphical.target' 2>/dev/null | grep -qx active && \
           guest_ssh 'systemctl is-active gdm.service' 2>/dev/null | grep -qx active; then
            desktop_active=1
            break
        fi
        sleep 2
    done
    (( desktop_active == 1 )) || fail "Desktop target/GDM not active [${label}]"

    # Assert Utah identity
    guest_ssh 'grep -q "^ID=utah" /etc/os-release && grep -q "^IMAGE_ID=utah" /etc/os-release' || \
        fail "Utah identity assertion failed in /etc/os-release [${label}]"

    echo "Desktop target, GDM, and Utah identity verified [${label}]."
}

# ---------------------------------------------------------------------------
# Disk Setup
# ---------------------------------------------------------------------------
CURRENT_PHASE="disk_setup"
cp -f "${OVMF_VARS_SOURCE}" "${VARS}"

if [[ -f "${BASE_TARGET}" && ( "${BASE_TARGET}" == *.raw || "${BASE_TARGET}" == *.qcow2 ) ]]; then
    echo "Using existing disk image: ${BASE_TARGET}"
    if [[ "${BASE_TARGET}" != "${DISK}" ]]; then
        cp -f "${BASE_TARGET}" "${DISK}"
    fi
else
    echo "Installing base image '${BASE_TARGET}' to raw disk ${DISK} via bootc install to-disk..."
    rm -f "${DISK}"
    truncate -s 30G "${DISK}"

    mkdir -p "${WORK}/ssh"
    cp -f "${KEY_FILE}.pub" "${WORK}/ssh/authorized_keys"

    sudo podman run --rm --privileged --pid=host \
        --security-opt label=type:unconfined_t \
        -v "${WORK}:/data:Z" \
        "${BASE_TARGET}" bootc install to-disk \
            --via-loopback /data/bootable.raw \
            --filesystem btrfs --wipe --generic-image \
            --karg console=ttyS0,115200 --karg utah.local \
            --root-ssh-authorized-keys /data/id_ed25519.pub

    # Install removable-media EFI fallback for QEMU firmware compatibility
    loop="$(sudo losetup --find --show --partscan "${DISK}")"
    esp="${WORK}/esp_mount"
    mkdir -p "${esp}"
    trap 'sudo umount "${esp}" 2>/dev/null || true; sudo losetup -d "${loop}" 2>/dev/null || true; rm -rf "${esp}"' EXIT
    sudo mount "${loop}p2" "${esp}"
    sudo install -d "${esp}/EFI/BOOT"
    if [[ -f "${esp}/EFI/fedora/shimx64.efi" ]]; then
        sudo install -m 0644 "${esp}/EFI/fedora/shimx64.efi" "${esp}/EFI/BOOT/BOOTX64.EFI"
    fi
    if [[ -f "${esp}/EFI/fedora/grubx64.efi" ]]; then
        sudo install -m 0644 "${esp}/EFI/fedora/grubx64.efi" "${esp}/EFI/BOOT/grubx64.efi"
    fi
    sudo umount "${esp}"
    sudo losetup -d "${loop}"
    rm -rf "${esp}"
    trap cleanup EXIT
    sync
fi

# ---------------------------------------------------------------------------
# Phase 1: Boot initial Utah deployment
# ---------------------------------------------------------------------------
CURRENT_PHASE="initial_boot"
echo "=== Phase 1/5: Boot initial Utah deployment ==="
start_qemu "initial"
wait_for_ssh "${BOOT_TIMEOUT}" "initial"
verify_desktop_smoke "initial"
capture_screen "initial-desktop"

guest_ssh 'bootc status --format=json' > "${WORK}/initial-status.json"
guest_ssh 'cat /etc/os-release' > "${WORK}/initial-os-release.txt"

"${PYTHON}" scripts/verify-bootc-lifecycle.py record-phase \
    --work-dir "${WORK}" \
    --phase "initial_boot" \
    --status-file "${WORK}/initial-status.json" \
    --os-release "${WORK}/initial-os-release.txt" \
    --screenshot "${SHOTS}/initial-desktop.png" \
    --notes "Initial deployment booted to graphical target"

initial_digest="$("${PYTHON}" -c 'import json; print(json.load(open("'${WORK}/initial-status.json'"))["status"]["booted"]["image"]["imageDigest"])')"
echo "Active initial deployment digest: ${initial_digest}"

# ---------------------------------------------------------------------------
# Phase 2: Upgrade to candidate digest using bootc/uupd policy
# ---------------------------------------------------------------------------
CURRENT_PHASE="upgrade_staged"
echo "=== Phase 2/5: Stage upgrade via bootc/uupd policy ==="

# Check uupd update policy daemon
guest_ssh 'systemctl is-enabled uupd.timer' || true

if [[ -n "${CANDIDATE_TARGET}" ]]; then
    echo "Switching to candidate image: ${CANDIDATE_TARGET}"
    guest_ssh "bootc switch '${CANDIDATE_TARGET}'"
else
    echo "Executing bootc upgrade..."
    guest_ssh 'bootc upgrade'
fi

guest_ssh 'bootc status --format=json' > "${WORK}/upgrade-staged-status.json"

"${PYTHON}" scripts/verify-bootc-lifecycle.py record-phase \
    --work-dir "${WORK}" \
    --phase "upgrade_staged" \
    --status-file "${WORK}/upgrade-staged-status.json" \
    --notes "Candidate upgrade staged"

"${PYTHON}" scripts/verify-bootc-lifecycle.py assert-transition \
    --work-dir "${WORK}" \
    --from-phase "initial_boot" \
    --to-phase "upgrade_staged" \
    --relation "upgrade_staged"

staged_digest="$("${PYTHON}" -c 'import json; print(json.load(open("'${WORK}/upgrade-staged-status.json'"))["status"]["staged"]["image"]["imageDigest"])')"
echo "Staged candidate digest: ${staged_digest}"

# ---------------------------------------------------------------------------
# Phase 3: Reboot into upgraded deployment
# ---------------------------------------------------------------------------
CURRENT_PHASE="upgraded_boot"
echo "=== Phase 3/5: Reboot into upgraded candidate deployment ==="
reboot_guest "upgraded"
verify_desktop_smoke "upgraded"
capture_screen "upgraded-desktop"

guest_ssh 'bootc status --format=json' > "${WORK}/upgraded-status.json"
guest_ssh 'cat /etc/os-release' > "${WORK}/upgraded-os-release.txt"

"${PYTHON}" scripts/verify-bootc-lifecycle.py record-phase \
    --work-dir "${WORK}" \
    --phase "upgraded_boot" \
    --status-file "${WORK}/upgraded-status.json" \
    --os-release "${WORK}/upgraded-os-release.txt" \
    --screenshot "${SHOTS}/upgraded-desktop.png" \
    --notes "Upgraded deployment active with desktop target"

"${PYTHON}" scripts/verify-bootc-lifecycle.py assert-transition \
    --work-dir "${WORK}" \
    --from-phase "upgrade_staged" \
    --to-phase "upgraded_boot" \
    --relation "upgraded_boot"

# ---------------------------------------------------------------------------
# Phase 4: Stage rollback
# ---------------------------------------------------------------------------
CURRENT_PHASE="rollback_staged"
echo "=== Phase 4/5: Stage rollback via bootc rollback ==="
guest_ssh 'bootc rollback'

guest_ssh 'bootc status --format=json' > "${WORK}/rollback-staged-status.json"

"${PYTHON}" scripts/verify-bootc-lifecycle.py record-phase \
    --work-dir "${WORK}" \
    --phase "rollback_staged" \
    --status-file "${WORK}/rollback-staged-status.json" \
    --notes "Rollback staged to previous deployment"

"${PYTHON}" scripts/verify-bootc-lifecycle.py assert-transition \
    --work-dir "${WORK}" \
    --from-phase "upgraded_boot" \
    --to-phase "rollback_staged" \
    --relation "rollback_staged"

# ---------------------------------------------------------------------------
# Phase 5: Reboot into rollback deployment
# ---------------------------------------------------------------------------
CURRENT_PHASE="rollback_boot"
echo "=== Phase 5/5: Reboot into rollback deployment ==="
reboot_guest "rollback"
verify_desktop_smoke "rollback"
capture_screen "rollback-desktop"

guest_ssh 'bootc status --format=json' > "${WORK}/rollback-status.json"
guest_ssh 'cat /etc/os-release' > "${WORK}/rollback-os-release.txt"

"${PYTHON}" scripts/verify-bootc-lifecycle.py record-phase \
    --work-dir "${WORK}" \
    --phase "rollback_boot" \
    --status-file "${WORK}/rollback-status.json" \
    --os-release "${WORK}/rollback-os-release.txt" \
    --screenshot "${SHOTS}/rollback-desktop.png" \
    --notes "Rollback deployment active and verified"

"${PYTHON}" scripts/verify-bootc-lifecycle.py assert-transition \
    --work-dir "${WORK}" \
    --from-phase "rollback_staged" \
    --to-phase "rollback_boot" \
    --relation "rollback_boot"

# ---------------------------------------------------------------------------
# Phase 6: Finalize report and diagnostics
# ---------------------------------------------------------------------------
CURRENT_PHASE="complete"
stop_qemu "complete"

echo "=== Generating final lifecycle diagnostics report ==="
"${PYTHON}" scripts/verify-bootc-lifecycle.py generate-report --work-dir "${WORK}"
cat "${WORK}/lifecycle-report.md"

echo "SUCCESS: Bootc upgrade and rollback lifecycle fully validated on Utah."
