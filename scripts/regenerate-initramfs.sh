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
# bootc deploys kernels from /usr/lib/modules/<release>/vmlinuz. Promote the
# gaming flavor's OGC kernel from /boot into that layout before rebuilding;
# retaining the base vmlinuz would silently boot the base instead of OGC.
set -euo pipefail

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

kver="$(/usr/local/libexec/utah-live-kernel / --select-bootc --promote-ogc)"
image="/usr/lib/modules/${kver}/initramfs.img"

# No host-only: the image boots on hardware the build host knows nothing
# about. The remaining options (early_microcode, the bootc and ostree modules,
# Common's unlock modules) come from dracut.conf.d. Compress with zstd to keep
# the installed image's initramfs within the layer and offline ISO budget (#740).
dracut --no-hostonly --reproducible --zstd --kver "$kver" --force "$image"

listing="$(lsinitrd "$image")"
grep -q '^Early CPIO image' <<<"$listing" \
    || fail "${image} has no early microcode cpio"
for vendor in AuthenticAMD GenuineIntel; do
    grep -q "kernel/x86/microcode/${vendor}.bin" <<<"$listing" \
        || fail "${image} carries no ${vendor} microcode"
done

modules="$(lsinitrd -m "$image")"
for module in bootc ostree fido2 tpm2-tss pkcs11 pcsc plymouth; do
    grep -qx "$module" <<<"$modules" \
        || fail "${image} is missing the ${module} dracut module"
done
echo "${image}: early microcode and unlock modules present"

# The splash and the LUKS passphrase prompt are drawn by the plymouthd inside
# this initramfs, from the theme dracut copied in, not from the image's
# /usr/share/plymouth (#461). Check the copy: the default theme, the plugin its
# ModuleName names, the renderers, the label plugin and font that draw the
# prompt's text, and Bluefin's watermark byte for byte. A missing watermark is a
# working but unbranded splash, which no earlier gate noticed.
# PLYMOUTH_SYSROOT (plymouth-populate-initrd's own name) points the image side
# at another tree; builds leave it unset.
sysroot="${PLYMOUTH_SYSROOT:-}"
# lsinitrd lists one entry per line, path last, or "path -> target" for links
# (dracut installs Plymouth.ttf as a link to the real font).
in_initramfs() {
    awk -v want="$1" '{ path = (NF >= 3 && $(NF - 1) == "->") ? $(NF - 2) : $NF }
        path ~ ("^" want "$") { found = 1 } END { exit !found }' <<<"$listing"
}
theme="$(plymouth-set-default-theme)"
theme_file="/usr/share/plymouth/themes/${theme}/${theme}.plymouth"
[[ -f "${sysroot}${theme_file}" ]] || fail "the default Plymouth theme ${theme} has no ${theme_file}"
in_initramfs "${theme_file#/}" || fail "${image} lacks the ${theme} Plymouth theme"
plugin="$(sed -n 's/^ModuleName=//p' "${sysroot}${theme_file}" | head -n 1)"
for so in "${plugin}" renderers/drm renderers/frame-buffer 'label-(pango|freetype)'; do
    in_initramfs "usr/lib(64)?/plymouth/${so}[.]so" \
        || fail "${image} lacks the Plymouth ${so} plugin"
done
in_initramfs "usr/share/fonts/Plymouth[.]ttf" \
    || fail "${image} lacks Plymouth's font; the passphrase prompt would have no text"
image_dir="$(realpath -m "$(sed -n 's/^ImageDir=//p' "${sysroot}${theme_file}" | head -n 1)")"
watermark="${image_dir}/watermark.png"
[[ -f "${sysroot}${watermark}" ]] \
    || fail "the ${theme} theme has no ${watermark}: the splash would be unbranded"
want="$(sha256sum <"${sysroot}${watermark}")"
got="$(lsinitrd -f "${watermark#/}" "$image" | sha256sum)"
[[ "$got" == "$want" ]] || fail "${image} carries a different ${watermark} than the image"
echo "${image}: Plymouth ${theme} theme (${plugin}) and its watermark present"
