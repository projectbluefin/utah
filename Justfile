repo_organization := env_var_or_default("REPO_ORGANIZATION", "projectbluefin")
image := "utah"
kernel_cache_image := "utah-kernel-cache"
base_dir := env_var_or_default("BASE_DIR", "output")
vm_ram := env_var_or_default("VM_RAM", "8192")
vm_cpus := env_var_or_default("VM_CPUS", "4")

default:
    @just --list

# Host-side unit tests for the helper scripts under scripts/. No image is
# needed, so they run inside `just check` rather than waiting for a build to
# fail on a wrong matrix.
#
# Discovery is delegated to tests/run_suite.py, which runs every directory
# under tests/ that holds test modules. Bare `unittest discover` rooted at
# tests/ skipped subdirectories such as tests/unit/ silently -- it reported
# OK whether the tests there passed, failed, or never ran.
#
# The third-party modules the suite needs are declared in
# tests/requirements.txt, not installed silently here. A quiet `pip install ||
# true` hid its own failure: the modules stayed missing and the suite reported
# 46 errors that read like regressions instead of one message naming the
# dependency.
test:
    #!/usr/bin/env bash
    set -euo pipefail
    missing=()
    for module in yaml jsonschema; do
        python3 -c "import ${module}" 2>/dev/null || missing+=("${module}")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        echo "host test dependencies missing: ${missing[*]}" >&2
        echo "they are declared in tests/requirements.txt; install them with:" >&2
        echo "    pip install -r tests/requirements.txt" >&2
        exit 1
    fi
    python3 tests/run_suite.py

check:
    #!/usr/bin/env bash
    set -euo pipefail
    # Every tracked shell and Python script is syntax-checked by enumeration,
    # not by a list maintained here by hand. That list had fallen six scripts
    # behind the tree -- including scripts/resolve-e2e-inputs.py, which decides
    # the E2E matrix, and iso/scripts/live-kernel.py, which runs mid-ISO-build
    # -- so a parse error in them surfaced minutes into a build instead of here.
    python3 scripts/check-script-syntax.py
    # The "definition of green" contract: every declared blocking gate must
    # really block (see .github/green-criteria.yml). Fails before a build if a
    # gate is missing, skippable, or stale.
    python3 scripts/green_contract.py --repo .
    test -f .github/green-criteria.yml
    test -f Containerfile
    test -f packages/base.toml
    test -f packages/utah.toml
    test -f packages/utah-packages.repo
    test -f system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    test -f system_files/shared/usr/lib/systemd/system/bootc-unified-storage.service.d/10-utah-local-test.conf
    # Enable-by-default as on Dakota (#1233): the catch-all that outranks
    # Hummingbird's 99-default-disable, applied at build time.
    grep -qx 'enable \*' system_files/shared/usr/lib/systemd/system-preset/98-utah-enable-default.preset
    grep -qx 'systemctl preset-all' scripts/configure-services.sh
    grep -q 'disable bootc-fetch-apply-updates.timer' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -q 'disable bootc-fetch-apply-updates.service' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -q 'bootc-fetch-apply-updates.timer' scripts/configure-services.sh
    # The serial getty is a Hummingbird server leftover that spams the journal
    # on machines without a serial port; the mask and the preset must both
    # survive, and the contract has to declare the mask. See #103.
    grep -q 'serial-getty@ttyS0.service' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -q 'systemctl mask serial-getty@ttyS0.service' scripts/configure-services.sh
    grep -q 'ln -sf /dev/null /usr/lib/systemd/system/serial-getty@ttyS0.service' scripts/configure-services.sh
    grep -q 'serial-getty@ttyS0.service' contracts/bluefin-desktop.toml
    test -f scripts/configure-services.sh
    test -f scripts/configure-branding.sh
    test -f scripts/verify-desktop-contract.py
    test -f scripts/verify-gnome-extensions.py
    test -f scripts/mirror-shim.sh
    test -f scripts/install-v4l2loopback.sh
    test -f scripts/pin-build-mtimes.py
    test -f scripts/podman-epoch-args.sh
    test -f scripts/check-reproducible-build.sh
    test -f packages/RPM-GPG-KEY-fedora-44-primary
    test -f contracts/bluefin-desktop.toml
    # The reusable image workflow checks out this repository without
    # submodules. Populate them here before validating the source contract;
    # otherwise CI reports every extension as missing while a developer clone
    # (or the contract workflow's recursive checkout) passes.
    git submodule update --init --recursive
    python3 scripts/verify-desktop-contract.py --check contracts/bluefin-desktop.toml
    python3 scripts/verify-gnome-extensions.py --source
    # Every Dakota item Utah lacks must be triaged (baselines/triage.toml).
    python3 scripts/image-baseline.py check
    grep -q '/system_files/bluefin' Containerfile
    grep -q '"flatpak-preinstall.service"' contracts/bluefin-desktop.toml
    grep -q 'flathub.flatpakrepo' scripts/configure-services.sh
    # The install path is the gnome-setup medium (iso/raw, #1569).
    test -f iso/raw/Containerfile
    test -f iso/raw/build-raw-image.sh
    test -f iso/raw/raw_e2e.py
    grep -q 'ENABLE_SSHD' Containerfile
    grep -q 'ENABLE_SSHD="${ENABLE_SSHD:-0}"' Justfile
    grep -q 'ARG PACKAGE_IMAGE_SHA=' Containerfile
    grep -q 'ARG PACKAGE_IMAGE_REF=' Containerfile
    grep -q -- '--mount=type=bind,from=packages,source=/repository,target=/etc/utah-packages,ro' Containerfile
    # The package repository is bind mounted, never committed; a COPY would ship
    # the whole ~4 GB RPM repository in every image and of every ISO (#128).
    # `! cmd` is exempt from errexit, so a bare `! grep` neither stops this
    # script nor changes its exit status unless it happens to be the last line
    # of the recipe: it reads as a gate and enforces nothing. Use an explicit if.
    if grep -q 'COPY --from=packages' Containerfile; then
      echo 'Containerfile must bind-mount the package repository, not COPY it' >&2
      exit 1
    fi
    test -f packages/RPM-GPG-KEY-redhat-release-2
    # Every executable release asset fetched during composition must be pinned
    # and verified; no build may resolve a mutable latest release.
    python3 scripts/check-download-integrity.py
    # Reject unpinned Python installs before running the rest of the suite.
    python3 tests/test_python_install_pins.py
    # The status page's package grid is generated from the manifests; a stale
    # committed copy would publish a list the image no longer installs.
    python3 scripts/generate-site-data.py --check
    # README.md and docs/skills/package-contract.md quote the same package
    # counts by hand; catch it before the manifests move and the prose does not.
    python3 scripts/check-doc-counts.py
    python3 scripts/install-packages.py --check --repos-dir packages packages/base.toml
    python3 scripts/verify-rpm-contract.py --check packages/base.toml
    # run the host-side unit suite (tests/test_*.py) via its dedicated recipe
    just test
    grep -qE 'reusable-build\.yml@(v1|[0-9a-f]{40} # v1)$' .github/workflows/build.yml
    test -f Containerfile.kernel
    # The cache image must be built from the same base the image is, or the
    # prebuilt NVIDIA module would be linked against a kernel this image never
    # boots.  Two literals, one invariant, so assert it rather than trust it.
    diff <(grep -m1 '^ARG BASE_IMAGE=' Containerfile) \
         <(grep -m1 '^ARG BASE_IMAGE=' Containerfile.kernel)
    python3 scripts/flavors.py list >/dev/null
    python3 scripts/check_workflow_outputs.py
    bash scripts/check-skill-frontmatter.sh
    bash scripts/check-skill-index.sh
    python3 scripts/generate_skill_index.py --check
    python3 scripts/generate_agent_context.py --check
    # Neither workflows nor the Justfile may carry their own copy of the flavor
    # list or flavored image names. That drift is what config/flavors.json and
    # scripts/flavors.py exist to stop: narrowing the build matrix while
    # promote and release still name images nothing produces fails late.
    if grep -rnE 'utah-(nvidia|gaming)' .github/workflows/; then
      echo 'no workflow may name a flavored image; read it from config/flavors.json' >&2
      exit 1
    fi
    if grep -nE '(utah|\{\{ image \}\})-(nvidia|gaming)' Justfile; then
      echo 'no recipe may name a flavored image; use flavors.py image' >&2
      exit 1
    fi
    if grep -rnE 'utah-(nvidia|gaming)' iso/scripts/; then
      echo 'no ISO script may name a flavored image; use flavors.py image' >&2
      exit 1
    fi

# Verify branding, desktop defaults, first-boot Flatpak policy, and service
# enablement in an already-composed image. The same verifier runs in the
# Containerfile, so this is useful for a local image or a CI artifact.
check-desktop-contract image_ref="localhost/utah:testing":
    #!/usr/bin/env bash
    set -euo pipefail
    podman run --rm --entrypoint /usr/bin/python3 \
      -v "$PWD/contracts/bluefin-desktop.toml:/tmp/bluefin-desktop.toml:ro" \
      -v "$PWD/scripts/verify-desktop-contract.py:/tmp/verify-desktop-contract.py:ro" \
      "{{ image_ref }}" /tmp/verify-desktop-contract.py /tmp/bluefin-desktop.toml
    podman run --rm --entrypoint /usr/bin/python3 \
      -v "$PWD/scripts/verify-gnome-extensions.py:/tmp/verify-gnome-extensions.py:ro" \
      "{{ image_ref }}" /tmp/verify-gnome-extensions.py

# Fail fast when a contract package is in none of the repositories the image
# actually enables, instead of discovering it twenty minutes into a build.
#
# The pinned base image is pulled from a remote registry here, so a CDN that
# drops a blob mid-read fails this gate before a single package is evaluated.
# Exit 125 is the container engine refusing to run the container at all, which
# is that failure and not a verdict about the package set. The flavor builds
# already retry their registry work, and this gate gates them, so a transient
# here is strictly more expensive than one there. Retry 125 and nothing else:
# a real resolution failure must stay immediate and loud rather than paying for
# three slow dnf resolves to reach the same answer.
#
# Resolves dependencies on the pinned base and package image. Needs podman and network.
# Then probes each [unavailable] entry the same way: a blocked entry that now
# resolves is stale parity debt, and the base image is already cached.
check-repos:
    #!/usr/bin/env bash
    set -uo pipefail
    python3 scripts/check-retag-names.py || exit "$?"
    gate() {
      for attempt in 1 2 3; do
        python3 scripts/check-repo-availability.py "$@"
        status=$?
        if [ "$status" -ne 125 ]; then
          return "$status"
        fi
        echo "check-repos: container engine could not run (exit 125), attempt ${attempt}/3" >&2
        if [ "$attempt" -ne 3 ]; then
          sleep $(( attempt * 15 ))
        fi
      done
      echo "check-repos: giving up after 3 engine failures; the registry is not serving the pinned image" >&2
      return 125
    }
    gate packages/base.toml packages/utah.toml || exit "$?"
    gate --check-unavailable packages/base.toml packages/utah.toml

# Verify Red Hat's signature on every Red Hat Hardened Images pin
# (registry.access.redhat.com/hi/<name>:<tag>@sha256:<digest>) in the
# Containerfiles. config/redhat-hi-cosign.pub is the public key block of Red
# Hat's release key 3 (https://security.access.redhat.com/data/63405576.txt).
# Red Hat signs with that key and does not use the transparency log, hence
# --insecure-ignore-tlog. Fails when no pin is left, so the CI step goes with
# the last one. Needs cosign and network.
verify-rhhi-signatures:
    #!/usr/bin/env bash
    set -euo pipefail
    mapfile -t refs < <(grep -rhoE 'registry\.access\.redhat\.com/hi/[a-z0-9._-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}' \
      Containerfile Containerfile.kernel iso | sort -u)
    if [ "${#refs[@]}" -eq 0 ]; then
      echo "verify-rhhi-signatures: no Red Hat Hardened Images pin found" >&2
      exit 1
    fi
    for ref in "${refs[@]}"; do
      cosign verify --key config/redhat-hi-cosign.pub --insecure-ignore-tlog "$ref" >/dev/null
      echo "verified: $ref"
    done

# Re-measure Utah against Dakota, its parity target: Dakota's element list from
# its SBOM, then the user-visible files inside both images. Needs podman and
# gh. Then review baselines/GAP.md and triage new gaps.
baselines dakota="ghcr.io/projectbluefin/dakota:testing" utah="ghcr.io/projectbluefin/utah:testing":
    #!/usr/bin/env bash
    set -euo pipefail
    # Dakota's publish.yml runs only for its `testing` and `next` branches;
    # `testing` publishes the image measured here. The SBOM comes first:
    # extract names each Dakota file's owner from elements.tsv.
    dakota_branch=testing
    run=$(gh run list -R projectbluefin/dakota -w publish.yml -b "$dakota_branch" -s success -L 20 \
      --json databaseId --jq '.[].databaseId' | while read -r id; do
        if gh api "repos/projectbluefin/dakota/actions/runs/$id/artifacts" \
          --jq '.artifacts[].name' | grep -x sbom-dakota >/dev/null; then
          echo "$id"
          break
        fi
      done)
    if [ -z "$run" ]; then
      echo "no successful projectbluefin/dakota publish.yml run on $dakota_branch in the last 20 has an sbom-dakota artifact" >&2
      exit 1
    fi
    python3 scripts/image-baseline.py dakota "$run" baselines/dakota
    for pair in "{{ dakota }} dakota" "{{ utah }} utah"; do
      read -r image dir <<<"$pair"
      podman pull -q "$image" >/dev/null
      python3 scripts/image-baseline.py extract "$image" "baselines/$dir"
    done
    python3 scripts/image-baseline.py gap

image_name base_name stream flavor:
    @python3 scripts/flavors.py image "{{ flavor }}"

generate-default-tag stream build_number:
    @echo "{{ stream }}"

setup-cache base_name stream build_number event_name:
    @echo "utah-{{ stream }} 1"

# The half-hour OGC kernel compile and the NVIDIA module build live in their own
# image, keyed by their own inputs, so a push that touches neither does not pay
# for them.  See Containerfile.kernel.
kernel-cache-tag:
    @./scripts/kernel-cache-tag.sh

kernel-cache-ref:
    @echo "ghcr.io/{{ repo_organization }}/{{ kernel_cache_image }}:$(./scripts/kernel-cache-tag.sh)"

build-kernel-cache:
    #!/usr/bin/env bash
    set -euo pipefail
    ref="ghcr.io/{{ repo_organization }}/{{ kernel_cache_image }}:$(./scripts/kernel-cache-tag.sh)"
    echo "Building $ref"
    podman build --tag "$ref" --file Containerfile.kernel .

build-ghcr base_name stream flavor kernel_pin="":
    #!/usr/bin/env bash
    set -euo pipefail
    version="{{ stream }}-$(date -u +%Y%m%d)-$(git rev-parse --short HEAD)"
    image_name="$(python3 scripts/flavors.py image '{{ flavor }}')"
    # The kernel cache image and the layer cache below are both published
    # private by default, and the reusable build workflow only logs in to GHCR
    # for non-PR events -- so pulling either would 401 on exactly the runs that
    # need them most.  It passes GITHUB_TOKEN through to this recipe, so use it.
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      auth_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/utah-registry.XXXXXX")"
      trap 'rm -rf "$auth_dir"' EXIT
      export DOCKER_CONFIG="$auth_dir" REGISTRY_AUTH_FILE="$auth_dir/config.json"
      echo "${GITHUB_TOKEN}" | podman login ghcr.io -u "${GITHUB_ACTOR:-x}" --password-stdin
    fi
    # Every flavor takes the kernel cache image as its base: its distribution
    # kernel is Fedora's rebuilt with the btrfs fs-verity fix, which main needs
    # as much as the rest (scripts/install-fedora-kernel.sh).  The OGC and
    # NVIDIA flavors also find /utah-cache there and unpack rather than compile.
    # BASE_IMAGE overrides it with a local `just build-kernel-cache` result, as
    # unsigned development input -- for a change to the cache's own inputs,
    # whose tag is not published yet. CI never takes the override.
    if [ -n "${BASE_IMAGE:-}" ] && [ "${GITHUB_ACTIONS:-false}" != true ]; then
      echo "Building on the local kernel cache ${BASE_IMAGE} (unverified)"
      base_args=(--build-arg BASE_IMAGE="${BASE_IMAGE}")
    else
      cache_ref="$(./scripts/kernel-cache-tag.sh)"
      cache_ref="ghcr.io/{{ repo_organization }}/{{ kernel_cache_image }}:${cache_ref}"
      # Verify immutable bytes, never a mutable tag that FROM can re-resolve.
      # The reusable builder runs this via sudo, whose secure_path drops tools
      # installed through GITHUB_PATH. Bootstrap a checksummed binary outside
      # the context in CI and invoke it by absolute path; local builds require it.
      # Keep this the version sigstore/cosign-installer gives kernel_cache:
      # cosign 3 signs in the bundle format stored as an OCI referrer, which
      # cosign 2 cannot see ("no signatures found").
      cosign_bin="$(command -v cosign || true)"
      if [ "${GITHUB_ACTIONS:-false}" = true ]; then
        case "$(uname -m)" in
          x86_64) cosign_arch=amd64; cosign_sha=424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa ;;
          aarch64) cosign_arch=arm64; cosign_sha=ade83b7aac62e131cacfe96d958948b2150cc9efec46a570a2649b8e27dedad2 ;;
          *) echo "Unsupported cosign architecture" >&2; exit 1 ;;
        esac
        cosign_bin="${RUNNER_TEMP:?}/utah-tools/cosign"
        mkdir -p "${cosign_bin%/*}"
        curl -fsSL "https://github.com/sigstore/cosign/releases/download/v3.1.4/cosign-linux-${cosign_arch}" -o "$cosign_bin"
        echo "${cosign_sha}  ${cosign_bin}" | sha256sum --check --strict
        chmod 0755 "$cosign_bin"
      fi
      if [ -z "$cosign_bin" ]; then
        echo "cosign is required to verify the kernel cache before building" >&2
        exit 1
      fi
      digest="$(skopeo inspect "docker://${cache_ref}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["Digest"])')"
      cache_ref="${cache_ref%:*}@${digest}"
      "$cosign_bin" verify "$cache_ref" \
        --certificate-oidc-issuer https://token.actions.githubusercontent.com \
        --certificate-identity-regexp '^https://github\.com/projectbluefin/utah/\.github/workflows/build\.yml@refs/(heads/testing|pull/[0-9]+/merge)$'
      base_args=(--build-arg BASE_IMAGE="$cache_ref")
    fi
    # Registry layer cache, the same arrangement Bluefin uses.  The package
    # transaction is the one expensive layer in the Containerfile and its
    # inputs move rarely: the two manifests, the repo files, the pinned package
    # image and the install script.  With --cache-from an unchanged layer is
    # pulled instead of rebuilt; with --cache-to (REGISTRY_CACHE_WRITE=1, which
    # the reusable workflow sets for non-PR events only) it is published for
    # the next run.  Pull-request and local builds are read-only, so nothing a
    # PR does can poison what testing builds from.
    #
    # The cache lives in the image's own GHCR package as SHA-keyed blobs, so it
    # needs no package of its own and GITHUB_TOKEN already has write access to
    # it.  Podman 5 wants an untagged ref here.  The probe is skopeo list-tags,
    # which succeeds on any readable package and fails on one that is private
    # to us or not yet pushed, in which case the cache is simply off.
    layer_cache_ref="ghcr.io/{{ repo_organization }}/${image_name}"
    layer_cache_args=()
    layer_cache_readable=false
    if command -v skopeo >/dev/null 2>&1 && skopeo list-tags "docker://${layer_cache_ref}" >/dev/null 2>&1; then
      layer_cache_readable=true
      layer_cache_args+=(--cache-from "$layer_cache_ref")
    fi
    if [ "${REGISTRY_CACHE_WRITE:-0}" = "1" ]; then
      layer_cache_args+=(--cache-to "$layer_cache_ref")
      echo "Registry layer cache: read=${layer_cache_readable} write=true (${layer_cache_ref})"
    elif [ "$layer_cache_readable" = true ]; then
      echo "Registry layer cache: read-only (${layer_cache_ref})"
    else
      echo "Registry layer cache: off (${layer_cache_ref} is not readable from here)"
    fi
    # Key the package transaction on Hummingbird's repository revision so the
    # layer cache above cannot pin a rolling repository to an old snapshot
    # (HUMMINGBIRD_REPO_DAY in the Containerfile). An unreachable repo
    # would fail the transaction anyway; warn here and let it.
    # Bash builtins only: the recipe also runs in a sandboxed PATH
    # (tests/test_kernel_cache_signing.py), where sed and curl may be absent.
    hb_baseurl=""
    while IFS= read -r line; do
      case "$line" in baseurl=*) hb_baseurl="${line#baseurl=}"; break ;; esac
    done < packages/hummingbird.repo
    hb_revision=""
    if command -v curl >/dev/null 2>&1 \
        && repomd="$(curl -fsSL --retry 3 --connect-timeout 10 --max-time 60 "${hb_baseurl%/}/repodata/repomd.xml")" \
        && [[ "$repomd" =~ \<revision\>([^<]+)\</revision\> ]]; then
      hb_revision="${BASH_REMATCH[1]}"
    fi
    if [ -z "$hb_revision" ]; then
      echo "::warning title=Hummingbird revision unresolved::${hb_baseurl} gave no repomd revision; the package layer may come from cache"
      hb_revision=unresolved
    elif [[ "$hb_revision" =~ ^[0-9]{9,}$ ]]; then
      # The revision is a publish timestamp and moves several times a day.
      # Keyed on its UTC day, the transaction refreshes once a day and the
      # builds in between still share the cached layer.
      hb_revision="$(date -u -d "@${hb_revision}" +%Y-%m-%d)"
    fi
    echo "Hummingbird repository day: ${hb_revision}"
    # Epoch follows commits to build inputs, including shipped device docs;
    # other documentation-only commits retain the cache key. The layer helper rejects any newer RPM payload
    # before timestamp rewriting could invalidate its Python caches.
    source_date_epoch="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct -- Containerfile packages system_files scripts config .gitmodules docs/agent)}"
    # The created label reuses this epoch: the last build-input commit time, not wall-clock build time.
    mapfile -t epoch_args < <(scripts/podman-epoch-args.sh "${source_date_epoch}" rewrite)
    gnome_extension_provenance="$(python3 scripts/verify-gnome-extensions.py --print-provenance)"
    common_args=()
    if [ -n "${COMMON_IMAGE_REF:-}" ]; then
      # A Common override must name the digest it builds from, so the image's
      # provenance.json records the candidate layer, not the pinned default.
      if [ -z "${COMMON_IMAGE:-}" ] || [[ ! "${COMMON_IMAGE_SHA:-}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
        echo "error: COMMON_IMAGE_REF needs COMMON_IMAGE and COMMON_IMAGE_SHA (sha256:<64 hex>) for provenance" >&2
        exit 1
      fi
      common_args+=(
        --build-arg COMMON_IMAGE="$COMMON_IMAGE"
        --build-arg COMMON_IMAGE_SHA="$COMMON_IMAGE_SHA"
        --build-arg COMMON_IMAGE_REF="$COMMON_IMAGE_REF"
      )
    fi
    podman build \
      "${epoch_args[@]}" \
      --build-arg SOURCE_DATE_EPOCH="${source_date_epoch}" \
      --build-arg UTAH_REWRITE_TIMESTAMPS=1 \
      "${base_args[@]}" \
      "${common_args[@]}" \
      "${layer_cache_args[@]}" \
      --build-arg HUMMINGBIRD_REPO_DAY="$hb_revision" \
      --build-arg IMAGE_NAME="$image_name" \
      --build-arg IMAGE_ID="{{ image }}" \
      --build-arg IMAGE_FLAVOR={{ flavor }} \
      --build-arg IMAGE_VENDOR={{ repo_organization }} \
      --build-arg VERSION="$version" \
      --build-arg SHA_HEAD="$(git rev-parse HEAD)" \
      --build-arg IMAGE_CREATED="$(date -u -d "@${source_date_epoch}" +%Y-%m-%dT%H:%M:%SZ)" \
      --build-arg GNOME_EXTENSION_PROVENANCE="${gnome_extension_provenance}" \
      --build-arg SHA_HEAD_SHORT="$(git rev-parse --short HEAD)" \
      --build-arg ENABLE_SSHD="${ENABLE_SSHD:-0}" \
      --tag "localhost/$image_name:{{ stream }}" \
      --file Containerfile .
    # The revision label must name the commit that built the image (#1400).
    built_revision="$(podman inspect "localhost/$image_name:{{ stream }}" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["Config"]["Labels"].get("org.opencontainers.image.revision",""))')"
    if [ "$built_revision" != "$(git rev-parse HEAD)" ]; then
      echo "error: image revision label '${built_revision}' != HEAD $(git rev-parse HEAD)" >&2
      exit 1
    fi

# Prove a rebuild that changes nothing produces the same image (utah#313).
# Builds the flavor twice, uncached, with the wall-clock build args held
# constant, and diffs the ordered layer digests. Two full builds: slow, and
# deliberately not part of `just check` or the PR matrix.
check-reproducible flavor="main":
    bash scripts/check-reproducible-build.sh "{{ flavor }}"

# Compose with an RPM repository already in local containers-storage. This uses
# the same Containerfile transaction as CI without waiting for publication.
build-local stream="testing" package_image="localhost/utah-packages:local-merged":
    #!/usr/bin/env bash
    set -euo pipefail
    package_image="{{ package_image }}"
    podman image exists "$package_image" || {
      echo "Local package image not found: $package_image" >&2
      exit 1
    }
    # The kernel cache image is the base here too (see build-ghcr). BASE_IMAGE
    # overrides it with a local build of Containerfile.kernel, which is trusted
    # as development input; the published cache is verified like build-ghcr's.
    base_image="${BASE_IMAGE:-}"
    if [ -z "$base_image" ]; then
      command -v cosign >/dev/null 2>&1 || { echo "cosign is required to verify the kernel cache before building (or set BASE_IMAGE)" >&2; exit 1; }
      base_image="ghcr.io/{{ repo_organization }}/{{ kernel_cache_image }}:$(./scripts/kernel-cache-tag.sh)"
      digest="$(skopeo inspect "docker://${base_image}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["Digest"])')"
      base_image="${base_image%:*}@${digest}"
      cosign verify "$base_image" \
        --certificate-oidc-issuer https://token.actions.githubusercontent.com \
        --certificate-identity-regexp '^https://github\.com/projectbluefin/utah/\.github/workflows/build\.yml@refs/(heads/testing|pull/[0-9]+/merge)$'
    fi
    version="local-{{ stream }}-$(git rev-parse --short HEAD)"
    # Same build-input epoch and guarded export policy as build-ghcr.
    source_date_epoch="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct -- Containerfile packages system_files scripts config .gitmodules docs/agent)}"
    mapfile -t epoch_args < <(scripts/podman-epoch-args.sh "${source_date_epoch}" rewrite)
    gnome_extension_provenance="$(python3 scripts/verify-gnome-extensions.py --print-provenance)"
    podman build \
      "${epoch_args[@]}" \
      --build-arg SOURCE_DATE_EPOCH="${source_date_epoch}" \
      --build-arg UTAH_REWRITE_TIMESTAMPS=1 \
      --build-arg BASE_IMAGE="$base_image" \
      --build-arg PACKAGE_IMAGE_REF="$package_image" \
      --build-arg IMAGE_NAME="{{ image }}" \
      --build-arg IMAGE_ID="{{ image }}" \
      --build-arg IMAGE_FLAVOR=main \
      --build-arg IMAGE_VENDOR="{{ repo_organization }}" \
      --build-arg VERSION="$version" \
      --build-arg SHA_HEAD="$(git rev-parse HEAD)" \
      --build-arg IMAGE_CREATED="$(date -u -d "@${source_date_epoch}" +%Y-%m-%dT%H:%M:%SZ)" \
      --build-arg GNOME_EXTENSION_PROVENANCE="${gnome_extension_provenance}" \
      --build-arg SHA_HEAD_SHORT="$(git rev-parse --short HEAD)" \
      --build-arg ENABLE_SSHD="${ENABLE_SSHD:-1}" \
      --tag "localhost/{{ image }}:{{ stream }}" \
      --file Containerfile .

generate-build-tags base_name stream flavor kernel_pin build_number version event_name event_number:
    @echo "{{ stream }} {{ version }}"

tag-images image_name default_tag alias_tags:
    #!/usr/bin/env bash
    set -euo pipefail
    for tag in {{ alias_tags }}; do podman tag "localhost/{{ image_name }}:{{ default_tag }}" "localhost/{{ image_name }}:$tag"; done

# The reusable build runs this under sudo, before the push. The image is in
# rootful containers-storage, where syft's podman provider cannot reach it (no
# podman socket on the runner), so scan the mounted image root instead. Hand
# sbom_out back to the invoking user whatever happens: sign-and-publish later
# writes its own SBOM there unprivileged, and a root-owned sbom_out made that
# step fail, which skipped the build-provenance attestation. With no
# attestation, post-testing-e2e's verify-provenance got a 404 for every flavor
# and :testing stopped advancing.
gen-sbom base_name stream flavor syft_cmd:
    #!/usr/bin/env bash
    set -euo pipefail
    image_name="$(python3 scripts/flavors.py image '{{ flavor }}')"
    ref="localhost/$image_name:{{ stream }}"
    mounted=""
    finish() {
        if [[ -n "$mounted" ]]; then podman image unmount "$ref" >/dev/null || true; fi
        if [[ -n "${SUDO_UID:-}" && -d sbom_out ]]; then chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" sbom_out; fi
    }
    trap finish EXIT
    mkdir -p "sbom_out/$image_name"
    root="$(podman image mount "$ref")"
    mounted=1
    "{{ syft_cmd }}" "dir:$root" --source-name "$ref" -o json >"sbom_out/$image_name/sbom.json"

# Install the locally built bootc image to a sparse disk for QEMU. This follows
# Bluefin's bootc-to-disk path rather than trying to boot an OCI layer directly.
generate-bootable-image stream="testing":
    #!/usr/bin/env bash
    set -euo pipefail
    ref="localhost/{{ image }}:{{ stream }}"
    # bootc needs the host root namespace. Local image builds normally use
    # rootless Podman, so transfer that image into rootful storage once before
    # running the disk install.
    if sudo podman image exists "$ref"; then
      PODMAN=(sudo podman)
    elif podman image exists "$ref"; then
      echo "Loading rootless $ref into rootful Podman for bootc..."
      archive="$(mktemp -p /var/tmp --suffix=.oci.tar)"
      trap 'rm -f "$archive"' EXIT
      podman save --format oci-archive -o "$archive" "$ref"
      sudo podman load -i "$archive"
      PODMAN=(sudo podman)
    else
      echo "Image $ref not found; run: just build-ghcr {{ image }} {{ stream }} main" >&2
      exit 1
    fi
    mkdir -p "{{ base_dir }}"
    disk="$(realpath "{{ base_dir }}")/bootable.raw"
    rm -f "$disk"
    fallocate -l 30G "$disk"
    echo "Installing $ref to $disk"
    install_args=(
      --via-loopback /data/bootable.raw
      --filesystem btrfs
      --wipe
      --generic-image
    )
    volumes=(
      -v "$(realpath "{{ base_dir }}"):/data:Z"
    )
    # The local OCI ref is not available from the guest's localhost registry;
    # mark this disposable disk so its published-image-only unified-storage
    # service is skipped instead of retrying forever.
    if [[ "$ref" == localhost/* ]]; then
      install_args+=( --karg=utah.local )
    fi
    # Inject the developer key into root for headless boot diagnostics. This
    # does not enable password login and is only present in the disposable
    # locally generated disk, never in the OCI image.
    if [ -f "$HOME/.ssh/id_ed25519.pub" ]; then
      volumes+=( -v "$HOME/.ssh:/ssh:ro" )
      install_args+=( --root-ssh-authorized-keys /ssh/id_ed25519.pub )
    fi
    "${PODMAN[@]}" run --rm --privileged --pid=host \
      "${volumes[@]}" \
      --security-opt label=type:unconfined_t \
      "$ref" bootc install to-disk "${install_args[@]}"
    # bootupd writes the vendor EFI entry but a fresh QEMU VM has no NVRAM
    # entry. Install the standard removable-media fallback so QEMU firmware
    # can find the image without importing host firmware variables.
    loop="$(sudo losetup --find --show --partscan "$disk")"
    esp="$(mktemp -d)"
    trap 'sudo umount "$esp" 2>/dev/null || true; sudo losetup -d "$loop" 2>/dev/null || true; rm -rf "$esp"' EXIT
    sudo mount "${loop}p2" "$esp"
    sudo install -d "$esp/EFI/BOOT"
    sudo install -m 0644 "$esp/EFI/fedora/shimx64.efi" "$esp/EFI/BOOT/BOOTX64.EFI"
    # Fedora's shim looks for its GRUB loader beside the removable-media
    # fallback path when no vendor NVRAM entry exists.
    sudo install -m 0644 "$esp/EFI/fedora/grubx64.efi" "$esp/EFI/BOOT/grubx64.efi"
    sudo umount "$esp"
    sudo losetup -d "$loop"
    trap - EXIT
    rm -rf "$esp"
    sync
    echo "Bootable disk ready: $disk"

# Build the gnome-setup install medium (iso/raw, docs/adr/0001) from a local
# image: a raw disk that boots live (utah.live) and installs by block copy.
# Needs root (bootc install, loop devices). Write it to a USB stick with dd, or
# boot it in a VM next to a blank target disk (docs/skills/local-testing/
# references/image-installer.md).
medium stream="testing":
    #!/usr/bin/env bash
    set -euo pipefail
    ref="localhost/{{ image }}:{{ stream }}"
    sudo podman image exists "$ref" || { echo "Image $ref not found in root's storage; build it with sudo just build-ghcr {{ image }} {{ stream }} main" >&2; exit 1; }
    mkdir -p "{{ base_dir }}"
    sudo podman build --layers=false -t localhost/utah-raw:local \
      --build-arg SOURCE_IMAGE="$ref" -f iso/raw/Containerfile iso/raw
    sudo bash iso/raw/build-raw-image.sh localhost/utah-raw:local "$(realpath "{{ base_dir }}")/medium.raw" \
      "ghcr.io/{{ repo_organization }}/{{ image }}:{{ stream }}"
    sudo bash iso/raw/bake-flatpaks.sh localhost/utah-raw:local "$(realpath "{{ base_dir }}")/medium.raw"
    sudo bash iso/raw/shrink-raw-image.sh localhost/utah-raw:local "$(realpath "{{ base_dir }}")/medium.raw"
    echo "Medium ready: {{ base_dir }}/medium.raw"

# Boot the installed Utah disk through QEMU-for-Docker. Open the printed URL
# and confirm GDM appears and the GNOME Shell desktop renders. The disk is
# mounted at /boot.img and -snapshot keeps the test disposable.
boot-vm:
    #!/usr/bin/env bash
    set -euo pipefail
    disk="$(realpath "{{ base_dir }}/bootable.raw")"
    test -f "$disk" || { echo "Missing $disk; run just generate-bootable-image" >&2; exit 1; }
    port=8006
    while ss -H -tln "sport = :$port" 2>/dev/null | grep -q .; do
      port=$((port + 1))
    done
    ssh_port=2222
    while ss -H -tln "sport = :$ssh_port" 2>/dev/null | grep -q .; do
      ssh_port=$((ssh_port + 1))
    done
    echo "QEMU web console: http://127.0.0.1:$port"
    echo "SSH diagnostics: ssh -p $ssh_port root@127.0.0.1"
    podman run --rm --privileged --device /dev/kvm --pull=always \
      --publish "127.0.0.1:${port}:8006" \
      --publish "127.0.0.1:${ssh_port}:22" \
      --env USER_PORTS=22 \
      --env NETWORK=user \
      --env CPU_CORES="{{ vm_cpus }}" \
      --env RAM_SIZE="{{ vm_ram }}" \
      --env TPM=y \
      --env BOOT_MODE=uefi \
      --env ARGUMENTS=-snapshot \
      --volume "$disk:/boot.img" \
      ghcr.io/qemus/qemu:latest

secureboot base_name default_tag flavor:
    #!/usr/bin/env bash
    set -euo pipefail
    image_name="$(python3 scripts/flavors.py image '{{ flavor }}')"
    podman run --rm --entrypoint /bin/sh "localhost/$image_name:{{ default_tag }}" -c 'test -e /usr/lib/modules || test -e /boot'

# Regenerate the status page's package data from the manifests. Run this after
# changing packages/base.toml or packages/utah.toml; `just check` fails if
# the committed copy is stale.
site-data:
    python3 scripts/generate-site-data.py

# Serve the status page locally at http://localhost:8000 for a visual check.
# The live status and roadmap cards call the public GitHub API from the browser.
site-serve: site-data
    python3 -m http.server 8000 --directory site
