#!/usr/bin/env bash
# Live-boot gate for a tacklebox-built Utah ISO.
#
# Boots the ISO in QEMU with the serial console captured to a file, waits for
# the UTAH_LIVE_READY marker the live_customize step installs (#229), and
# fails if it never appears. A boot that stops before the desktop still reaches
# multi-user.target and runs the marker, so this proves the live root came all
# the way up -- not merely that QEMU started.
#
# Usage: tacklebox-boot-gate.sh ISO [EXPECT_MARKER]
set -euo pipefail

ISO="$(realpath "${1:?live ISO path is required}")"
EXPECT_MARKER="${2:-UTAH_LIVE_READY}"
WORK="${UTAH_ISO_BOOT_WORK:-/var/tmp/utah-tbx-boot}"
VM_RAM="${UTAH_ISO_BOOT_RAM:-6144}"
VM_CPUS="${UTAH_ISO_BOOT_CPUS:-2}"
BOOT_TIMEOUT="${UTAH_ISO_BOOT_TIMEOUT:-300}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

mkdir -p "${WORK}"
SERIAL="${WORK}/serial.log"
MONITOR="${WORK}/monitor.sock"
VARS="${WORK}/ovmf-vars.fd"
rm -f "${SERIAL}" "${MONITOR}"
: > "${SERIAL}"

QEMU="$(command -v qemu-system-x86_64 /usr/libexec/qemu-kvm 2>/dev/null | head -1)"
[[ -n "${QEMU}" ]] || { echo "qemu-system-x86_64 not found" >&2; exit 1; }
OVMF_CODE=""
for f in /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/OVMF/OVMF_CODE_4M.fd \
         /usr/share/OVMF/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd; do
  [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
done
[[ -n "${OVMF_CODE}" ]] || { echo "OVMF firmware not found" >&2; exit 1; }
OVMF_VARS_SRC=""
for f in /usr/share/edk2/ovmf/OVMF_VARS.fd /usr/share/OVMF/OVMF_VARS_4M.fd \
         /usr/share/OVMF/OVMF_VARS.fd /usr/share/ovmf/OVMF_VARS.fd; do
  [[ -f "$f" ]] && { OVMF_VARS_SRC="$f"; break; }
done
[[ -n "${OVMF_VARS_SRC}" ]] || { echo "OVMF variable store not found" >&2; exit 1; }
cp -f "${OVMF_VARS_SRC}" "${VARS}"

ACCEL="-accel kvm"
QEMU_CPU="host"
# TCG cannot emulate the host CPU model: qemu-system-x86_64 refuses
# '-cpu host' without KVM/HVF, so the fallback must switch both.
test -r /dev/kvm || { echo "No /dev/kvm; falling back to TCG (much slower)" >&2; ACCEL="-accel tcg,thread=multi"; QEMU_CPU="qemu64"; }

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
    -daemonize -pidfile "${WORK}/boot.pid"

# Wait for the boot to reach a point where the marker has run. The marker is a
# oneshot after the display manager; graphical.target is the earliest full
# convergence. Either way the marker is on the serial line by then.
echo "Waiting up to ${BOOT_TIMEOUT}s for ${EXPECT_MARKER}"
for _ in $(seq 1 "$((BOOT_TIMEOUT / 5))"); do
  if grep -aqi "Reached target .*Graphical" "${SERIAL}" 2>/dev/null \
     || grep -aqi "Reached target .*Multi-User" "${SERIAL}" 2>/dev/null; then
    break
  fi
  if grep -qa "Kernel panic" "${SERIAL}" 2>/dev/null; then
    echo "FAIL: kernel panic during boot" >&2; tail -60 "${SERIAL}" >&2; exit 1
  fi
  sleep 5
done

if grep -qa "${EXPECT_MARKER}" "${SERIAL}"; then
  echo "PASS: ${SERIAL} observed ${EXPECT_MARKER}"
else
  echo "FAIL: ${EXPECT_MARKER} never appeared on the serial console" >&2
  echo "--- units that failed ---" >&2
  sed 's/\x1b\[[0-9;:]*m//g' "${SERIAL}" 2>/dev/null \
    | grep -aE 'Failed to start|Failed to mount|Timed out waiting|emergency' \
    | tail -20 >&2 || true
  echo "--- last 60 serial lines ---" >&2
  tail -60 "${SERIAL}" >&2 || true
  python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect('${MONITOR}'); s.sendall(b'quit\n')" 2>/dev/null || true
  exit 1
fi
