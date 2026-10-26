# Packages and parity

Utah composes Hummingbird's `os-minimal` base with the Bluefin package
contract through the Utah package factory. `/usr/share/utah/bluefin.toml`
is the mirrored Bluefin manifest; `/usr/share/utah/utah.toml` contains Utah's
additions, `[parity]` provisions, and `[unavailable]` exceptions with tracking
issues. `/usr/share/utah/package-origins.json` and `package-origins.txt`
record package origins. Query the machine with `rpm -qa` and `rpm -qf PATH`.

Only Hummingbird's `public-hummingbird-x86_64-rpms` repository is enabled at
runtime. The package factory's build-time repository is disabled because its
mounted contents are absent. Fedora repositories are never enabled at runtime.
`dnf` therefore cannot resolve every package used to build the image.
Do not enable Fedora repositories or install packages into the read-only root
as a repair. Use Homebrew for user tools or rebuild the image for OS changes.

Checkout work: update `packages/utah.toml` or the package factory, preserve
Bluefin's byte-for-byte manifest, and keep tracking issues for unavailable
packages. Build, parity and repository checks require a checkout and network;
they are not commands to run from this installed bundle.
