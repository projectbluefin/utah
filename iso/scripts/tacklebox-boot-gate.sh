#!/usr/bin/env bash
# Live-boot gate for a tacklebox-built Utah ISO.
#
# Boots the ISO in QEMU with the serial console captured to a file, waits for
# the UTAH_LIVE_READY marker the live_customize step installs (#229), and
# fails if it never appears. A boot that stops before the desktop still reaches
# multi-user.target and runs the marker, so this proves the live root came all
# the way up -- not merely that QEMU started.
#
# The gate returns as soon as the outcome is known: the marker on serial
# passes, a kernel panic or a QEMU that exited fails, and only a boot that
# shows neither waits out UTAH_ISO_BOOT_TIMEOUT. The VM is stopped on every
# exit path.
#
# Usage: tacklebox-boot-gate.sh ISO [EXPECT_MARKER]
set -euo pipefail

ISO="$(realpath "${1:?live ISO path is required}")"
EXPECT_MARKER="${2:-UTAH_LIVE_READY}"
WORK="${UTAH_ISO_BOOT_WORK:-/var/tmp/utah-tbx-boot}"
VM_RAM="${UTAH_ISO_BOOT_RAM:-6144}"
VM_CPUS="${UTAH_ISO_BOOT_CPUS:-2}"
BOOT_TIMEOUT="${UTAH_ISO_BOOT_TIMEOUT:-300}"
POLL_SECONDS="${UTAH_ISO_BOOT_POLL:-5}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

mkdir -p "${WORK}"
SERIAL="${WORK}/serial.log"
MONITOR="${WORK}/monitor.sock"
VARS="${WORK}/ovmf-vars.fd"
PIDFILE="${WORK}/boot.pid"
rm -f "${SERIAL}" "${MONITOR}" "${PIDFILE}"
: > "${SERIAL}"

QEMU="$(command -v qemu-system-x86_64 /usr/libexec/qemu-kvm 2>/dev/null | head -1)"
[[ -n "${QEMU}" ]] || { echo "qemu-system-x86_64 not found" >&2; exit 1; }
OVMF_CODE=""
for f in "${UTAH_ISO_BOOT_OVMF_CODE:-}" /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/OVMF/OVMF_CODE_4M.fd \
         /usr/share/OVMF/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd; do
  [[ -n "$f" && -f "$f" ]] && { OVMF_CODE="$f"; break; }
done
[[ -n "${OVMF_CODE}" ]] || { echo "OVMF firmware not found" >&2; exit 1; }
OVMF_VARS_SRC=""
for f in "${UTAH_ISO_BOOT_OVMF_VARS:-}" /usr/share/edk2/ovmf/OVMF_VARS.fd /usr/share/OVMF/OVMF_VARS_4M.fd \
         /usr/share/OVMF/OVMF_VARS.fd /usr/share/ovmf/OVMF_VARS.fd; do
  [[ -n "$f" && -f "$f" ]] && { OVMF_VARS_SRC="$f"; break; }
done
[[ -n "${OVMF_VARS_SRC}" ]] || { echo "OVMF variable store not found" >&2; exit 1; }
cp -f "${OVMF_VARS_SRC}" "${VARS}"

ACCEL="-accel kvm"
QEMU_CPU="host"
# TCG cannot emulate the host CPU model: qemu-system-x86_64 refuses
# '-cpu host' without KVM/HVF, so the fallback must switch both.
{ test -r /dev/kvm && test -w /dev/kvm; } || { echo "No usable /dev/kvm; falling back to TCG (much slower)" >&2; ACCEL="-accel tcg,thread=multi"; QEMU_CPU="qemu64"; }

echo "Booting ${ISO}"
"${QEMU}" \
    -machine q35 -cpu "${QEMU_CPU}" -m "${VM_RAM}" -smp "${VM_CPUS}" ${ACCEL} \
    -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
    -drive "if=pflash,format=raw,file=${VARS}" \
    -drive "if=none,id=iso,file=${ISO},media=cdrom,readonly=on,format=raw" \
    -device virtio-scsi-pci,id=scsi \
    -device scsi-cd,drive=iso \
    -net none \
    -monitor "unix:${MONITOR},server,nowait" \
    -serial "file:${SERIAL}" \
    -display none \
    -daemonize -pidfile "${PIDFILE}"

QEMU_PID="$(cat "${PIDFILE}")"
stop_vm() {
  python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect('${MONITOR}'); s.sendall(b'quit\n')" 2>/dev/null || true
  kill "${QEMU_PID}" 2>/dev/null || true
}
trap stop_vm EXIT

fail_with_serial() {
  echo "FAIL: $1" >&2
  echo "--- units that failed ---" >&2
  sed 's/\x1b\[[0-9;:]*m//g' "${SERIAL}" 2>/dev/null \
    | grep -aE 'Failed to start|Failed to mount|Timed out waiting|emergency' \
    | tail -20 >&2 || true
  echo "--- last 60 serial lines ---" >&2
  tail -60 "${SERIAL}" >&2 || true
  exit 1
}

# The marker is a oneshot after the display manager, so it is the last thing
# the boot writes to serial: poll for it directly. Waiting on a systemd
# "Reached target" line first made the gate sit out the whole timeout whenever
# that line never reached serial (Post-Testing E2E runs 44538634534 and
# 44604822618: 30m03s each against a 1800 s timeout, marker present).
echo "Waiting up to ${BOOT_TIMEOUT}s for ${EXPECT_MARKER}"
started=${SECONDS}
deadline=$((SECONDS + BOOT_TIMEOUT))
while :; do
  if grep -qaF "${EXPECT_MARKER}" "${SERIAL}"; then
    echo "PASS: ${SERIAL} observed ${EXPECT_MARKER} after $((SECONDS - started))s"
    exit 0
  fi
  grep -qa "Kernel panic" "${SERIAL}" && fail_with_serial "kernel panic during boot"
  kill -0 "${QEMU_PID}" 2>/dev/null \
    || fail_with_serial "QEMU exited after $((SECONDS - started))s before ${EXPECT_MARKER} appeared"
  (( SECONDS < deadline )) || break
  sleep "${POLL_SECONDS}"
done
fail_with_serial "${EXPECT_MARKER} never appeared on the serial console within ${BOOT_TIMEOUT}s"
