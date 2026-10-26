# Image details in fastfetch

`configure-services.sh` patches Common's `/usr/bin/ublue-image-info.sh`.
It removes the legacy rpm-ostree lock glyph (#931): Hummingbird does not
ship rpm-ostree, and that probe does not describe bootc signature verification.
The removal is repeatable and fails the build if an unfamiliar rpm-ostree
call remains, so a changed upstream script requires review.

Preserve the boot-time reference snapshot and baked tag fallback when
updating this patch. `tests/test_image_info_patch.py` exercises these paths
without a composed image. Never suppress fastfetch stderr to clean up a
screenshot. Historical verification screenshots remain evidence of their
run; the next successful E2E capture refreshes the README hero.
