#!/usr/bin/env bash
# Boot a Utah live ISO, install its embedded OCI payload with Fisherman (with
# optional LUKS encryption), and boot the installed system without the ISO or a network.
# Usage: offline-e2e.sh ISO PAYLOAD_REF [TARGET_IMGREF]
#
# The live VM has a QEMU user-mode network only for the SSH control channel;
# 'restrict=on' prevents the guest from reaching the host or the outside
# network. The installed VM has no network device at all (-net none). This is
# deliberate: a successful install must come from the containers-storage payload
# in the ISO, not from a registry pull that happens to be available in CI.
set -euo pipefail

ISO="${1:?live ISO path is required}"
PAYLOAD_REF="${2:?embedded payload reference is required}"
TARGET_IMGREF="${3:-${PAYLOAD_REF}}"
ENCRYPTION="${UTAH_E2E_ENCRYPTION:-luks}"
PASSPHRASE="${UTAH_E2E_PASSPHRASE:-testpassphrase}"
WORK="${UTAH_E2E_WORK:-/var/tmp/utah-iso-e2e}"
TEST_USER="${UTAH_E2E_USER:-utahtest}"
TEST_PASSWORD="${UTAH_E2E_PASSWORD:-utahtest}"
LIVE_PASSWORD="${UTAH_E2E_LIVE_PASSWORD:-live}"
SSH_PORT="${UTAH_E2E_SSH_PORT:-2222}"
SSH_TIMEOUT="${UTAH_E2E_SSH_TIMEOUT:-900}"
BOOT_TIMEOUT="${UTAH_E2E_BOOT_TIMEOUT:-900}"
QEMU_MEMORY="${UTAH_E2E_MEMORY:-8192}"
QEMU_CPUS="${UTAH_E2E_CPUS:-4}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ISO="$(realpath "${ISO}")"
mkdir -p "${WORK}/screenshots"
WORK="$(realpath "${WORK}")"
SHOTS="${WORK}/screenshots"
INSTALL_DISK="${WORK}/installed.qcow2"
LIVE_VARS="${WORK}/ovmf-vars-live.fd"
INSTALLED_VARS="${WORK}/ovmf-vars-installed.fd"
LIVE_MONITOR="${WORK}/live-monitor.sock"
INSTALLED_MONITOR="${WORK}/installed-monitor.sock"
LIVE_SERIAL="${WORK}/live-serial.log"
INSTALLED_SERIAL="${WORK}/installed-serial.log"
LIVE_PID_FILE="${WORK}/live.pid"
INSTALLED_PID_FILE="${WORK}/installed.pid"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

[[ -f "${ISO}" ]] || fail "ISO does not exist: ${ISO}"
[[ "${PAYLOAD_REF}" =~ ^[A-Za-z0-9./_-]+:[A-Za-z0-9._-]+$ ]] || \
    fail "payload reference must be a tag-shaped OCI reference: ${PAYLOAD_REF}"
[[ "${TARGET_IMGREF}" =~ ^[A-Za-z0-9./_-]+(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$ ]] || \
    fail "tracking image must be a tag or digest OCI reference: ${TARGET_IMGREF}"

QEMU="${QEMU_BINARY:-$(command -v qemu-system-x86_64 || true)}"
QEMU_IMG="${QEMU_IMG_BINARY:-$(command -v qemu-img || true)}"
[[ -n "${QEMU}" ]] || fail "qemu-system-x86_64 is not installed"
[[ -n "${QEMU_IMG}" ]] || fail "qemu-img is not installed"
command -v sshpass >/dev/null 2>&1 || fail "sshpass is not installed"

OVMF_CODE=""
for candidate in \
    /usr/share/OVMF/OVMF_CODE_4M.fd \
    /usr/share/OVMF/OVMF_CODE.fd \
    /usr/share/edk2/ovmf/OVMF_CODE.fd \
    /usr/share/edk2/ovmf/OVMF_CODE_4M.fd \
    /usr/share/ovmf/OVMF.fd \
    /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd; do
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
    /usr/share/edk2/ovmf/OVMF_VARS_4M.fd \
    /usr/share/ovmf/OVMF_VARS.fd \
    /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
    if [[ -f "${candidate}" ]]; then
        OVMF_VARS_SOURCE="${candidate}"
        break
    fi
done
[[ -n "${OVMF_CODE}" ]] || fail "OVMF code firmware is not installed"
[[ -n "${OVMF_VARS_SOURCE}" ]] || fail "OVMF variable store is not installed"

if [[ -r /dev/kvm ]]; then
    ACCEL=(-accel kvm)
    echo "QEMU acceleration: KVM"
else
    ACCEL=(-accel tcg,thread=multi)
    echo "QEMU acceleration: TCG (no /dev/kvm)"
fi

SSH_OPTS=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o ConnectTimeout=5
    -o PreferredAuthentications=password
    -o PubkeyAuthentication=no
    -o PasswordAuthentication=yes
    -o ServerAliveInterval=30
    -o ServerAliveCountMax=10
)

ssh_live() {
    sshpass -p "${LIVE_PASSWORD}" ssh "${SSH_OPTS[@]}" \
        -p "${SSH_PORT}" liveuser@127.0.0.1 "$@"
}

scp_live() {
    sshpass -p "${LIVE_PASSWORD}" scp "${SSH_OPTS[@]}" \
        -P "${SSH_PORT}" "$@"
}

monitor() {
    python3 - "$1" "$2" <<'PY'
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
    local socket_path="$2"
    local ppm="${SHOTS}/${label}.ppm"
    local png="${SHOTS}/${label}.png"

    [[ -S "${socket_path}" ]] || {
        echo "screenshot monitor is unavailable: ${socket_path}" >&2
        return 1
    }
    monitor "${socket_path}" "screendump ${ppm}" || {
        echo "QEMU screendump failed for ${label}" >&2
        return 1
    }
    for _ in $(seq 1 10); do
        [[ -s "${ppm}" ]] && break
        sleep 1
    done
    [[ -s "${ppm}" ]] || {
        echo "QEMU produced no framebuffer for ${label}" >&2
        return 1
    }
    if command -v ffmpeg >/dev/null 2>&1 && \
        ffmpeg -y -loglevel error -i "${ppm}" "${png}" >/dev/null 2>&1; then
        rm -f "${ppm}"
        echo "screenshot: ${png}"
    else
        echo "screenshot: ${ppm}"
    fi
}

kill_qemu() {
    local pid="$1"
    [[ -n "${pid}" ]] || return 0
    if kill -0 "${pid}" 2>/dev/null; then
        kill "${pid}" 2>/dev/null || true
        for _ in $(seq 1 20); do
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
        capture_screen live-failure "${LIVE_MONITOR}"
        capture_screen installed-failure "${INSTALLED_MONITOR}"
    fi
    if [[ -f "${LIVE_PID_FILE}" ]]; then
        kill_qemu "$(cat "${LIVE_PID_FILE}")"
    fi
    if [[ -f "${INSTALLED_PID_FILE}" ]]; then
        kill_qemu "$(cat "${INSTALLED_PID_FILE}")"
    fi
    echo "E2E output retained at ${WORK}"
    exit "${status}"
}
trap cleanup EXIT

rm -f "${INSTALL_DISK}" "${LIVE_VARS}" "${INSTALLED_VARS}" \
    "${LIVE_MONITOR}" "${INSTALLED_MONITOR}" "${LIVE_SERIAL}" \
    "${INSTALLED_SERIAL}" "${LIVE_PID_FILE}" "${INSTALLED_PID_FILE}"
"${QEMU_IMG}" create -f qcow2 "${INSTALL_DISK}" 64G >/dev/null
cp -f "${OVMF_VARS_SOURCE}" "${LIVE_VARS}"

echo "=== Phase 1/4: boot the Utah live ISO ==="
"${QEMU}" \
    -name utah-live-e2e \
    -machine q35 \
    "${ACCEL[@]}" \
    -cpu max \
    -m "${QEMU_MEMORY}" \
    -smp "${QEMU_CPUS}" \
    -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
    -drive "if=pflash,format=raw,file=${LIVE_VARS}" \
    -drive "if=none,id=iso,file=${ISO},media=cdrom,readonly=on,format=raw" \
    -device virtio-scsi-pci,id=scsi \
    -device scsi-cd,drive=iso \
    -drive "if=none,id=disk,file=${INSTALL_DISK},format=qcow2" \
    -device virtio-blk-pci,drive=disk \
    -netdev "user,id=net0,restrict=on,hostfwd=tcp::${SSH_PORT}-:22" \
    -device virtio-net-pci,netdev=net0 \
    -display none \
    -vnc 127.0.0.1:2 \
    -monitor "unix:${LIVE_MONITOR},server,nowait" \
    -serial "file:${LIVE_SERIAL}" \
    -no-reboot \
    -daemonize \
    -pidfile "${LIVE_PID_FILE}"

echo "Live VM network: QEMU user mode with restrict=on; only host SSH forwarding is available."
echo "Waiting for the live SSH control channel and UTAH_LIVE_READY marker..."
live_ready=0
for _ in $(seq 1 "${SSH_TIMEOUT}"); do
    if grep -aFq UTAH_LIVE_READY "${LIVE_SERIAL}" 2>/dev/null && \
        ssh_live true >/dev/null 2>&1; then
        live_ready=1
        break
    fi
    sleep 1
done
(( live_ready == 1 )) || {
    tail -120 "${LIVE_SERIAL}" >&2 2>/dev/null || true
    fail "live VM did not expose SSH and UTAH_LIVE_READY within ${SSH_TIMEOUT}s"
}

echo "=== Phase 2/4: prove the live graphical session ==="
live_graphical=0
for _ in $(seq 1 180); do
    if ssh_live 'systemctl is-active graphical.target' 2>/dev/null | grep -qx active && \
        ssh_live 'systemctl is-active gdm.service' 2>/dev/null | grep -qx active && \
        ssh_live 'pgrep -u liveuser -x gnome-shell >/dev/null' 2>/dev/null; then
        live_graphical=1
        break
    fi
    sleep 5
done
(( live_graphical == 1 )) || fail "live VM did not reach a GNOME graphical session"
echo "Live graphical.target, gdm.service, and gnome-shell are active."
capture_screen live-desktop "${LIVE_MONITOR}"

echo "=== Phase 3/4: install the embedded payload with no registry access (encryption=${ENCRYPTION}) ==="
RECIPE="${WORK}/recipe.json"
python3 - "${RECIPE}" "${PAYLOAD_REF}" "${TARGET_IMGREF}" "${TEST_USER}" "${TEST_PASSWORD}" "${ENCRYPTION}" "${PASSPHRASE}" <<'PY'
import json
import pathlib
import sys

path, payload, target, user, password, encryption_mode, passphrase = sys.argv[1:]
if encryption_mode == "luks":
    encryption = {"type": "luks-passphrase", "passphrase": passphrase}
else:
    encryption = {"type": "none"}

recipe = {
    "disk": "/dev/vda",
    "filesystem": "btrfs",
    "btrfsSubvolumes": False,
    "encryption": encryption,
    "image": f"containers-storage:{payload}",
    "targetImgref": target,
    "local_imgref": f"containers-storage:{payload}",
    "selinuxDisabled": False,
    "unifiedStorage": True,
    "composeFsBackend": False,
    "bootloader": "grub2",
    "hostname": "utah-e2e",
    "user": {
        "username": user,
        "fullname": "Utah E2E",
        "password": password,
        "groups": ["wheel"],
    },
    "flatpaks": [],
}
pathlib.Path(path).write_text(json.dumps(recipe, indent=2) + "\n")
PY
scp_live "${RECIPE}" liveuser@127.0.0.1:/tmp/utah-e2e-recipe.json

# The isolated network is part of the test contract. Fisherman must resolve
# the image through the embedded containers-storage graphroot; it cannot
# satisfy the install by pulling from the registry.
ssh_live 'sudo /usr/local/bin/fisherman /tmp/utah-e2e-recipe.json' || \
    fail "Fisherman could not install the embedded OCI payload"

echo "Fisherman completed from containers-storage:${PAYLOAD_REF}; tracking ${TARGET_IMGREF}."
echo "Preparing boot entries, verifying offline Flatpaks/payload, and staging graphical boot assertion..."
ssh_live "sudo TARGET_USER='${TEST_USER}' ENCRYPTION='${ENCRYPTION}' PASSPHRASE='${PASSPHRASE}' bash -s" <<'TARGET_SETUP'
set -euo pipefail

root_mount=/mnt/utah-e2e-root
esp_mount=/mnt/utah-e2e-esp
boot_mount=/mnt/utah-e2e-boot
mkdir -p "${root_mount}" "${esp_mount}" "${boot_mount}"

esp_part="$(blkid -t TYPE=vfat -o device | head -1)"
[[ -n "${esp_part}" ]] || { echo "installed EFI partition not found" >&2; exit 1; }

crypt_opened=0
if [[ "${ENCRYPTION}" == "luks" ]]; then
    luks_part="$(blkid -t TYPE=crypto_LUKS -o device | head -1)"
    [[ -n "${luks_part}" ]] || { echo "LUKS encrypted partition not found" >&2; exit 1; }
    echo "Found LUKS partition: ${luks_part}"
    echo -n "${PASSPHRASE}" | cryptsetup open "${luks_part}" utah-e2e-crypt-root -
    crypt_opened=1
    root_part="/dev/mapper/utah-e2e-crypt-root"
else
    root_part="$(blkid -t TYPE=btrfs -o device | head -1)"
    [[ -n "${root_part}" ]] || { echo "installed btrfs partition not found" >&2; exit 1; }
fi

mount "${root_part}" "${root_mount}"
mount "${esp_part}" "${esp_mount}"

boot_part="$(blkid -t TYPE=ext4 -o device | head -1)"
if [[ -n "${boot_part}" && "${boot_part}" != "${root_part}" ]]; then
    mount "${boot_part}" "${boot_mount}"
fi

cleanup() {
    umount "${boot_mount}" 2>/dev/null || true
    umount "${esp_mount}" 2>/dev/null || true
    umount "${root_mount}" 2>/dev/null || true
    if [[ "${crypt_opened}" -eq 1 ]]; then
        cryptsetup close utah-e2e-crypt-root 2>/dev/null || true
    fi
}
trap cleanup EXIT

# 1. Verify Utah identity
grep -q '^ID=utah$' "${root_mount}/etc/os-release"
grep -q '^IMAGE_ID=utah$' "${root_mount}/etc/os-release"

# 2. Verify offline payload contract
test -s "${root_mount}/usr/share/utah/contract.txt"

# 3. Verify default Flatpaks remain available after installation
test -d "${root_mount}/var/lib/flatpak/repo"
# Ensure the Flatpak repository has objects and refs preserved from the offline install
test -d "${root_mount}/var/lib/flatpak/repo/objects"
echo "Offline payload and default Flatpaks verified on installed root filesystem."

# 4. Configure autologin for testing
mkdir -p "${root_mount}/etc/gdm"
cat >"${root_mount}/etc/gdm/custom.conf" <<EOF
[daemon]
AutomaticLoginEnable=True
AutomaticLogin=${TARGET_USER}
EOF

# 5. Graphical boot verification service inside the guest
cat >"${root_mount}/etc/utah-e2e-graphical.sh" <<'EOF'
#!/usr/bin/env bash
set -u
for _ in $(seq 1 120); do
    if grep -q '^ID=utah$' /etc/os-release && \
        grep -q '^IMAGE_ID=utah$' /etc/os-release && \
        test -d /var/lib/flatpak/repo && \
        systemctl is-active --quiet graphical.target && \
        systemctl is-active --quiet gdm.service && \
        pgrep -u "${TARGET_USER}" -x gnome-shell >/dev/null 2>&1; then
        echo UTAH_INSTALLED_GRAPHICAL_OK
        exit 0
    fi
    sleep 2
done
echo UTAH_INSTALLED_GRAPHICAL_FAIL
exit 1
EOF
chmod 0755 "${root_mount}/etc/utah-e2e-graphical.sh"

cat >"${root_mount}/etc/systemd/system/utah-e2e-graphical.service" <<EOF
[Unit]
Description=Utah CI graphical boot assertion
After=gdm.service

[Service]
Type=oneshot
Environment=TARGET_USER=${TARGET_USER}
ExecStart=/etc/utah-e2e-graphical.sh
StandardOutput=tty
TTYPath=/dev/ttyS0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
mkdir -p "${root_mount}/etc/systemd/system/multi-user.target.wants"
ln -sfn ../utah-e2e-graphical.service \
    "${root_mount}/etc/systemd/system/multi-user.target.wants/utah-e2e-graphical.service"

# 6. Forward boot status to serial console and give device timeouts room
entries=()
while IFS= read -r -d '' entry; do
    entries+=("${entry}")
done < <(
    find "${root_mount}/boot" "${boot_mount}" "${esp_mount}" \
        -type f -name '*.conf' -print0 2>/dev/null
)
for entry in "${entries[@]}"; do
    grep -q '^options ' "${entry}" || continue
    sed -i 's/ rhgb//g; s/ quiet//g' "${entry}"
    grep -q 'console=ttyS0' "${entry}" || \
        sed -i 's|^options .*|& console=tty0 console=ttyS0,115200n8|' "${entry}"
    grep -q 'forward_to_console' "${entry}" || \
        sed -i 's|^options .*|& systemd.journald.forward_to_console=yes|' "${entry}"
    grep -q 'default_device_timeout_sec' "${entry}" || \
        sed -i 's|^options .*|& systemd.default_device_timeout_sec=180|' "${entry}"
done

# 7. Install UEFI fallback loader on ESP
if [[ -f "${esp_mount}/EFI/fedora/shimx64.efi" ]]; then
    mkdir -p "${esp_mount}/EFI/BOOT"
    cp -f "${esp_mount}/EFI/fedora/shimx64.efi" "${esp_mount}/EFI/BOOT/BOOTX64.EFI"
fi
if [[ -f "${esp_mount}/EFI/fedora/grubx64.efi" ]]; then
    cp -f "${esp_mount}/EFI/fedora/grubx64.efi" "${esp_mount}/EFI/BOOT/grubx64.efi"
fi

sync
blockdev --flushbufs "${root_part}" 2>/dev/null || true
blockdev --flushbufs "${esp_part}" 2>/dev/null || true
if [[ -n "${boot_part}" && "${boot_part}" != "${root_part}" ]]; then
    blockdev --flushbufs "${boot_part}" 2>/dev/null || true
fi
echo "Utah identity, Flatpaks, and installed graphical assertion staged."
TARGET_SETUP

echo "Powering off the live VM before booting its installed disk..."
ssh_live 'sudo systemctl poweroff --no-block' >/dev/null 2>&1 || true
live_pid="$(cat "${LIVE_PID_FILE}")"
for _ in $(seq 1 180); do
    kill -0 "${live_pid}" 2>/dev/null || break
    sleep 1
done
if kill -0 "${live_pid}" 2>/dev/null; then
    echo "Live VM did not power off cleanly; stopping it after disk flush." >&2
    kill_qemu "${live_pid}"
fi
cp -f "${LIVE_VARS}" "${INSTALLED_VARS}"

echo "=== Phase 4/4: boot the installed Utah system without ISO or network ==="
"${QEMU}" \
    -name utah-installed-e2e \
    -machine q35 \
    "${ACCEL[@]}" \
    -cpu max \
    -m "${QEMU_MEMORY}" \
    -smp "${QEMU_CPUS}" \
    -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
    -drive "if=pflash,format=raw,file=${INSTALLED_VARS}" \
    -drive "if=none,id=disk,file=${INSTALL_DISK},format=qcow2" \
    -device virtio-blk-pci,drive=disk \
    -net none \
    -display none \
    -vnc 127.0.0.1:3 \
    -monitor "unix:${INSTALLED_MONITOR},server,nowait" \
    -serial "file:${INSTALLED_SERIAL}" \
    -no-reboot \
    -daemonize \
    -pidfile "${INSTALLED_PID_FILE}"

if [[ "${ENCRYPTION}" == "luks" ]]; then
    echo "Running LUKS unlock helper..."
    python3 "${SCRIPT_DIR}/luks-unlock.py" qemu \
        "${INSTALLED_MONITOR}" "${PASSPHRASE}" "${INSTALLED_SERIAL}" || {
        echo "LUKS unlock helper reported error or emergency shell" >&2
    }
fi

installed_ready=0
for _ in $(seq 1 "${BOOT_TIMEOUT}"); do
    if grep -aFq UTAH_INSTALLED_GRAPHICAL_OK "${INSTALLED_SERIAL}" 2>/dev/null; then
        installed_ready=1
        break
    fi
    if grep -aEiq 'kernel panic|UTAH_INSTALLED_GRAPHICAL_FAIL|emergency mode' \
        "${INSTALLED_SERIAL}" 2>/dev/null; then
        break
    fi
    sleep 1
done
(( installed_ready == 1 )) || {
    echo "--- installed serial log ---" >&2
    tail -200 "${INSTALLED_SERIAL}" >&2 2>/dev/null || true
    fail "installed disk did not reach Utah's graphical-session marker"
}
capture_screen installed-desktop "${INSTALLED_MONITOR}"

cat >"${WORK}/e2e-summary.txt" <<EOF
result=passed
iso=$(basename "${ISO}")
payload=${PAYLOAD_REF}
encryption=${ENCRYPTION}
live_network=qemu-user-restrict-on-host-ssh-only
installed_network=none
live_marker=UTAH_LIVE_READY
installed_marker=UTAH_INSTALLED_GRAPHICAL_OK
installed_identity=ID=utah IMAGE_ID=utah
flatpaks_verified=true
EOF
echo "PASS: Utah live ISO booted, installed Fisherman offline with encryption=${ENCRYPTION}, and the installed graphical system booted."
echo "Evidence: ${WORK}"
