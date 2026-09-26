ARG BASE_IMAGE=quay.io/hummingbird-community/bootc-os:latest@sha256:7ea735968c2543f51a975474b13b17bb8e110852045fd99bd13066179bf775f2
# The package factory publishes a complete, digest-addressable RPM repository.
# Keep this pin in Utah so an image build is reproducible and can be reviewed
# against the exact package set it consumes.
ARG PACKAGE_IMAGE=ghcr.io/projectbluefin/utah-packages
ARG PACKAGE_IMAGE_SHA=sha256:68810ae7300e87cb957a54a10365cb1da765f679781081dead5a07b7ced31174
# CI keeps PACKAGE_IMAGE_SHA pinned. PACKAGE_IMAGE_REF supports a local image
# in containers-storage, where no registry digest is available.
ARG PACKAGE_IMAGE_REF=${PACKAGE_IMAGE}@${PACKAGE_IMAGE_SHA}
ARG COMMON_IMAGE=ghcr.io/projectbluefin/common
ARG COMMON_IMAGE_SHA=sha256:4603e008ff9b81444fd763fbf58bff5d5b2efd1b79f348d94d8120a4b695c6ff
ARG BREW_IMAGE=ghcr.io/ublue-os/brew
ARG BREW_IMAGE_SHA=sha256:e9a72571b7644b6277f0638b6a3c5e497e265e1098ab91224567acbdeb8b74ea

FROM ${COMMON_IMAGE}@${COMMON_IMAGE_SHA} AS common
FROM ${BREW_IMAGE}@${BREW_IMAGE_SHA} AS brew
FROM ${PACKAGE_IMAGE_REF} AS packages
FROM ${BASE_IMAGE}

# Layer discipline, because it is where the build time goes.
#
# Every instruction below commits a layer, and committing a layer means walking
# the whole root filesystem to produce the diff. On the hosted runner that is
# about ten seconds per layer before the package transaction and forty seconds
# per layer after it, when /usr is several gigabytes. The eighteen one-file
# COPYs this used to open with cost three minutes on their own, for a few
# kilobytes of scripts. So sources are copied in as few instructions as the
# distinct origins allow, and small RUN steps are folded into their neighbours.
#
# The per-image build arguments (name, flavor, version, commit) are declared
# late, immediately before the first step that reads them, and the labels
# that quote them come last. A build argument is part of the cache key of
# every RUN declared after it, whether that RUN uses it or not -- so with
# VERSION declared at the top, the package transaction missed the registry
# layer cache on every commit, since VERSION carries the date and the commit.
# Declared here, nothing above the branding step ever sees them.

# Manifests, the desktop contract, and the repository definitions the package
# transaction reads. These, the pinned package image and the install script
# are the whole input to the expensive layer, so everything else waits its
# turn below them.
COPY packages/bluefin.toml packages/utah.toml contracts/bluefin-desktop.toml /usr/share/utah/
COPY packages/hummingbird.repo packages/nvidia-container.repo packages/utah-packages.repo /etc/yum.repos.d/
# Hummingbird signs its RPMs with Red Hat's release key 2 (fd431d51); the key
# lets packages/hummingbird.repo run with gpgcheck=1 here and in the live ISO
# build on top of this image.
COPY packages/RPM-GPG-KEY-redhat-release-2 /etc/pki/rpm-gpg/
# The package image is an RPM repository, not a runtime dependency. It is
# bind mounted into the two RUN steps that install from it and never copied
# into a layer: a COPY used to put the whole ~4 GB repository at
# /etc/utah-packages, nothing ever removed it, and it was two thirds of every
# published image and of every live ISO, whose squashfs holds the image (#128).
# Reproducibility still comes from the digest-pinned `packages` stage, which is
# the only source the package transaction can see -- that is what the old
# comment meant by "does not depend on a mutable Pages mirror." The mount is a
# BuildKit RUN --mount, so it costs no layer and leaves nothing on disk.
# One layer for all of Utah's scripts. They are staged under /tmp and installed
# by name in the RUN below, because a multi-source COPY cannot rename and
# every downstream path expects the utah- prefix.
COPY scripts/install-packages.py \
     scripts/verify-rpm-contract.py \
     scripts/build-gnome-extensions.sh \
     scripts/install-ogc-kernel.sh \
     scripts/install-nvidia.sh \
     scripts/clean-stage.sh \
     scripts/configure-services.sh \
     scripts/configure-branding.sh \
     scripts/verify-desktop-contract.py \
     scripts/verify-gnome-extensions.py \
     scripts/mirror-shim.sh \
     scripts/verify-efi-chain.sh \
     scripts/fix-home-labels.sh \
     /tmp/utah-scripts/
# Common publishes Bluefin artwork, desktop defaults, Brewfiles, and setup
# hooks in a separate profile from its shared system files. Both are required:
# copying only /system_files/shared leaves a functional GNOME desktop that is
# still visibly Hummingbird and has no default Flatpak set.
COPY --from=common /system_files/shared /tmp/utah-common
COPY --from=common /system_files/bluefin /tmp/utah-bluefin
COPY --from=brew /system_files /tmp/utah-brew
COPY system_files/shared /tmp/utah-local


RUN for pair in install-packages.py:utah-install-packages \
                verify-rpm-contract.py:utah-verify-rpm-contract \
                build-gnome-extensions.sh:utah-build-gnome-extensions \
                install-ogc-kernel.sh:utah-install-ogc-kernel \
                install-nvidia.sh:utah-install-nvidia \
                clean-stage.sh:utah-clean-stage \
                configure-services.sh:utah-configure-services \
                configure-branding.sh:utah-configure-branding \
                verify-desktop-contract.py:utah-verify-desktop-contract \
                verify-gnome-extensions.py:utah-verify-gnome-extensions \
                mirror-shim.sh:utah-mirror-shim \
                verify-efi-chain.sh:utah-verify-efi-chain \
                fix-home-labels.sh:utah-fix-home-labels; do \
      install -Dm 0755 "/tmp/utah-scripts/${pair%%:*}" "/usr/local/libexec/${pair##*:}" || exit 1; \
    done && \
    cp -a /tmp/utah-common/. / && \
    cp -a /tmp/utah-bluefin/. / && \
    cp -a /tmp/utah-brew/. / && \
    cp -a /tmp/utah-local/. / && \
    rm -rf /tmp/utah-scripts /tmp/utah-common /tmp/utah-bluefin /tmp/utah-brew /tmp/utah-local && \
    rm -f /etc/dconf/db/distro.d/05-bluefin-searchlight-extension
# The last line drops Common's settings for the Search Light extension. Utah no
# longer ships that extension: its shader code calls set_shader_source, which
# GNOME 51 removed, so it errored at load and failed the ISO end-to-end test.
# Settings for an extension the image does not carry are noise in dconf.

# This first check covers the flavor-independent contract only, which is why it
# pins IMAGE_FLAVOR=main. verify-rpm-contract.py reads IMAGE_FLAVOR from the
# environment, and the build sets it, so without this the nvidia flavors
# asserted here that nvidia-driver, nvidia-driver-cuda and
# nvidia-container-toolkit were installed -- several steps before
# utah-install-nvidia runs. The flavor-aware assertion is the second call,
# after the NVIDIA and OGC step.
#
# Utah keeps Bluefin's user-facing package contract.  Hummingbird supplies the
# bootable base; the pinned Utah package repository and Hummingbird's own
# repository supply the desktop and the rest.  Fedora repositories are never
# enabled at runtime -- they are bootstrap material for the package factory's
# buildroot, not a source of installed packages.
# A missing package is a build failure: silently skipping one would make parity
# claims meaningless.  The only exceptions are the packages listed under
# [unavailable] in packages/utah.toml, each of which carries a tracking issue.
#
# The package lists live in the manifests, not here.  When they were spelled
# out in this RUN as well, the two copies drifted and the contract check was
# asserting a different set than the install had asked for.
RUN --mount=type=bind,from=packages,source=/repository,target=/etc/utah-packages,ro \
    /usr/local/libexec/utah-install-packages \
      /usr/share/utah/bluefin.toml /usr/share/utah/utah.toml && \
    IMAGE_FLAVOR=main /usr/local/libexec/utah-verify-rpm-contract \
      /usr/share/utah/bluefin.toml /usr/share/utah/utah.toml && \
    /usr/local/libexec/utah-fix-home-labels && \
    DNF="$(command -v dnf5 || command -v dnf)" && \
    "$DNF" clean all && rm -rf /var/cache/libdnf5 /var/cache/dnf

# Per-image arguments. Nothing above this line may read them; see the note on
# layer discipline at the top.
ARG IMAGE_NAME=utah
# Canonical OS identity, distinct from the repository name a flavor publishes
# under. Always utah; never flavored.
ARG IMAGE_ID=utah
ARG IMAGE_FLAVOR=main
ARG IMAGE_VENDOR=projectbluefin
ARG VERSION=testing
ARG SHA_HEAD_SHORT=unknown
# Production images keep SSH closed; local VM diagnostics can opt in with
# ENABLE_SSHD=1, following tunaOS's debug-image convention.
ARG ENABLE_SSHD=0
# Renovate can update this pinned release independently of the base image.
# UUPD_SHA256 is the x86_64 tarball digest from the release's published
# uupd_<version>_checksums.txt; a mutable download that executed in the image
# is otherwise unverified. Both move together, so Renovate updates both.
ARG UUPD_VERSION=v1.4.0
ARG UUPD_SHA256=c7463f193cd35b92cde2ee05496501d6ac13808899bd26e17e027b7ee9ee1acc
# The two systemd units come from raw.githubusercontent at the same tag, and a
# git tag move changes what raw.* serves for them without touching the release
# asset the checksum above covers. uupd.service runs as root on a timer, so
# verify the units against their own digests too; all four ARGs move together.
ARG UUPD_SERVICE_SHA256=65dd2b64dcb6a9f77227612aa624ef17fe43b32eb835b51d7d22a22755dc21a8
ARG UUPD_TIMER_SHA256=bbb5f098ec33d047bdef571e0bc112364df157e0f92d73e0febab703c4a3c099

# Hummingbird defaults to a server preset and disables unlisted services.
# configure-services is the Utah equivalent of bluefin-lts's 40-services.sh:
# it applies the desktop service policy, login defaults, update policy, and
# removes the extension build toolchain before the final cleanup.
#
# The shim mirroring and the EFI chain guard at the end belong to the same
# step. The guard fails the build when shim has no packaged GRUB with a
# matching prefix beside it: that shipped once, and only the post-testing
# install e2e noticed, three days later (scripts/verify-efi-chain.sh).
#
# The shim mirroring at the end belongs to the same step: it was a layer of its
# own and cost forty seconds to commit a few megabytes. It lives in
# scripts/mirror-shim.sh rather than inline, because as a bare && chain a
# failure printed nothing at all -- see the comment at the top of that script.
RUN mkdir -p /tmp/uupd && \
    curl -fsSL "https://github.com/ublue-os/uupd/releases/download/${UUPD_VERSION}/uupd_Linux_x86_64.tar.gz" \
      -o /tmp/uupd/uupd_Linux_x86_64.tar.gz && \
    echo "${UUPD_SHA256}  /tmp/uupd/uupd_Linux_x86_64.tar.gz" | sha256sum --check --strict && \
    tar -xzf /tmp/uupd/uupd_Linux_x86_64.tar.gz -C /tmp/uupd && \
    curl -fsSL "https://raw.githubusercontent.com/ublue-os/uupd/${UUPD_VERSION}/uupd.service" \
      -o /tmp/uupd/uupd.service && \
    curl -fsSL "https://raw.githubusercontent.com/ublue-os/uupd/${UUPD_VERSION}/uupd.timer" \
      -o /tmp/uupd/uupd.timer && \
    echo "${UUPD_SERVICE_SHA256}  /tmp/uupd/uupd.service" | sha256sum --check --strict && \
    echo "${UUPD_TIMER_SHA256}  /tmp/uupd/uupd.timer" | sha256sum --check --strict && \
    /usr/local/libexec/utah-build-gnome-extensions && \
    /usr/local/libexec/utah-verify-gnome-extensions && \
    glib-compile-schemas /usr/share/glib-2.0/schemas && \
    ENABLE_SSHD="${ENABLE_SSHD}" /usr/local/libexec/utah-configure-services && \
    /usr/local/libexec/utah-configure-branding && \
    /usr/local/libexec/utah-verify-desktop-contract /usr/share/utah/bluefin-desktop.toml && \
    /usr/local/libexec/utah-mirror-shim && \
    /usr/local/libexec/utah-verify-efi-chain

# Dakota-compatible flavors: OGC is built and asserted before NVIDIA so the
# NVIDIA path can bind its module to the exact kernel tree it will boot.
RUN --mount=type=bind,from=packages,source=/repository,target=/etc/utah-packages,ro \
    case "${IMAGE_FLAVOR}" in \
      gaming|nvidia-gaming) /usr/local/libexec/utah-install-ogc-kernel ;; \
      main|nvidia) ;; \
      *) echo "Unknown Utah image flavor: ${IMAGE_FLAVOR}" >&2; exit 2 ;; \
    esac && \
    case "${IMAGE_FLAVOR}" in \
      nvidia|nvidia-gaming) /usr/local/libexec/utah-install-nvidia "${IMAGE_FLAVOR}" ;; \
      main|gaming) ;; \
    esac && \
    IMAGE_FLAVOR="${IMAGE_FLAVOR}" /usr/local/libexec/utah-verify-rpm-contract \
      /usr/share/utah/bluefin.toml /usr/share/utah/utah.toml && \
    # The package repository is now only ever bind mounted, so it is absent from
    # the committed image. Flip it disabled here -- the last step that installs
    # anything -- so later dnf calls on the image (the live ISO build's included)
    # do not fail on a file:// baseurl that no longer exists.
    sed -i 's/^enabled=1$/enabled=0/' /etc/yum.repos.d/utah-packages.repo \
      && grep -q '^enabled=0$' /etc/yum.repos.d/utah-packages.repo

# Everything above writes build-time residue that bootc lint rejects: dnf logs
# under /var/log, cockpit and dnf state under /run, and ~45 /var directories
# with no tmpfiles.d entry. This must run after the last package install, which
# is the NVIDIA and OGC step, not after the main transaction. The lint that
# checks the result runs in the same layer: nothing can change between the two.
# The home-label check runs first: clean-stage removes the utah-* helpers.
RUN /usr/local/libexec/utah-fix-home-labels --check && \
    /usr/local/libexec/utah-clean-stage && \
    bootc container lint --fatal-warnings --skip nonempty-boot

LABEL org.opencontainers.image.title="Utah"
LABEL org.opencontainers.image.description="A Hummingbird-based Bluefin GNOME workstation"
LABEL org.opencontainers.image.source="https://github.com/projectbluefin/utah"
LABEL org.opencontainers.image.vendor="${IMAGE_VENDOR}"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL containers.bootc=1

CMD ["/sbin/init"]
