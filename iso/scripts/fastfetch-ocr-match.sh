#!/usr/bin/env bash
# Decide whether a tesseract transcript of the final desktop screenshot proves
# that fastfetch ran inside the installed session.
#
# Kept separate from the E2E harness (iso/raw/raw_e2e.py) so the decision can be
# exercised against real transcripts (tests/test_iso_ci_boot_gates.py) without
# booting a VM.
#
# Two independent tokens must be present, because either alone lies:
#
#   1. The sentinel the test's .bashrc echoes after fastfetch. Matched as
#      E2E<sep>FASTFETCH rather than the full UTAH-E2E-FASTFETCH: OCR routinely
#      drops the first glyph of a line, and run 35374557822 read the sentinel as
#      "TAH-E2E-FASTFETCH" on a screenshot that plainly showed fastfetch.
#   2. A token only fastfetch's own body produces, so a sentinel left in
#      scrollback cannot pass the gate by itself: the kernel line or another
#      field fastfetch prints (desktop, window manager, terminal, shell,
#      packages). Bluefin's fastfetch config draws its labels as Nerd Font
#      glyphs, which OCR does not read as text at all, so the field values are
#      what can match. No single field is required, because tesseract drops
#      whole fields, and the kernel line is the one it loses most:
#      "Linux 7.3.3-200.1.utah1.fc44.x86_64" came back as
#      "Linu 200.1.utah1.fc4:" or not at all, failing seven straight
#      post-testing runs (44691397660 to 44751145611) on screenshots that
#      plainly showed fastfetch. Run 45636142672 lost every field below the
#      hardware block as well, so the build date ("Forged on") and the machine
#      model ("KVM/QEMU"), which nothing else on screen prints, count too.
#
# Fallback rule (#375): tesseract then substituted inside the fragment itself,
# reading "E2E" as "£26" on four consecutive flavors and runs while reading the
# "-FASTFETCH" suffix and the whole fastfetch body cleanly -- and identical
# sentinel pixels read clean the rest of the time, so the reader diverges on
# the same input. When the fragment fails, accept the suffix plus the kernel
# token plus a second body token. Three independent tokens keep the scrollback
# protection of the primary rule: a stale scrollback that contains all three
# contains a whole previous fastfetch, which is what the primary rule already
# accepts.
set -euo pipefail

OCR_TEXT="${1:?path to the tesseract transcript is required}"

[[ -s "${OCR_TEXT}" ]] || exit 1

KERNEL='kernel|Linux[[:space:]]+[0-9]+\.[0-9]+'
BODY='Mutter|Wayland|GNOME [0-9]+|ghostty|\(flatpak\)|bash [0-9]+\.[0-9]+|Forged on|KVM/QEMU'

if grep -qiE 'E2E[^A-Za-z0-9]?FASTFETCH' "${OCR_TEXT}" \
    && grep -qiE "${KERNEL}|${BODY}" "${OCR_TEXT}"; then
    exit 0
fi

grep -qiE 'FASTFETCH' "${OCR_TEXT}" || exit 1
grep -qiE "${KERNEL}" "${OCR_TEXT}" || exit 1
grep -qiE "${BODY}" "${OCR_TEXT}" || exit 1
