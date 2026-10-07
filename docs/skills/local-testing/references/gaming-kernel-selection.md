# Gaming kernel selection

OGC's version can sort below Hummingbird's base kernel. Module-directory sort
order and `/boot/vmlinuz` therefore cannot establish what bootc installs.
Before the final initramfs rebuild, promote OGC into its bootc module-directory
layout and remove other kernels' vmlinuz/initramfs boot files while retaining
module trees. Require exactly one bootc kernel and select that same release
for live dracut and ISO assembly. Its final dracut rebuild supplies microcode
and unlock modules for the kernel actually deployed.

The source kernel must exist and be nonempty before removing base boot files.
Keep the cached archive format and kernel-cache inputs unchanged: this is a
runtime layout repair, and does not require recompiling a verified OGC cache.
The installed VM must compare uname to the OGC release receipt; an ISO that
installs successfully but boots the base kernel does not prove gaming-flavor
acceptance. QEMU cannot verify a physical NVIDIA GPU or Secure Boot key.
