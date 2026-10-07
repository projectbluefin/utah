#!/usr/bin/env bash
# Validate bootc upgrade and rollback lifecycle in QEMU.
#
# Lifecycle Phases:
#   1. Boot a known Utah deployment, verify desktop session and baseline digest
#   2. Stage upgrade to a candidate digest using bootc / uupd policy
#   3. Reboot into the upgraded deployment, verify graphical desktop and identity
#   4. Roll back to the previous deployment and reboot
#   5. Verify the restored deployment boots successfully to graphical desktop
#
# Diagnostics:
#   Active phase, deployment slot, and digest are recorded at every stage.
#   Any failure captures screenshots, serial console logs, failed systemd units,
#   and emits a structured lifecycle failure report.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: lifecycle-e2e.sh <disk-or-iso> <baseline-image> <candidate-target-image> [passphrase]

Arguments:
  disk-or-iso             Live debug ISO, or an already installed disk (.qcow2,
                          .raw) that carries the UTAH_LIFECYCLE_USER password
                          account -- the harness drives the guest over SSH with
                          that account. The disk from `just
                          generate-bootable-image` provisions no such account,
                          so pass the debug ISO instead and let the install
                          phase create one.
  baseline-image          Image ref the baseline deployment tracks. A live ISO
                          installs its offline payload under this ref, so it
                          must be the target ref the ISO was built with (`just
                          iso testing` uses ghcr.io/projectbluefin/utah:testing).
                          Phase 1 fails unless the booted deployment tracks it.
  candidate-target-image  Candidate image ref/digest to upgrade to (e.g. ghcr.io/projectbluefin/utah@sha256:...).
                          With the default bootc policy it must differ from
                          baseline-image: `bootc switch` to the ref already
                          booted stages nothing.
  passphrase              LUKS passphrase if disk is encrypted (default: testpassphrase)

Environment variables:
  UTAH_LIFECYCLE_WORK     Working directory for disks and evidence (default: /var/tmp/utah-lifecycle-e2e)
  UTAH_LIFECYCLE_POLICY   Upgrade policy to test: 'bootc' or 'uupd' (default: bootc).
                          'uupd' runs uupd.service in the guest and therefore
                          requires the candidate to live in the same repository
                          as the booted deployment, since uupd follows the
                          reference that deployment already tracks.
  UTAH_LIFECYCLE_RAM      VM memory in MB (default: 8192)
  UTAH_LIFECYCLE_CPUS     VM virtual CPUs (default: 4)
  UTAH_LIFECYCLE_USER     User account on installed system (default: utahtest)
  UTAH_LIFECYCLE_PASSWORD Password for user account (default: utahtest)
  UTAH_LIFECYCLE_SSH_PORT Port forwarded to guest SSH (default: 2224)
  UTAH_LIFECYCLE_VNC      VNC display offset (default: 4)
  UTAH_E2E_WORK           Work directory for the ISO install phase when a live
                          ISO is given (default: <UTAH_LIFECYCLE_WORK>/install)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

DISK_OR_ISO="${1:-}"
BASELINE_REF="${2:-}"
TARGET_IMAGE="${3:-}"
PASSPHRASE="${4:-testpassphrase}"

if [[ -z "${DISK_OR_ISO}" ]]; then
    echo "ERROR: disk-or-iso path is required" >&2
    usage >&2
    exit 1
fi

if [[ -z "${BASELINE_REF}" ]]; then
    echo "ERROR: baseline-image reference is required" >&2
    usage >&2
    exit 1
fi

if [[ -z "${TARGET_IMAGE}" ]]; then
    echo "ERROR: candidate-target-image reference is required" >&2
    usage >&2
    exit 1
fi

WORK="${UTAH_LIFECYCLE_WORK:-/var/tmp/utah-lifecycle-e2e}"
POLICY="${UTAH_LIFECYCLE_POLICY:-bootc}"
VM_RAM="${UTAH_LIFECYCLE_RAM:-8192}"
VM_CPUS="${UTAH_LIFECYCLE_CPUS:-4}"
TEST_USER="${UTAH_LIFECYCLE_USER:-utahtest}"
TEST_PASSWORD="${UTAH_LIFECYCLE_PASSWORD:-utahtest}"
SSH_PORT="${UTAH_LIFECYCLE_SSH_PORT:-2224}"
VNC_DISPLAY="${UTAH_LIFECYCLE_VNC:-4}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ "${POLICY}" != "bootc" && "${POLICY}" != "uupd" ]]; then
    echo "ERROR: UTAH_LIFECYCLE_POLICY must be 'bootc' or 'uupd', got '${POLICY}'" >&2
    exit 1
fi

mkdir -p "${WORK}"
EVIDENCE="${WORK}/evidence"
SHOTS="${WORK}/screenshots"
mkdir -p "${EVIDENCE}" "${SHOTS}"

VM_DISK="${WORK}/lifecycle-vm.qcow2"
VARS="${WORK}/ovmf-vars.fd"
MONITOR="${WORK}/monitor.sock"
SERIAL_LOG="${WORK}/serial.log"
PID_FILE="${WORK}/vm.pid"

ACTIVE_PHASE="initialization"
ACTIVE_DEPLOYMENT="none"
ACTIVE_DIGEST="none"
EXPECTED_DIGEST=""

QEMU="$(command -v qemu-system-x86_64 /usr/libexec/qemu-kvm 2>/dev/null | head -1 || true)"
[[ -n "${QEMU}" ]] || { echo "ERROR: qemu-system-x86_64 not found" >&2; exit 1; }

OVMF_CODE=""
for f in /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/OVMF/OVMF_CODE_4M.fd \
         /usr/share/OVMF/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd \
         /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd; do
    [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
done
[[ -n "${OVMF_CODE}" ]] || { echo "ERROR: OVMF firmware code not found" >&2; exit 1; }

OVMF_VARS_SRC=""
for f in /usr/share/edk2/ovmf/OVMF_VARS.fd /usr/share/OVMF/OVMF_VARS_4M.fd \
         /usr/share/OVMF/OVMF_VARS.fd \
         /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
    [[ -f "$f" ]] && { OVMF_VARS_SRC="$f"; break; }
done
[[ -n "${OVMF_VARS_SRC}" ]] || { echo "ERROR: OVMF variable store not found" >&2; exit 1; }

ACCEL="-accel kvm"
test -r /dev/kvm || { echo "No /dev/kvm; falling back to TCG (much slower)"; ACCEL="-accel tcg,thread=multi"; }

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o ConnectTimeout=5
          -o PreferredAuthentications=password
          -o ServerAliveInterval=30 -o ServerAliveCountMax=20)

ssh_target() {
    sshpass -p "${TEST_PASSWORD}" ssh "${SSH_OPTS[@]}" \
        -p "${SSH_PORT}" "${TEST_USER}@127.0.0.1" "$@"
}

# Supply sudo's password on stdin: the installed test account is in wheel,
# not NOPASSWD, and SSH deliberately runs without a tty. Quote every argument
# before sending it through the guest's shell; never put the password there.
ssh_target_sudo() {
    local command
    printf -v command '%q ' "$@"
    printf '%s\n' "${TEST_PASSWORD}" | ssh_target "sudo -S -p '' -- ${command}"
}

verify_boot_files() {
    local phase="$1" checker
    checker="$(< "${ROOT}/iso/scripts/verify-boot-files.sh")"
    # sudo stdin carries the password, so send the checker as a quoted bash -c
    # argument. Preserve guest output even when a file or SSH check fails.
    ssh_target_sudo bash -c "${checker}" > "${EVIDENCE}/boot-files-${phase}.txt" 2>&1 \
        || diagnose_failure "Kernel/initrd file validation failed (${phase}); see boot-files-${phase}.txt"
}

lifecycle_helper() {
    python3 "${ROOT}/scripts/bootc_lifecycle.py" "$@"
}

# Digest and image lookups must never abort the script: `set -e` would kill the
# run before the caller's guard can raise diagnostics for the empty result.
extract_digest() {
    lifecycle_helper extract-digest --status "$1" --slot "$2" || true
}

extract_image() {
    lifecycle_helper extract-image --status "$1" --slot "$2" || true
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

# Type at the GDM greeter through the monitor, as luks-e2e.sh does. SSH login
# alone starts no graphical session, and every reboot returns to the greeter.
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
            *) return 1 ;;
        esac
        monitor "${sock}" "sendkey ${key}" || return 1
        sleep 0.05
    done
    monitor "${sock}" "sendkey ret"
}

shot() {
    local label="$1" sock="$2"
    local ppm="${SHOTS}/${label}.ppm" png="${SHOTS}/${label}.png"
    rm -f "${ppm}" "${png}"
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

qemu_pids=()
cleanup() {
    for pid in "${qemu_pids[@]:-}"; do
        [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
    done
}
trap cleanup EXIT

diagnose_boot() {
    local log="$1"
    local keep="${log%.log}-$(date +%Y%m%d-%H%M%S).log"
    cp -f "${log}" "${keep}" 2>/dev/null && echo "  serial log kept: ${keep}" >&2
    echo "--- units that failed ---" >&2
    sed 's/\x1b\[[0-9;:]*m//g' "${log}" 2>/dev/null \
        | grep -aE 'Dependency failed|Failed to start|Failed to mount|Timed out waiting|job .* timed out|Started .*emergency' \
        | tail -30 >&2 || echo "  (none recorded)" >&2
    echo "--- last 100 console lines ---" >&2
    tail -100 "${log}" >&2 || true
}

diagnose_failure() {
    local reason="$1"
    echo "DIAGNOSING LIFECYCLE FAILURE in phase: ${ACTIVE_PHASE}" >&2
    shot "failure-${ACTIVE_PHASE}" "${MONITOR}" || true
    diagnose_boot "${SERIAL_LOG}"

    # Query journal and bootc status if SSH is available
    if ssh_target true 2>/dev/null; then
        echo "--- active bootc status ---" >&2
        ssh_target_sudo bootc status >&2 2>/dev/null || true
        ssh_target_sudo bootc status --format=json > "${WORK}/failure-status.json" 2>/dev/null || true
    fi

    # Record structured failure diagnostics
    python3 "${ROOT}/scripts/bootc_lifecycle.py" record-diagnostics \
        --output-dir "${EVIDENCE}" \
        --phase "${ACTIVE_PHASE}" \
        --deployment "${ACTIVE_DEPLOYMENT}" \
        --digest "${ACTIVE_DIGEST}" \
        --status FAIL \
        --reason "${reason}" || true

    # Print human-readable report identifying deployment and digest
    python3 "${ROOT}/scripts/bootc_lifecycle.py" failure-report \
        --phase "${ACTIVE_PHASE}" \
        --deployment "${ACTIVE_DEPLOYMENT}" \
        --digest "${ACTIVE_DIGEST}" \
        ${EXPECTED_DIGEST:+--expected-digest "${EXPECTED_DIGEST}"} \
        --reason "${reason}"

    exit 1
}

wait_for_boot() {
    local phase_label="$1"
    local timeout_secs="${2:-450}"
    echo "Waiting for ${phase_label} to reach graphical target..."
    local seen_target=0
    for (( i=0; i<timeout_secs; i+=5 )); do
        if grep -qa "Reached target.*Graphical" "${SERIAL_LOG}" 2>/dev/null; then
            seen_target=1
            echo "  serial: reached graphical target"
            break
        fi
        if grep -qa "Kernel panic" "${SERIAL_LOG}" 2>/dev/null; then
            diagnose_failure "Kernel panic observed on serial console during ${phase_label}"
        fi
        sleep 5
    done
    if (( ! seen_target )); then
        diagnose_failure "Timeout waiting for graphical target during ${phase_label}"
    fi

    echo "Waiting for SSH connectivity..."
    local ssh_ready=0
    for (( i=0; i<300; i+=5 )); do
        if ssh_target true 2>/dev/null; then
            ssh_ready=1
            echo "  ssh: connected as ${TEST_USER}"
            break
        fi
        sleep 5
    done
    if (( ! ssh_ready )); then
        diagnose_failure "Timeout waiting for SSH connection during ${phase_label}"
    fi
}

reboot_guest() {
    local label="$1" request_rc=0 connection_down=0 shutdown_seen=0 i
    : > "${SERIAL_LOG}"
    # systemd may stop sshd before SSH reports the successful reboot request.
    # Remote command/sudo failures are not transport teardown: fail those
    # immediately. Exit 255 alone proves nothing; require fresh shutdown
    # evidence and observed SSH loss before accepting that transport result.
    if ssh_target_sudo systemctl reboot --no-block; then
        request_rc=0
    else
        request_rc=$?
        if (( request_rc != 255 )); then
            diagnose_failure "Clean reboot request failed (exit ${request_rc}) during ${label}"
        fi
    fi

    for (( i=0; i<180; i+=2 )); do
        if grep -qaE 'Stopping ostree-finalize-staged\.service|Stopped target sysinit\.target|Reached target (shutdown|reboot)\.target|systemd-shutdown|reboot: Restarting system' "${SERIAL_LOG}" 2>/dev/null; then
            shutdown_seen=1
        fi
        if ! ssh_target true 2>/dev/null; then
            connection_down=1
        fi
        if (( connection_down && (request_rc == 0 || shutdown_seen) )); then
            echo "  clean reboot observed: SSH disconnected (request exit ${request_rc})"
            # The following unlock/graphical/digest gates must prove the new
            # boot. Never use QEMU system_reset while finalization is in flight.
            return 0
        fi
        sleep 2
    done

    diagnose_failure "Clean reboot was not observed during ${label} (request exit ${request_rc}, SSH down ${connection_down}, shutdown evidence ${shutdown_seen})"
}

verify_desktop_and_identity() {
    local label="$1"
    echo "Verifying desktop services and identity (${label})..."

    # 1. Graphical target and GDM
    ssh_target 'systemctl is-active graphical.target' | grep -qx active \
        || diagnose_failure "graphical.target is not active during ${label}"
    ssh_target 'systemctl is-active gdm.service' | grep -qx active \
        || diagnose_failure "gdm.service is not active during ${label}"

    # 2. Enter the graphical session, not merely the SSH session. The disposable
    # installed disk offers its single test account selected at GDM: Enter
    # opens the password field, then the same monitor key path as luks-e2e.sh
    # submits the password. Leave an already-running session untouched.
    if ! ssh_target "pgrep -u ${TEST_USER} -x gnome-shell >/dev/null" 2>/dev/null; then
        shot "${label}-greeter" "${MONITOR}"
        echo "Logging in at the greeter as ${TEST_USER} (${label})..."
        monitor "${MONITOR}" "sendkey ret" \
            || diagnose_failure "Could not select the GDM test account during ${label}"
        sleep 3
        send_keys "${MONITOR}" "${TEST_PASSWORD}" \
            || diagnose_failure "Could not submit the GDM test password during ${label}"
    fi
    local shell_active=0
    for _ in $(seq 1 48); do
        if ssh_target "pgrep -u ${TEST_USER} -x gnome-shell >/dev/null" 2>/dev/null; then
            shell_active=1
            break
        fi
        sleep 5
    done
    if (( ! shell_active )); then
        ssh_target 'loginctl list-sessions --no-legend' >&2 2>/dev/null || true
        diagnose_failure "gnome-shell is not running for ${TEST_USER} after GDM login during ${label}"
    fi
    echo "  desktop: gdm.service and gnome-shell active"

    # 3. Enforce the Utah os-release identity, not just a readable file.
    # Keep these values aligned with contracts/bluefin-desktop.toml.
    ssh_target '. /etc/os-release; test "${ID:-}" = hummingbird && test "${NAME:-}" = Utah && printf "%s\n" "${PRETTY_NAME:-}" | grep -Eq "^Utah \\(Version: .+\\)$"' \
        || diagnose_failure "os-release does not identify Utah during ${label}"
    echo "  identity: Utah on Hummingbird"

    # 4. Check for failed units
    local failed_units
    failed_units="$(ssh_target 'systemctl list-units --state=failed --no-legend 2>/dev/null' || true)"
    if [[ -n "${failed_units}" ]]; then
        echo "Warning: failed units observed during ${label}:" >&2
        echo "${failed_units}" >&2
    fi
}

# Collect the systemd-boot entry listing from the guest and run the
# bootmgr validator against the bootc status JSON that the phase already
# produced. Each entry is emitted as `=== ENTRY <path> ===\n<content>\n=== END ===`
# so the parser can read the file names back out without shell globbing; the
# harness-side validation joins the phases together and surfaces a missing
# entry as an explicit failure rather than letting a half-finalized staged
# deployment boot the old kernel set.
collect_bootmgr_listing() {
    # Read both the Fedora/bootc layout's BLS root
    # `/boot/loader/entries` (ostree writes Type #1 entries here per the
    # sysroot deploy) and the alternative ESP `/boot/efi/loader/entries`
    # (BLS spec says implementations should also pick those up). The
    # find tolerates either or both being absent.
    # Reads as root, since on a vfat ESP (/boot/efi) the loader entries
    # are fmask=0077 root-only and an EPERM read silently produces an
    # empty listing indistinguishable from "finalize wrote nothing".
    # `sudo` is preferred over `2>/dev/null || true` so a permission
    # failure fails loudly and the validator's missing-entry message is
    # honest. The ext4 `/boot/loader/entries` is not root-only but the
    # script reads both roots through one command, so the sudo is
    # unconditional.
    #
    # The script travels as a `bash -c` argument through `ssh_target_sudo`,
    # so stdin carries only sudo's password. Never pipe the script to
    # `bash -s` after the password: if sudo did not consume the password
    # line (NOPASSWD, cached ticket), bash would execute it as a command.
    local script
    script="$(cat <<'INNER'
shopt -s nullglob
seen=0
for entries_dir in /boot/loader/entries /boot/efi/loader/entries; do
    [ -d "$entries_dir" ] || continue
    boot_root="${entries_dir%/loader/entries}"
    [ -n "$boot_root" ] || boot_root="/"
    for f in "$entries_dir"/*.conf; do
        [ -f "$f" ] || continue
        printf '=== ENTRY %s ===\n' "$f"
        cat "$f"
        printf '\n=== END ===\n'
        # After the entry body (so parse_loader_listing's current_buf
        # does not collect them into the entry), record existence of
        # the paths the entry references (kernel, initrd, image) so a
        # pruned kernel/initrd surfaces as missing-on-disk rather than
        # as a BLS entry pointing at nothing. The STAT markers are
        # consumed by validate_bootmgr_entries to fail any entry whose
        # referenced files are not present. BLS Type #1 paths are
        # partition-relative, so resolve them under the entry's boot root.
        for path in $(awk '
                    /^linux[[:space:]]/  { print $2 }
                    /^initrd[[:space:]]/ { print $2 }
                    /^image[[:space:]]/  { print $2 }
                ' "$f"); do
            target="${boot_root}/${path#/}"
            if [ -e "$target" ]; then
                printf 'STAT %s %s present\n' "$f" "$path"
            else
                printf 'STAT %s %s missing\n' "$f" "$path"
            fi
        done
        seen=$((seen+1))
    done
done
echo "$seen entries captured" >&2
INNER
)"
    ssh_target_sudo bash -c "${script}"
}

# Arguments:
#   $1 status-file: path to the bootc status JSON the phase captured
#   $2 label: short label written to the evidence file
#   $3 slots: comma-separated bootc slots the phase must have entries for
verify_bootmgr_entries() {
    local status_file="$1"
    local label="$2"
    local slots="${3:-booted,staged,rollback}"
    local listing_file="${EVIDENCE}/loader-entries-${label}.txt"
    local diag_file="${EVIDENCE}/bootmgr-${label}.json"

    echo "Verifying systemd-boot entries (${label})..."
    collect_bootmgr_listing > "${listing_file}" \
        || diagnose_failure "Failed to collect systemd-boot entries during ${label}"
    python3 "${ROOT}/scripts/bootc_lifecycle.py" validate-bootmgr \
        --status "${status_file}" \
        --listing "${listing_file}" \
        --slots "${slots}" \
        --output "${diag_file}" \
        || diagnose_failure "systemd-boot entry validation failed during ${label} (see ${diag_file})"
    echo "  boot-manager: BLS entries verified for slots ${slots}"
}

# --- Prepare Disk ---
echo "=== Preparing test deployment ==="
if [[ "${DISK_OR_ISO}" == *.iso ]]; then
    echo "Running installation phase from ISO ${DISK_OR_ISO}..."
    # luks-e2e.sh writes its installed disk under its own work directory, which
    # defaults somewhere else entirely. Pin it to a directory this script owns so
    # the disk is where the next phase looks for it.
    INSTALL_WORK="${UTAH_E2E_WORK:-${WORK}/install}"
    mkdir -p "${INSTALL_WORK}"
    UTAH_E2E_WORK="${INSTALL_WORK}" bash "${ROOT}/iso/scripts/luks-e2e.sh" \
        "${DISK_OR_ISO}" "${BASELINE_REF}" "${PASSPHRASE}"
    DISK_SOURCE="${INSTALL_WORK}/install.qcow2"
else
    DISK_SOURCE="${DISK_OR_ISO}"
fi

[[ -f "${DISK_SOURCE}" ]] || diagnose_failure "Source disk does not exist: ${DISK_SOURCE}"

rm -f "${VM_DISK}" "${MONITOR}" "${SERIAL_LOG}"
# The overlay must declare the backing file's real format: `bootc install
# to-disk` writes a raw image while the ISO install phase writes qcow2, and
# QEMU refuses to open a backing file whose declared format does not match.
# The backing path must also be absolute, since a relative one would be
# resolved against the overlay's directory in the work tree.
DISK_SOURCE="$(realpath "${DISK_SOURCE}")"
BACKING_FORMAT="$(qemu-img info --output=json "${DISK_SOURCE}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("format",""))' || true)"
[[ -n "${BACKING_FORMAT}" ]] || diagnose_failure "Could not determine image format of ${DISK_SOURCE}"
qemu-img create -f qcow2 -b "${DISK_SOURCE}" -F "${BACKING_FORMAT}" "${VM_DISK}" >/dev/null
cp -f "${OVMF_VARS_SRC}" "${VARS}"

# --- Start VM ---
echo "=== Launching QEMU VM for Lifecycle Test ==="
"${QEMU}" \
    -name utah-lifecycle \
    -machine q35 -cpu host -m "${VM_RAM}" -smp "${VM_CPUS}" ${ACCEL} \
    -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
    -drive "if=pflash,format=raw,file=${VARS}" \
    -drive "if=none,id=disk,file=${VM_DISK},format=qcow2" \
    -device virtio-blk-pci,drive=disk \
    -netdev "user,id=net0,restrict=off,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" \
    -device virtio-net-pci,netdev=net0 \
    -monitor "unix:${MONITOR},server,nowait" \
    -serial "file:${SERIAL_LOG}" \
    -vnc "127.0.0.1:${VNC_DISPLAY}" \
    -daemonize -pidfile "${PID_FILE}"
qemu_pids+=("$(cat "${PID_FILE}")")

# Handle LUKS unlock if disk is encrypted
sleep 3
python3 "${ROOT}/iso/scripts/luks-unlock.py" qemu \
    "${MONITOR}" "${PASSPHRASE}" "${SERIAL_LOG}" 2>/dev/null || true

# --- Phase 1: Baseline Deployment ---
ACTIVE_PHASE="baseline"
ACTIVE_DEPLOYMENT="baseline"
echo "=== Phase 1/5: Verify Baseline Deployment ==="
wait_for_boot "Phase 1 (Baseline)"
verify_desktop_and_identity "baseline"
shot baseline-desktop "${MONITOR}"

ssh_target_sudo bootc status --format=json > "${WORK}/baseline-status.json" \
    || diagnose_failure "Failed to query bootc status from baseline deployment"

BASELINE_DIGEST="$(extract_digest "${WORK}/baseline-status.json" booted)"
[[ -n "${BASELINE_DIGEST}" ]] || diagnose_failure "Could not extract baseline booted digest"
BASELINE_IMAGE="$(extract_image "${WORK}/baseline-status.json" booted)"
[[ -n "${BASELINE_IMAGE}" ]] || diagnose_failure "Could not extract baseline booted image reference"
[[ "${BASELINE_IMAGE}" == "${BASELINE_REF}" ]] \
    || diagnose_failure "Baseline deployment tracks '${BASELINE_IMAGE}', not the requested baseline image '${BASELINE_REF}'"
ACTIVE_DIGEST="${BASELINE_DIGEST}"

python3 "${ROOT}/scripts/bootc_lifecycle.py" validate-phase baseline \
    --status "${WORK}/baseline-status.json" \
    || diagnose_failure "Baseline deployment validation failed"
verify_boot_files baseline

verify_bootmgr_entries "${WORK}/baseline-status.json" baseline booted

python3 "${ROOT}/scripts/bootc_lifecycle.py" record-diagnostics \
    --output-dir "${EVIDENCE}" \
    --phase baseline \
    --deployment "baseline" \
    --digest "${BASELINE_DIGEST}" \
    --status PASS
echo "  [Phase 1] PASS: Active deployment: baseline, Digest: ${BASELINE_DIGEST}"

# --- Phase 2: Stage Upgrade via Policy ---
ACTIVE_PHASE="staged"
ACTIVE_DEPLOYMENT="staged-candidate"
echo "=== Phase 2/5: Stage Candidate Upgrade (${POLICY} policy) ==="
echo "  Target candidate image: ${TARGET_IMAGE}"

if [[ "${POLICY}" == "uupd" ]]; then
    echo "Staging the candidate through uupd..."
    ssh_target 'systemctl is-enabled uupd.timer 2>/dev/null' | grep -qE 'enabled|enabled-runtime' \
        || diagnose_failure "uupd.timer is not enabled on host"
    ssh_target 'command -v uupd >/dev/null 2>&1' \
        || diagnose_failure "uupd is not installed on host"

    # uupd drives `bootc upgrade`, which only follows the image reference the
    # booted deployment already tracks. A candidate in a different repository
    # cannot be reached that way, and quietly running `bootc switch` instead
    # would report a uupd result for an upgrade uupd never performed.
    BASELINE_REPO="$(lifecycle_helper image-repository --ref "${BASELINE_IMAGE}" || true)"
    CANDIDATE_REPO="$(lifecycle_helper image-repository --ref "${TARGET_IMAGE}" || true)"
    if [[ -z "${BASELINE_REPO}" || "${BASELINE_REPO}" != "${CANDIDATE_REPO}" ]]; then
        diagnose_failure "uupd policy can only upgrade within the booted image repository (booted '${BASELINE_REPO}', candidate '${CANDIDATE_REPO}'); rerun with UTAH_LIFECYCLE_POLICY=bootc to switch repositories"
    fi

    # Run the shipped unit rather than the binary directly: the unit is what the
    # timer triggers in production, including its distrobox module override.
    ssh_target_sudo systemctl start uupd.service \
        || diagnose_failure "uupd.service failed while staging the candidate upgrade"
    if ssh_target 'systemctl is-failed uupd.service >/dev/null 2>&1'; then
        diagnose_failure "uupd.service entered a failed state while staging the candidate upgrade"
    fi
else
    echo "Executing bootc switch to candidate target..."
    ssh_target_sudo bootc switch "${TARGET_IMAGE}" \
        || diagnose_failure "bootc switch command failed"
fi

ssh_target_sudo bootc status --format=json > "${WORK}/staged-status.json" \
    || diagnose_failure "Failed to query bootc status after staging upgrade"

CANDIDATE_DIGEST="$(extract_digest "${WORK}/staged-status.json" staged)"
[[ -n "${CANDIDATE_DIGEST}" ]] || diagnose_failure "Could not extract candidate digest after staging"
ACTIVE_DIGEST="${CANDIDATE_DIGEST}"

python3 "${ROOT}/scripts/bootc_lifecycle.py" validate-phase staged \
    --status "${WORK}/staged-status.json" \
    --baseline-digest "${BASELINE_DIGEST}" \
    --candidate-image "${TARGET_IMAGE}" \
    || diagnose_failure "Staged deployment validation failed"
# Finalization publishes the staged entry at shutdown; only published entries
# must have their files now. The upgraded/rollback gates run after reboot.
verify_boot_files staged

# Phase 2 only checks `booted`: ostree-finalize-staged does not write the
# staged BLS entry until shutdown, so on a correctly-functioning system the
# entry does not exist yet and would fail the validator. Phase 3 (post-reboot)
# checks both slots.
verify_bootmgr_entries "${WORK}/staged-status.json" staged "booted"

python3 "${ROOT}/scripts/bootc_lifecycle.py" record-diagnostics \
    --output-dir "${EVIDENCE}" \
    --phase staged \
    --deployment "staged-candidate" \
    --digest "${ACTIVE_DIGEST}" \
    --status PASS
shot upgrade-staged "${MONITOR}"
echo "  [Phase 2] PASS: Active deployment: staged, Candidate digest: ${ACTIVE_DIGEST}"

# --- Phase 3: Boot Upgraded Deployment ---
ACTIVE_PHASE="upgraded"
ACTIVE_DEPLOYMENT="upgraded"
EXPECTED_DIGEST="${ACTIVE_DIGEST}"
echo "=== Phase 3/5: Reboot and Verify Upgraded Deployment ==="
reboot_guest "Phase 3 (Upgraded)"
sleep 3
python3 "${ROOT}/iso/scripts/luks-unlock.py" qemu \
    "${MONITOR}" "${PASSPHRASE}" "${SERIAL_LOG}" 2>/dev/null || true

wait_for_boot "Phase 3 (Upgraded)"
verify_desktop_and_identity "upgraded"
shot upgraded-desktop "${MONITOR}"

ssh_target_sudo bootc status --format=json > "${WORK}/upgraded-status.json" \
    || diagnose_failure "Failed to query bootc status on upgraded deployment"

UPGRADED_DIGEST="$(extract_digest "${WORK}/upgraded-status.json" booted)"
[[ -n "${UPGRADED_DIGEST}" ]] || diagnose_failure "Could not extract booted digest from upgraded deployment"
ACTIVE_DIGEST="${UPGRADED_DIGEST}"

python3 "${ROOT}/scripts/bootc_lifecycle.py" validate-phase upgraded \
    --status "${WORK}/upgraded-status.json" \
    --baseline-digest "${BASELINE_DIGEST}" \
    --candidate-digest "${EXPECTED_DIGEST}" \
    || diagnose_failure "Upgraded deployment validation failed"
verify_boot_files upgraded

# Phase 3 (post-upgrade reboot) must check both `booted` and `rollback`:
# the upgraded deployment is the booted slot, and the previous baseline
# (now the rollback target) is in the rollback slot. Checking only
# `booted` would silently miss the regression where ostree-finalize-staged
# failed to (re)write the rollback slot's BLS entry -- the booted entry
# necessarily exists since the guest booted from it.
verify_bootmgr_entries "${WORK}/upgraded-status.json" upgraded "booted,rollback"

python3 "${ROOT}/scripts/bootc_lifecycle.py" record-diagnostics \
    --output-dir "${EVIDENCE}" \
    --phase upgraded \
    --deployment "upgraded-candidate" \
    --digest "${UPGRADED_DIGEST}" \
    --status PASS
echo "  [Phase 3] PASS: Active deployment: upgraded, Digest: ${UPGRADED_DIGEST}"

# --- Phase 4: Rollback to Previous Deployment ---
ACTIVE_PHASE="rollback"
ACTIVE_DEPLOYMENT="rollback"
EXPECTED_DIGEST="${BASELINE_DIGEST}"
echo "=== Phase 4/5: Rollback to Previous Deployment ==="
echo "Executing bootc rollback inside guest..."
ssh_target_sudo bootc rollback \
    || diagnose_failure "bootc rollback command failed"

reboot_guest "Phase 4 (Rollback)"
sleep 3
python3 "${ROOT}/iso/scripts/luks-unlock.py" qemu \
    "${MONITOR}" "${PASSPHRASE}" "${SERIAL_LOG}" 2>/dev/null || true

wait_for_boot "Phase 4 (Rollback)"
verify_desktop_and_identity "rollback"
shot rollback-desktop "${MONITOR}"

ssh_target_sudo bootc status --format=json > "${WORK}/rollback-status.json" \
    || diagnose_failure "Failed to query bootc status after rollback"

RESTORED_DIGEST="$(extract_digest "${WORK}/rollback-status.json" booted)"
[[ -n "${RESTORED_DIGEST}" ]] || diagnose_failure "Could not extract restored booted digest after rollback"
ACTIVE_DIGEST="${RESTORED_DIGEST}"

python3 "${ROOT}/scripts/bootc_lifecycle.py" validate-phase rollback \
    --status "${WORK}/rollback-status.json" \
    --baseline-digest "${BASELINE_DIGEST}" \
    || diagnose_failure "Rollback verification failed"
verify_boot_files rollback

# Phase 4 (post-rollback) must check both `booted` and `rollback`: the
# rolled-back baseline is now the booted slot, and the upgraded deployment
# (previously booted, now booted-then-replaced) is in the rollback slot.
# Checking only `booted` would miss the regression where the rollback slot
# BLS entry went missing after `bootc rollback`.
verify_bootmgr_entries "${WORK}/rollback-status.json" rollback "booted,rollback"

python3 "${ROOT}/scripts/bootc_lifecycle.py" record-diagnostics \
    --output-dir "${EVIDENCE}" \
    --phase rollback \
    --deployment "baseline-restored" \
    --digest "${RESTORED_DIGEST}" \
    --status PASS
echo "  [Phase 4] PASS: Active deployment: rollback-restored, Digest: ${RESTORED_DIGEST}"

# --- Phase 5: Final Summary ---
ACTIVE_PHASE="complete"
echo "=== Phase 5/5: Generate Lifecycle Summary and Evidence ==="
python3 "${ROOT}/scripts/bootc_lifecycle.py" summary --evidence-dir "${EVIDENCE}"

echo
echo "======================================================================"
echo "PASS: Utah bootc upgrade and rollback lifecycle validated successfully."
echo "  Baseline digest:  ${BASELINE_DIGEST}"
echo "  Candidate digest: ${CANDIDATE_DIGEST}"
echo "  Restored digest:  ${RESTORED_DIGEST}"
echo "  Evidence saved:   ${EVIDENCE}"
echo "  Screenshots:      ${SHOTS}"
echo "======================================================================"
