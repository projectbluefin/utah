ARG BASE_IMAGE=quay.io/hummingbird-community/bootc-os:latest@sha256:c5539f9ed4d93aab6bd41e4f5aef8ab83055f3f9e855a47b69fadb7420d0d1df
# The package factory publishes a complete, digest-addressable RPM repository.
# Keep this pin in Utah so an image build is reproducible and can be reviewed
# against the exact package set it consumes.
ARG PACKAGE_IMAGE=ghcr.io/projectbluefin/utah-packages
ARG PACKAGE_IMAGE_SHA=sha256:2848c60d51fc6d75c3c89b246aad0e5ebf1fe84c5b7696203f02f14727bd158b
ARG COMMON_IMAGE=ghcr.io/projectbluefin/common
ARG COMMON_IMAGE_SHA=sha256:fb943c87866292fb74eb74610e9cd08a1a91fe42e763e28473f3f57cf18f26a5
ARG BREW_IMAGE=ghcr.io/ublue-os/brew
ARG BREW_IMAGE_SHA=sha256:8f952ae54585db9f855a306ef365e13609ed7c7944b12b823ba7d5ce8e1a145b

FROM ${COMMON_IMAGE}@${COMMON_IMAGE_SHA} AS common
FROM ${BREW_IMAGE}@${BREW_IMAGE_SHA} AS brew
FROM ${PACKAGE_IMAGE}@${PACKAGE_IMAGE_SHA} AS packages
FROM ${BASE_IMAGE}

ARG IMAGE_NAME=utah
ARG IMAGE_FLAVOR=main
ARG IMAGE_VENDOR=projectbluefin
ARG VERSION=testing
ARG SHA_HEAD_SHORT=unknown
# Production images keep SSH closed; local VM diagnostics can opt in with
# ENABLE_SSHD=1, following tunaOS's debug-image convention.
ARG ENABLE_SSHD=0
# Renovate can update this pinned release independently of the base image.
ARG UUPD_VERSION=v1.4.0

LABEL org.opencontainers.image.title="Utah"
LABEL org.opencontainers.image.description="A Hummingbird-based Bluefin GNOME workstation"
LABEL org.opencontainers.image.source="https://github.com/projectbluefin/utah"
LABEL org.opencontainers.image.vendor="${IMAGE_VENDOR}"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL containers.bootc=1

COPY packages/bluefin.toml /usr/share/utah/bluefin.toml
COPY packages/utah.toml /usr/share/utah/utah.toml
COPY packages/hummingbird.repo /etc/yum.repos.d/hummingbird.repo
COPY packages/nvidia-container.repo /etc/yum.repos.d/nvidia-container.repo
COPY packages/utah-packages.repo /etc/yum.repos.d/utah-packages.repo
# The package image is an RPM repository, not a runtime dependency. Its
# contents are intentionally copied into the image so the package transaction
# is reproducible and does not depend on a mutable Pages mirror.
COPY --from=packages /repository /etc/utah-packages
COPY scripts/install-packages.py /usr/local/libexec/utah-install-packages
COPY scripts/verify-rpm-contract.py /usr/local/libexec/utah-verify-rpm-contract
COPY scripts/build-gnome-extensions.sh /usr/local/libexec/utah-build-gnome-extensions
COPY scripts/install-ogc-kernel.sh /usr/local/libexec/utah-install-ogc-kernel
COPY scripts/install-nvidia.sh /usr/local/libexec/utah-install-nvidia
COPY scripts/clean-stage.sh /usr/local/libexec/utah-clean-stage
COPY scripts/configure-services.sh /usr/local/libexec/utah-configure-services
COPY scripts/configure-branding.sh /usr/local/libexec/utah-configure-branding
COPY scripts/verify-desktop-contract.py /usr/local/libexec/utah-verify-desktop-contract
COPY scripts/verify-gnome-extensions.py /usr/local/libexec/utah-verify-gnome-extensions
COPY contracts/bluefin-desktop.toml /usr/share/utah/bluefin-desktop.toml
# Common publishes Bluefin artwork, desktop defaults, Brewfiles, and setup
# hooks in a separate profile from its shared system files. Both are required:
# copying only /system_files/shared leaves a functional GNOME desktop that is
# still visibly Hummingbird and has no default Flatpak set.
COPY --from=common /system_files/shared /tmp/utah-common
COPY --from=common /system_files/bluefin /tmp/utah-bluefin
COPY --from=brew /system_files /tmp/utah-brew
COPY system_files/shared /tmp/utah-local

RUN chmod 0755 /usr/local/libexec/utah-install-packages /usr/local/libexec/utah-verify-rpm-contract /usr/local/libexec/utah-build-gnome-extensions /usr/local/libexec/utah-install-ogc-kernel /usr/local/libexec/utah-install-nvidia /usr/local/libexec/utah-clean-stage /usr/local/libexec/utah-configure-services /usr/local/libexec/utah-configure-branding /usr/local/libexec/utah-verify-desktop-contract /usr/local/libexec/utah-verify-gnome-extensions && \
    cp -a /tmp/utah-common/. / && \
    cp -a /tmp/utah-bluefin/. / && \
    cp -a /tmp/utah-brew/. / && \
    cp -a /tmp/utah-local/. / && \
    rm -rf /tmp/utah-common /tmp/utah-bluefin /tmp/utah-brew /tmp/utah-local

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
RUN /usr/local/libexec/utah-install-packages \
      /usr/share/utah/bluefin.toml /usr/share/utah/utah.toml && \
    IMAGE_FLAVOR=main /usr/local/libexec/utah-verify-rpm-contract \
      /usr/share/utah/bluefin.toml /usr/share/utah/utah.toml && \
    DNF="$(command -v dnf5 || command -v dnf)" && \
    "$DNF" clean all && rm -rf /var/cache/libdnf5 /var/cache/dnf

# Hummingbird defaults to a server preset and disables unlisted services.
# configure-services is the Utah equivalent of bluefin-lts's 40-services.sh:
# it applies the desktop service policy, login defaults, update policy, and
# removes the extension build toolchain before the final cleanup.
RUN mkdir -p /tmp/uupd && \
    curl -fsSL "https://github.com/ublue-os/uupd/releases/download/${UUPD_VERSION}/uupd_Linux_x86_64.tar.gz" \
      | tar -xzf - -C /tmp/uupd && \
    curl -fsSL "https://raw.githubusercontent.com/ublue-os/uupd/${UUPD_VERSION}/uupd.service" \
      -o /tmp/uupd/uupd.service && \
    curl -fsSL "https://raw.githubusercontent.com/ublue-os/uupd/${UUPD_VERSION}/uupd.timer" \
      -o /tmp/uupd/uupd.timer && \
    /usr/local/libexec/utah-build-gnome-extensions && \
    /usr/local/libexec/utah-verify-gnome-extensions && \
    glib-compile-schemas /usr/share/glib-2.0/schemas && \
    ENABLE_SSHD="${ENABLE_SSHD}" /usr/local/libexec/utah-configure-services && \
    /usr/local/libexec/utah-configure-branding && \
    /usr/local/libexec/utah-verify-desktop-contract /usr/share/utah/bluefin-desktop.toml

# Fedora's shim package stages its EFI payload under bootupd's update tree,
# while bootupd discovers image-provided EFI components under /usr/lib/efi.
# Mirror the signed payload into bootupd's component layout so bootc can create
# a generic disk image without depending on the build host's ESP.
RUN shim_version="$(rpm -q --qf '%{VERSION}-%{RELEASE}' shim-x64)" && \
    test -d /usr/lib/bootupd/updates/EFI/fedora && \
    install -d "/usr/lib/efi/shim/${shim_version}/EFI/fedora" && \
    cp -a /usr/lib/bootupd/updates/EFI/fedora/. "/usr/lib/efi/shim/${shim_version}/EFI/fedora/"

# Dakota-compatible flavors: OGC is built and asserted before NVIDIA so the
# NVIDIA path can bind its module to the exact kernel tree it will boot.
RUN case "${IMAGE_FLAVOR}" in \
      gaming|nvidia-gaming) /usr/local/libexec/utah-install-ogc-kernel ;; \
      main|nvidia) ;; \
      *) echo "Unknown Utah image flavor: ${IMAGE_FLAVOR}" >&2; exit 2 ;; \
    esac && \
    case "${IMAGE_FLAVOR}" in \
      nvidia|nvidia-gaming) /usr/local/libexec/utah-install-nvidia "${IMAGE_FLAVOR}" ;; \
      main|gaming) ;; \
    esac && \
    IMAGE_FLAVOR="${IMAGE_FLAVOR}" /usr/local/libexec/utah-verify-rpm-contract \
      /usr/share/utah/bluefin.toml /usr/share/utah/utah.toml

# Everything above writes build-time residue that bootc lint rejects: dnf logs
# under /var/log, cockpit and dnf state under /run, and ~45 /var directories
# with no tmpfiles.d entry. This must run after the last package install, which
# is the NVIDIA and OGC step, not after the main transaction.
RUN /usr/local/libexec/utah-clean-stage

RUN bootc container lint --fatal-warnings --skip nonempty-boot

CMD ["/sbin/init"]
