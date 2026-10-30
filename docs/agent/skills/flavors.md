# Image flavors

Read `flavors.json` beside this bundle's entry point for the build's flavor
set. `main` uses the distribution kernel; `nvidia` adds NVIDIA drivers;
`gaming` uses the OGC kernel; `nvidia-gaming` combines the latter two.
Read `/usr/share/ublue-os/image-info.json`, `/etc/os-release`, `uname -r`,
and `bootc status` to identify the actual booted image and kernel.

A staged image may differ from the booted image. Diagnose the booted deployment
before changing streams or flavors. Switching images changes the next boot
and needs the owner's agreement. Adding or retiring a flavor is checkout work:
edit `config/flavors.json`, then run the repository's checks and image tests.
