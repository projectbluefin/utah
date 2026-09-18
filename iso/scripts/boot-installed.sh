#!/usr/bin/env bash
# Boot the disk `just luks-test` installed, with a console you can drive.
#
# The end-to-end test proves the system works and then throws the machine
# away. This keeps it: same disk, same firmware variables, but a VNC display
# and an optional browser console so the result can be used rather than only
# asserted about.
#
# The disk is opened through a qcow2 overlay, so anything done here is
# discarded on the next run and the verified artifact stays as the test left
# it.
#
# The root volume is encrypted. Plymouth asks for the passphrase on the
# console before anything else happens.
set -euo pipefail

WORK="${UTAH_E2E_WORK:-/var/tmp/utah-luks-e2e}"
BASE="${WORK}/install.qcow2"
VARS_SRC="${WORK}/ovmf-vars-installed.fd"
OVERLAY="${WORK}/try.qcow2"
VARS="${WORK}/try-vars.fd"
VNC_DISPLAY="${UTAH_TRY_VNC:-1}"
VNC_PORT=$((5900 + VNC_DISPLAY))
WEB_PORT="${UTAH_TRY_WEB_PORT:-8080}"
SSH_PORT="${UTAH_TRY_SSH_PORT:-2299}"
NOVNC_IMAGE="${UTAH_TRY_NOVNC_IMAGE:-docker.io/geek1011/easy-novnc:latest}"

[[ -f "${BASE}" ]] || {
    echo "No installed disk at ${BASE} -- run 'just luks-test' first." >&2
    exit 1
}
# The firmware variables matter as much as the disk: the installer writes an
# NVRAM boot entry and this ESP has no \EFI\BOOT\BOOTX64.EFI fallback, so a
# fresh variable store boots to the EFI shell instead of to Utah.
[[ -f "${VARS_SRC}" ]] || {
    echo "No firmware variables at ${VARS_SRC} -- rerun 'just luks-test'." >&2
    exit 1
}

QEMU="$(command -v qemu-system-x86_64 /usr/libexec/qemu-kvm 2>/dev/null | head -1)"
[[ -n "${QEMU}" ]] || { echo "qemu-system-x86_64 not found" >&2; exit 1; }
OVMF_CODE=""
for f in /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/OVMF/OVMF_CODE_4M.fd \
         /usr/share/OVMF/OVMF_CODE.fd \
         /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd; do
    [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
done
[[ -n "${OVMF_CODE}" ]] || { echo "OVMF firmware not found" >&2; exit 1; }

if [[ -f "${WORK}/try.pid" ]] && kill -0 "$(cat "${WORK}/try.pid")" 2>/dev/null; then
    echo "Already running (pid $(cat "${WORK}/try.pid")). Stop it with:"
    echo "  kill \$(cat ${WORK}/try.pid)"
    exit 0
fi

rm -f "${OVERLAY}"
qemu-img create -f qcow2 -b "${BASE}" -F qcow2 "${OVERLAY}" >/dev/null
cp -f "${VARS_SRC}" "${VARS}"

"${QEMU}" -name utah-try \
    -machine q35 -cpu host -m 8192 -smp 4 -accel kvm \
    -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
    -drive "if=pflash,format=raw,file=${VARS}" \
    -drive "if=none,id=disk,file=${OVERLAY},format=qcow2" \
    -device virtio-blk-pci,drive=disk \
    -netdev "user,id=net0,hostfwd=tcp::${SSH_PORT}-:22" \
    -device virtio-net-pci,netdev=net0 \
    -vnc "127.0.0.1:${VNC_DISPLAY}" \
    -monitor "unix:${WORK}/try-monitor.sock,server,nowait" \
    -serial "file:${WORK}/try-serial.log" \
    -daemonize -pidfile "${WORK}/try.pid"

echo "Utah is booting."
echo
echo "  VNC        vnc://127.0.0.1:${VNC_PORT}"

# A browser console is nicer than asking for a VNC client, but it is a
# convenience: if the proxy image cannot be fetched, the VNC port above is
# still there and the VM is unaffected.
if command -v podman >/dev/null 2>&1; then
    podman rm -f utah-novnc >/dev/null 2>&1 || true
    if podman run -d --name utah-novnc --network host "${NOVNC_IMAGE}" \
        --addr "127.0.0.1:${WEB_PORT}" --host 127.0.0.1 --port "${VNC_PORT}" \
        --no-url-password >/dev/null 2>&1; then
        echo "  Browser    http://127.0.0.1:${WEB_PORT}"
        # Open it. The point of a browser console is not having to go and find
        # it. UTAH_TRY_OPEN=0 for a headless host, where xdg-open has nothing
        # to open and would just print an error.
        if [[ "${UTAH_TRY_OPEN:-1}" == "1" ]] && command -v xdg-open >/dev/null 2>&1; then
            setsid xdg-open "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1 &
        fi
    else
        echo "  (browser console unavailable: could not start ${NOVNC_IMAGE})"
    fi
fi

cat <<EOF

  SSH        ssh -p ${SSH_PORT} utahtest@127.0.0.1
  Serial     ${WORK}/try-serial.log

The root volume is encrypted, so Plymouth asks for a passphrase first.
Defaults from the test, overridable there:

  passphrase   testpassphrase
  user         utahtest / utahtest

Stop it with:  kill \$(cat ${WORK}/try.pid) && podman rm -f utah-novnc
EOF
