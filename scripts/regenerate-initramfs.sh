#!/usr/bin/bash
# Rebuild the initramfs of the kernel bootc boots, after the last package
# install, and assert what it must carry (#564).
#
# The base image ships an initramfs generated before Utah installs anything.
# Nothing in Utah's build regenerated it, so the image booted an initrd with
# no early CPU microcode (linux-firmware's amd-ucode and microcode_ctl arrive
# later) and without the fido2, tpm2-tss, pkcs11 and pcsc modules Common's
# dracut.conf.d/90-passkeys-tpm.conf asks for, so a LUKS volume enrolled for
# TPM or passkey unlock could not open from the initrd. Bluefin LTS regenerates
# at the same point (build_scripts/26-packages-post.sh); this is the Utah
# equivalent.
#
# bootc boots the one kernel that has a vmlinuz under /usr/lib/modules, so
# that is the initramfs rebuilt here. The OGC kernel the gaming flavors carry
# is installed under /boot and is not touched.
set -euo pipefail

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

kernels=()
for dir in /usr/lib/modules/*/; do
    [ -f "${dir}vmlinuz" ] && kernels+=("$(basename "$dir")")
done
[ "${#kernels[@]}" -eq 1 ] \
    || fail "expected one bootc kernel under /usr/lib/modules, found: ${kernels[*]:-none}"
kver="${kernels[0]}"
image="/usr/lib/modules/${kver}/initramfs.img"

# No host-only: the image boots on hardware the build host knows nothing
# about. The remaining options (early_microcode, the bootc and ostree modules,
# Common's unlock modules) come from dracut.conf.d.
dracut --no-hostonly --reproducible --kver "$kver" --force "$image"

listing="$(lsinitrd "$image")"
grep -q '^Early CPIO image' <<<"$listing" \
    || fail "${image} has no early microcode cpio"
for vendor in AuthenticAMD GenuineIntel; do
    grep -q "kernel/x86/microcode/${vendor}.bin" <<<"$listing" \
        || fail "${image} carries no ${vendor} microcode"
done

modules="$(lsinitrd -m "$image")"
for module in bootc ostree fido2 tpm2-tss pkcs11 pcsc; do
    grep -qx "$module" <<<"$modules" \
        || fail "${image} is missing the ${module} dracut module"
done
echo "${image}: early microcode and unlock modules present"
