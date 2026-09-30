#!/usr/bin/env bash
# Decide whether a tesseract transcript of the final desktop screenshot proves
# that fastfetch ran inside the installed session.
#
# Kept separate from luks-e2e.sh so the decision can be exercised against real
# transcripts (tests/test_iso_ci.py) without booting a VM.
#
# Two independent tokens must be present, because either alone lies:
#
#   1. The sentinel the test's .bashrc echoes after fastfetch. Matched as
#      E2E<sep>FASTFETCH rather than the full UTAH-E2E-FASTFETCH: OCR routinely
#      drops the first glyph of a line, and run 35374557822 read the sentinel as
#      "TAH-E2E-FASTFETCH" on a screenshot that plainly showed fastfetch.
#   2. A token only fastfetch's own body produces, so a sentinel left in
#      scrollback cannot pass the gate by itself. Either the "Kernel" field
#      label or the kernel version line it labels: Bluefin's fastfetch config
#      draws its labels as Nerd Font glyphs, which OCR does not read as text at
#      all, so requiring the label alone can never match.
#
# Fallback rule (#375): tesseract then substituted inside the fragment itself,
# reading "E2E" as "£26" on four consecutive flavors and runs while reading the
# "-FASTFETCH" suffix and the whole fastfetch body cleanly -- and identical
# sentinel pixels read clean the rest of the time, so the reader diverges on
# the same input. When the fragment fails, accept the suffix plus the kernel
# token plus a second body token only a rendered fastfetch produces. Three
# independent tokens keep the scrollback protection of the primary rule: a
# stale scrollback that contains all three contains a whole previous fastfetch,
# which is what the primary rule already accepts.
set -euo pipefail

OCR_TEXT="${1:?path to the tesseract transcript is required}"

[[ -s "${OCR_TEXT}" ]] || exit 1

if grep -qiE 'E2E[^A-Za-z0-9]?FASTFETCH' "${OCR_TEXT}" \
    && grep -qiE 'kernel|Linux[[:space:]]+[0-9]+\.[0-9]+' "${OCR_TEXT}"; then
    exit 0
fi

grep -qiE 'FASTFETCH' "${OCR_TEXT}" || exit 1
grep -qiE 'kernel|Linux[[:space:]]+[0-9]+\.[0-9]+' "${OCR_TEXT}" || exit 1
grep -qiE 'Mutter|Wayland|GNOME [0-9]+|ghostty|\(flatpak\)|bash [0-9]+\.[0-9]+' "${OCR_TEXT}" || exit 1
