#!/usr/bin/bash
# Assert, from inside a booted Utah system, the things a container cannot show.
#
# Everything the image *contains* is already checked at build time:
# verify-rpm-contract, verify-desktop-contract, verify-gnome-extensions and
# bootc lint all run in the Containerfile. None of them can tell you whether the
# result boots, whether the kernel that boots is the one the flavor asked for,
# or whether the features that kernel exists for are actually live. That is what
# this checks, and it runs once on the first boot of a throwaway VM.
#
# It prints its findings to the console and ends with a single marker line the
# harness greps for. Anything unexpected exits non-zero without the marker, so a
# missing marker and a failure are the same thing to the caller -- a VM that
# hung or panicked cannot look like a pass.
set -uo pipefail

# configure-branding.sh records this; it is the image's own statement of which
# flavor it is, so the checks below test the image against itself rather than
# against something the harness passed in and could get wrong.
IMAGE_INFO=/usr/share/ublue-os/image-info.json
FLAVOR="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image-flavor"])' "$IMAGE_INFO" 2>/dev/null || echo unknown)"
KERNEL="$(uname -r)"
failures=0

note() { printf 'utah-boot: %s\n' "$*"; }
fail() { printf 'utah-boot: FAIL %s\n' "$*"; failures=$((failures + 1)); }

# systemd first: a degraded system may still start gdm, and the failed units
# are the most useful thing to print when something below goes wrong.
state="$(systemctl is-system-running 2>/dev/null || true)"
note "systemd state: ${state}"
case "$state" in
  running) ;;
  degraded)
    fail "systemd is degraded"
    systemctl --failed --no-legend --no-pager || true
    ;;
  *) fail "systemd never settled (${state})" ;;
esac

# The desktop. gdm is what 85-utah-desktop.preset enables and what a user sees
# first; graphical.target alone would be satisfied without it.
for _ in $(seq 1 60); do
  [ "$(systemctl is-active gdm.service 2>/dev/null)" = active ] && break
  sleep 5
done
if [ "$(systemctl is-active gdm.service 2>/dev/null)" = active ]; then
  note "gdm.service is active"
else
  fail "gdm.service did not become active"
  systemctl status gdm.service --no-pager -l 2>&1 | head -40 || true
fi

# Identity. The flavored repository name must not have leaked into the OS
# identity: every flavor presents itself as utah.
for pair in IMAGE_ID=utah VARIANT_ID=utah; do
  key="${pair%%=*}"; want="${pair#*=}"
  got="$(. /usr/lib/os-release 2>/dev/null; eval "printf '%s' \"\${$key:-}\"")"
  [ "$got" = "$want" ] && note "os-release ${key}=${got}" || fail "os-release ${key} is '${got}', expected '${want}'"
done

ogc_release=""
[ -f /usr/lib/utah/ogc-kernel-release ] && ogc_release="$(cat /usr/lib/utah/ogc-kernel-release)"

case "$FLAVOR" in
  gaming|nvidia-gaming)
    # The OGC kernel is the only reason these flavors exist, so booting the
    # stock kernel instead is a silent, total failure of the flavor.
    if [ -z "$ogc_release" ]; then
      fail "no OGC kernel release recorded in the image"
    elif [ "$KERNEL" = "$ogc_release" ]; then
      note "booted the OGC kernel ${KERNEL}"
    else
      fail "booted ${KERNEL}, expected the OGC kernel ${ogc_release}"
    fi
    # sched_ext and binderfs are what the OGC kernel is built for. The config
    # was asserted when it was compiled; these are the runtime proof.
    [ -d /sys/kernel/sched_ext ] && note "sched_ext is live" || fail "no /sys/kernel/sched_ext"
    grep -qw binder /proc/filesystems && note "binderfs is registered" \
      || fail "binderfs is not in /proc/filesystems"
    ;;
  main|nvidia)
    if [ -n "$ogc_release" ] && [ "$KERNEL" = "$ogc_release" ]; then
      fail "booted the OGC kernel on the ${FLAVOR} flavor"
    else
      note "booted the stock kernel ${KERNEL}"
    fi
    ;;
  *) fail "unknown image flavor '${FLAVOR}'" ;;
esac

case "$FLAVOR" in
  nvidia|nvidia-gaming)
    # Built against the kernel that is now running, which is the thing the
    # build could only assume. Loading it needs a GPU the runner has not got,
    # so this asserts the module is present and readable for this kernel.
    module="/usr/lib/modules/${KERNEL}/extra/nvidia/nvidia.ko"
    if [ -f "$module" ]; then
      note "nvidia.ko present for ${KERNEL}"
    else
      fail "no nvidia.ko for the booted kernel ${KERNEL}"
      find "/usr/lib/modules/${KERNEL}/extra" -maxdepth 3 2>/dev/null || true
    fi
    modinfo -F version nvidia >/dev/null 2>&1 \
      && note "modinfo resolves nvidia $(modinfo -F version nvidia 2>/dev/null)" \
      || fail "modinfo cannot resolve the nvidia module for ${KERNEL}"
    grep -q 'blacklist nouveau' /usr/lib/modprobe.d/00-nouveau-blacklist.conf 2>/dev/null \
      && note "nouveau is blacklisted" || fail "nouveau blacklist is missing"
    ;;
esac

if [ "$failures" -ne 0 ]; then
  printf 'utah-boot: %s check(s) failed on %s\n' "$failures" "$FLAVOR"
  exit 1
fi
# The harness greps for exactly this. Keep it on one line and last.
printf 'UTAH_BOOT_OK flavor=%s kernel=%s\n' "$FLAVOR" "$KERNEL"
