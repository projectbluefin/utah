# Desktop contract verifiers

The same verifier runs in the Containerfile and on demand, so a local image
or a CI artifact can be checked after the fact (recipe comment, `Justfile`,
`check-desktop-contract`):

- **In the image build** — the desktop RUN step ends with
  `utah-verify-desktop-contract /usr/share/utah/bluefin-desktop.toml`, after
  branding and services are configured; a contract failure fails the build.
  The extension verifier runs earlier in the same step.
  It also loads each `[media].decoders` element with `gst-inspect-1.0`; a
  missing element or a missing `gst-inspect-1.0` fails the build. `[gsettings]`
  checks that every schema override section resolves and reads the
  `[gsettings.values]` with `gsettings get` ([extension schemas](extension-schemas.md)).
- **On demand** — `just check-desktop-contract <ref>` (default
  `localhost/utah:testing`) podman-runs both verifiers inside an
  already-composed image: both verifiers and the desktop contract are
  bind-mounted read-only from the working tree under `/tmp`. The extension
  verifier uses installed mode to check the image's bundled extensions.
  Build-time helpers under `/usr/local/libexec` are unavailable after
  `clean-stage.sh` removes `/var/usrlocal` (the target of `/usr/local`), so
  on-demand checks must not depend on those helpers surviving cleanup.
- **Off-image** — `verify-desktop-contract.py --check` validates the contract
  TOML itself in source-only CI and is part of `just check`; it asserts
  nothing about any image.

## Verification

```bash
python3 scripts/verify-desktop-contract.py --check contracts/bluefin-desktop.toml
python3 scripts/verify-gnome-extensions.py --source
just check-desktop-contract localhost/utah:testing  # requires a locally built image
```
