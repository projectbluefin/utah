#!/usr/bin/env python3
"""Assert that Utah actually contains its Bluefin and GNOME 51 RPM contracts.

Beyond package presence, this is the supply-chain attestation for issue #21:
GNOME packages carry the promised major version and an approved factory
(`.bfin`) or Hummingbird (`.hum`) identity; parity packages cannot silently
resolve from an unapproved Fedora repository; the composed image exposes only
the runtime repositories the manifest allows; and the resolved
package-origin/NEVRA set is retained as a report with build provenance.

`--check` validates the manifest itself off-image: the `.repo` files in
`packages/` may name only the repositories the manifest allows. The on-image
run applies the same allowlist to the composed image's runtime RPM
repositories -- every `reposdir` dnf5 reads at runtime, derived from the
[main] config the base image ships rather than a hardcoded default (#454,
#513, #536).

Mirrors assert_packages_present from projectbluefin/bluefin's
build_files/shared/package-lib.sh: name every missing package, once.
"""

from __future__ import annotations

import argparse
import configparser
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

# The NVIDIA userspace no longer arrives as RPMs. UBlue's akmods bundle used to
# supply nvidia-driver, nvidia-driver-cuda and nvidia-container-toolkit, but it
# publishes nothing for Hummingbird's kernel, so install-nvidia.sh builds the
# open module from NVIDIA's own source and installs the matching userspace from
# the same payload. Those files are what the image needs; the RPM names were
# only ever how they happened to arrive.
#
# nvidia-container-toolkit still arrives as an RPM, from NVIDIA's own repository
# rather than from the akmods bundle, so it is asserted by name. It was recorded
# here as a real loss on the reasoning that the bundle was unusable; the bundle
# was one source, not the only one.
NVIDIA_PACKAGES = (
    "nvidia-container-toolkit",
)

CONTRACT_PATH = "/usr/share/utah/contract.txt"
DEFAULT_REPORT_DIR = "/usr/share/utah"
# The factory pin travels on the image as a stamp in the repository file, which
# is what the report quotes: it says which package factory the NEVRAs came from
# without needing a build argument plumbed through every stage.
FACTORY_REPO_PATH = "/etc/yum.repos.d/utah-packages.repo"
# Where the composed image's runtime RPM repositories live by default. --check
# works against the source repo files in packages/; the on-image run scans the
# paths dnf5 actually reads. dnf5 loads every one of these when it resolves
# packages, so scanning only /etc/yum.repos.d left a repo file the base ships
# in another default reposdir enabled at runtime yet invisible to the gate
# (#513). A base image can also override this default via `reposdir=` in
# /etc/dnf/dnf.conf or a libdnf5 drop-in (see dnf5_config_files), which replaces the
# default list (#536): a config that sets reposdir to one custom path bypasses
# the allowlist if this script only scans the hardcoded defaults.
DEFAULT_REPOS_DIRS: tuple[Path, ...] = (
    Path("/etc/yum.repos.d"),
    Path("/etc/distro.repos.d"),
    Path("/usr/share/dnf5/repos.d"),
)
# Where dnf5 looks for its [main] configuration: the drop-ins in
# /etc/dnf/libdnf5.conf.d and /usr/share/dnf5/libdnf.conf.d (merged by file
# name, /etc masking /usr/share, applied in file-name order), then
# /etc/dnf/dnf.conf; options from later files override earlier ones.
# runtime_reposdir_paths reads `reposdir=` from the same files, so the on-image
# scan honours the actual list dnf5 uses at runtime.
DNF_DISTRO_CONF_D = Path("/usr/share/dnf5/libdnf.conf.d")
DNF_USER_CONF_D = Path("/etc/dnf/libdnf5.conf.d")
DNF_MAIN_CONF = Path("/etc/dnf/dnf.conf")
FACTORY_PIN_RE = re.compile(r"^# factory-pin: (?P<digest>\S+)\s*$", re.MULTILINE)

DISABLED_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off"})
# Fetch-integrity options a repository may be approved to leave disabled via
# [repositories.security]; proxy= and sslverify=0 are never approvable.
APPROVABLE_SECURITY_OPTIONS: tuple[str, ...] = ("gpgcheck", "repo_gpgcheck")
# The config keys that set each approvable option. libdnf5 treats `gpgcheck` as
# an alias of its canonical `pkg_gpgcheck` (last assignment wins), so either
# spelling disables package signature verification and both map to the
# `gpgcheck` approval.
SIGNATURE_OPTION_KEYS: dict[str, tuple[str, ...]] = {
    "gpgcheck": ("gpgcheck", "pkg_gpgcheck"),
    "repo_gpgcheck": ("repo_gpgcheck",),
}


def section(overlay: Path, name: str, key: str = "packages") -> list[str]:
    """A named package list from an overlay manifest, in the order written.

    `key` selects which list in the section to read, so a section can declare
    more than one bucket of names ([factory] declares `packages` and `parity`).
    """
    data = tomllib.loads(overlay.read_text(encoding="utf-8"))
    if name not in data or key not in data[name]:
        return []
    return list(data[name][key])


def dnf5_config_files() -> list[Path]:
    """The dnf5 [main] config files in load order (later wins).

    Mirrors libdnf5 Base::load_config: the drop-in dirs
    /etc/dnf/libdnf5.conf.d and /usr/share/dnf5/libdnf.conf.d are merged by
    file name, a file in /etc masking a same-named file in /usr/share, and the
    union is applied sorted by file name (not by directory). /etc/dnf/dnf.conf
    is applied last. Getting this order wrong would let the gate resolve a
    different `reposdir=` than dnf5 does (#536).
    """
    by_name: dict[str, Path] = {}
    for conf_dir in (DNF_USER_CONF_D, DNF_DISTRO_CONF_D):
        if not conf_dir.is_dir():
            continue
        for p in sorted(conf_dir.glob("*.conf")):
            if p.is_file() and p.name not in by_name:
                by_name[p.name] = p
    paths = [by_name[name] for name in sorted(by_name)]
    paths.append(DNF_MAIN_CONF)
    return paths


class Dnf5ConfigError(Exception):
    """A dnf5 [main] config the gate cannot resolve the way dnf5 does."""


# Arches whose rpm `$arch` equals dnf5's `$basearch`, so both can be substituted
# from the running machine without reimplementing libdnf5's arch map.
_IDENTITY_BASEARCHES: frozenset[str] = frozenset(
    {"x86_64", "aarch64", "ppc64le", "s390x", "riscv64"}
)
_DNF_VAR_RE = re.compile(r"\$(?:\{(?P<braced>\w+)\}|(?P<bare>\w+))")
# libdnf5 also accepts `${var:-default}` and `${var:+alt}`; raise on any
# braced form whose body is not `\w+` so the gate does not silently scan
# the literal `${...}` substring dnf5 would have resolved differently
# (#540 review).
_DNF_VAR_UNKNOWN_RE = re.compile(r"\$\{(?P<body>[^}]*)\}")


def _substitute_dnf_vars(value: str, path: Path) -> str:
    """Expand `$basearch`/`$arch` in a [main] value as libdnf5 would.

    libdnf5 runs its variable substitution on every [main] value. Any other
    variable ($releasever, custom vars from vars.d, ...) cannot be resolved
    here with certainty, so it raises rather than scanning a literal path dnf5
    never reads.
    """
    machine = os.uname().machine
    known = {"arch": machine, "basearch": machine} if machine in _IDENTITY_BASEARCHES else {}

    def repl(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        if name not in known:
            raise Dnf5ConfigError(
                f"dnf5 config {path} sets reposdir with unresolvable variable ${name}"
            )
        return known[name]

    # Reject `${var:-default}` / `${var:+alt}` (and any other non-identifier
    # braced body) up front so the substitution below never passes a literal
    # `${...}` substring through unchanged when libdnf5 would have expanded
    # it via its own default/alternate-value rules.
    for unknown in _DNF_VAR_UNKNOWN_RE.finditer(value):
        body = unknown.group("body")
        if not re.fullmatch(r"\w+", body):
            raise Dnf5ConfigError(
                f"dnf5 config {path} sets reposdir with unresolvable "
                f"variable ${{{body}}}"
            )

    return _DNF_VAR_RE.sub(repl, value)


def parse_reposdir_from_config(config_files: list[Path]) -> list[Path] | None:
    """The reposdir list the latest [main] config wins with, or None.

    Returns the last non-empty `reposdir=` value found, which is what dnf5
    resolves at runtime (the docs say the later file's option wins). Returns
    None if no config sets the option, so the caller can fall back to the
    documented default. Duplicate keys and sections are accepted with the last
    value winning, as libdnf5's parser does. A config that exists but cannot be
    read or parsed raises Dnf5ConfigError: dnf5 itself aborts on such a file,
    and skipping it would silently widen the gate back to the defaults.
    Inline `#` text is not stripped, so `reposdir=/opt/x # note` scans the
    extra (nonexistent) entries too -- a harmless superset.
    """
    configured: list[Path] | None = None
    for path in config_files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            raise Dnf5ConfigError(f"could not read dnf5 config {path}: {err}") from err
        # libdnf5 is case-sensitive on option keys and accepts only `=` as
        # the delimiter (`reposdir` ≠ `Reposdir:`). configparser defaults to
        # a case-insensitive `optionxform=str.lower` and treats `:` as a
        # delimiter, so `Reposdir:` would be honoured here but ignored at
        # runtime; pin the same case sensitivity and delimiter set libdnf5
        # uses so the gate reads what dnf5 reads (#540 review).
        parser = configparser.ConfigParser(
            interpolation=None,
            strict=False,
            delimiters=("=",),
        )
        parser.optionxform = str
        try:
            parser.read_string(text, source=str(path))
        except configparser.Error as err:
            raise Dnf5ConfigError(f"could not parse dnf5 config {path}: {err}") from err
        if not parser.has_section("main"):
            continue
        raw = parser.get("main", "reposdir", fallback="").strip()
        if not raw:
            continue
        raw = _substitute_dnf_vars(raw, path)
        configured = [Path(p) for p in re.split(r"[\s,]+", raw) if p]
    return configured


def runtime_reposdir_paths() -> list[Path]:
    """The reposdir paths dnf5 actually scans at runtime.

    Reads every dnf5 [main] config in load order; if any sets `reposdir=`,
    that list replaces the documented default. Falls back to the default
    `DEFAULT_REPOS_DIRS` when no config opts in (#536): a base image that
    configures a custom reposdir would otherwise slip a `.repo` file past the
    allowlist gate that scans only the three defaults. Raises Dnf5ConfigError
    when a config cannot be resolved, so the caller fails the gate closed.
    """
    configured = parse_reposdir_from_config(dnf5_config_files())
    return list(configured) if configured is not None else list(DEFAULT_REPOS_DIRS)


def main_section_security_errors(
    config_files: list[Path], source: str
) -> list[str]:
    """Global options in the resolved [main] that reroute or weaken every repo.

    `proxy=` and `sslverify=0` set in the [main] section of dnf.conf or a
    libdnf5 drop-in apply to every allowlisted repository, so the per-section
    check in `check_repo_sections` -- which only inspects `.repo` sections and
    never the [main] block -- never inspects them (utah#352, adjacent to #339).
    `gpgcheck=0`/`pkg_gpgcheck=0`/`repo_gpgcheck=0` in [main] likewise disable
    signature verification for every repository that does not override them;
    per-repository approval in [repositories.security] does not cover [main],
    so a disabled value here is always reported (#345).
    Resolve the [main] options the way libdnf5 does (later file's value wins,
    including an empty `proxy=` clearing an earlier one) and report the
    effective values, using the same case-sensitive, `=`-only parsing as
    `parse_reposdir_from_config` so the gate reads what dnf5 reads. Like that
    function, a config that exists but cannot be read or parsed raises
    Dnf5ConfigError so the caller fails the gate closed.
    """
    proxy = ""
    sslverify = ""
    signature: dict[str, tuple[str, str]] = {}
    for path in config_files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            raise Dnf5ConfigError(f"could not read dnf5 config {path}: {err}") from err
        parser = configparser.ConfigParser(
            interpolation=None,
            strict=False,
            delimiters=("=",),
        )
        parser.optionxform = str
        try:
            parser.read_string(text, source=str(path))
        except configparser.Error as err:
            raise Dnf5ConfigError(f"could not parse dnf5 config {path}: {err}") from err
        if not parser.has_section("main"):
            continue
        if parser.has_option("main", "proxy"):
            proxy = parser.get("main", "proxy").strip()
        if parser.has_option("main", "sslverify"):
            sslverify = parser.get("main", "sslverify").strip()
        for approval, keys in SIGNATURE_OPTION_KEYS.items():
            values = [
                (key, parser.get("main", key).strip())
                for key in keys
                if parser.has_option("main", key)
            ]
            if not values:
                continue
            # configparser does not keep the relative order of two alias keys
            # in one file, so a disabled spelling wins (fail closed).
            disabled = [kv for kv in values if kv[1].lower() in DISABLED_VALUES]
            signature[approval] = disabled[0] if disabled else values[-1]
    errors: list[str] = []
    if proxy:
        errors.append(
            f"[main] in {source} sets proxy={proxy}; a proxy routes every allowlisted "
            "repository's fetches through an origin the allowlist does not name"
        )
    if sslverify.lower() in DISABLED_VALUES:
        errors.append(
            f"[main] in {source} sets sslverify={sslverify}; disabling TLS verification "
            "accepts any certificate every allowlisted repository presents"
        )
    for key, value in signature.values():
        if value.lower() in DISABLED_VALUES:
            errors.append(
                f"[main] in {source} sets {key}={value}; disabling RPM signature "
                "verification in [main] applies to every allowlisted repository; "
                "approve a repository's signature drift in [repositories.security] instead"
            )
    return errors


def is_installed(package: str) -> bool:
    """Whether an RPM named `package` is installed (rpm -q exit status)."""
    return subprocess.run(["rpm", "-q", package], capture_output=True).returncode == 0


def is_repo_enabled(enabled_val: str) -> bool:
    """Normalize boolean repository enabled semantics, failing closed on unknown values."""
    return enabled_val.strip().lower() not in DISABLED_VALUES


def determine_origin(pkg: str, release: str) -> str:
    """Classify a package's origin from its release identity, not its name.

    The NVIDIA case is decided by membership in NVIDIA_PACKAGES rather than by
    searching for "nvidia" inside the release string, which a coincidental
    rebuild could trip.
    """
    if ".bfin" in release:
        return "factory"
    if ".hum" in release:
        return "hummingbird"
    if pkg in NVIDIA_PACKAGES:
        return "nvidia"
    if ".fc" in release:
        return "fedora"
    return "unknown"


def query_packages(packages: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Query rpm for NEVRA attributes of requested packages.

    rpm -q prints one line per installed copy of a name; a multilib pair is two
    installs of one package. Every copy is retained so neither escapes the
    release-identity checks.
    """
    if not packages:
        return {}, []
    try:
        res = subprocess.run(
            ["rpm", "-q", "--qf", "%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n", *packages],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        res = None

    installed: dict[str, dict[str, Any]] = {}
    if res and res.stdout:
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) != 5:
                continue
            name, epoch, version, release, arch = parts
            nevra = (
                f"{name}-{version}-{release}.{arch}"
                if epoch in ("", "0", "(none)")
                else f"{name}-{epoch}:{version}-{release}.{arch}"
            )
            install = {
                "name": name, "epoch": epoch, "version": version,
                "release": release, "arch": arch, "nevra": nevra,
                "origin": determine_origin(name, release),
            }
            existing = installed.get(name)
            if existing is None:
                installed[name] = {**install, "installs": [install]}
            else:
                existing["installs"].append(install)
    missing = [p for p in packages if p not in installed]
    return installed, missing


def installs_of(info: dict[str, Any]) -> list[dict[str, str]]:
    """Every installed copy recorded for a package name, including multilib pairs."""
    return info.get("installs") or [info]


def verify_gnome_contract(
    gnome_packages: list[str],
    installed: dict[str, dict[str, Any]],
    major_versions: dict[str, str],
    factory_packages: set[str],
) -> list[str]:
    """Assert GNOME required major versions and factory/Hummingbird release identity.

    Which GNOME packages must carry `.bfin` is decided by `[factory]` in
    packages/utah.toml, so moving a package to or from the factory is a manifest
    edit, not a code edit. A factory package must be a factory rebuild; a
    non-factory package comes from Hummingbird but a factory rebuild is also
    approved; a bare Fedora build is never approved.
    """
    errors: list[str] = []
    for pkg in gnome_packages:
        if pkg not in installed:
            continue
        for info in installs_of(installed[pkg]):
            ver, rel = info["version"], info["release"]
            expected_major = major_versions.get(pkg)
            if expected_major:
                match = re.match(r"^(\d+)", ver)
                if not match or match.group(1) != str(expected_major):
                    errors.append(
                        f"GNOME package '{pkg}' version '{ver}' does not match required "
                        f"major version '{expected_major}'"
                    )
            if pkg in factory_packages:
                if ".bfin" not in rel:
                    errors.append(
                        f"GNOME package '{pkg}' release '{rel}' lacks expected factory "
                        "release identity (.bfin)"
                    )
            elif ".bfin" not in rel and ".hum" not in rel:
                # One defect, one error: a bare Fedora release is named as such,
                # anything else unidentified gets the general message.
                if ".fc" in rel:
                    errors.append(
                        f"GNOME package '{pkg}' resolved from unapproved Fedora release '{rel}'"
                    )
                else:
                    errors.append(
                        f"GNOME package '{pkg}' resolved from unapproved release '{rel}' "
                        "(expected Hummingbird `.hum` or factory `.bfin`)"
                    )
    return errors


def verify_parity_origin(
    parity_packages: list[str],
    installed: dict[str, dict[str, Any]],
    factory_parity: set[str] | None = None,
) -> list[str]:
    """Assert parity packages cannot silently resolve from another repository.

    Most parity packages are inherited from Hummingbird's repository. A `.fc`
    release without the `.hum` Hummingbird tag means DNF pulled it from the base
    image's Fedora repository instead of the pinned Hummingbird one.

    `[factory] parity` in packages/utah.toml names the parity packages the
    factory itself supplies. Those are held to the stronger rule: the factory
    publishes them as `.hum<N>.bfin`, which outranks Hummingbird's `.hum<N>`, so
    a copy without `.bfin` means the transaction resolved somewhere other than
    the factory repository -- the silent substitution issue #21 forbids.
    """
    factory_parity = factory_parity or set()
    errors: list[str] = []
    for pkg in parity_packages:
        if pkg not in installed:
            continue
        for info in installs_of(installed[pkg]):
            rel = info["release"]
            if pkg in factory_parity:
                if ".bfin" not in rel:
                    errors.append(
                        f"Parity package '{pkg}' is supplied by the factory but resolved "
                        f"from release '{rel}', which lacks the factory identity (.bfin)"
                    )
                continue
            if ".fc" in rel and ".hum" not in rel and ".bfin" not in rel:
                errors.append(
                    f"Parity package '{pkg}' resolved from unapproved Fedora release '{rel}'"
                )
    return errors


def normalize_baseurl(url: str) -> str:
    """Normalize a baseurl so two spellings of the same URL compare equal."""
    value = url.strip().rstrip("/")
    if not value:
        return ""
    value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", r"$\1", value)
    scheme, sep, rest = value.partition("://")
    if not sep:
        return value.lower()
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


def split_baseurls(raw: str) -> list[str]:
    """Split a baseurl option into the origins DNF would fetch from."""
    return [entry for entry in re.split(r"[\s,]+", raw.strip()) if entry]


def repo_pin_errors(
    section_name: str,
    parser: configparser.ConfigParser,
    source: str,
    expected_baseurls: dict[str, tuple[str, ...]],
) -> list[str]:
    """Check that an allowlisted repository serves the baseurl it is pinned to."""
    declared = expected_baseurls.get(section_name)
    if not declared:
        return [
            f"Allowlisted repository '{section_name}' is enabled in {source} but has "
            "no pinned baseurl in [repositories.baseurls]; an id on the allowlist is "
            "not approval of an unknown origin"
        ]
    baseurl = parser.get(section_name, "baseurl", fallback="").strip()
    indirection = next(
        (key for key in ("metalink", "mirrorlist")
         if parser.get(section_name, key, fallback="").strip()),
        "",
    )
    if indirection:
        return [
            f"Allowlisted repository '{section_name}' is enabled in {source} and "
            f"resolves via {indirection}; DNF merges those mirrors with any baseurl the "
            f"section declares, so only a pinned baseurl is approved "
            f"(expected one of: {', '.join(sorted(declared))})"
        ]
    if not baseurl:
        return [
            f"Allowlisted repository '{section_name}' is enabled in {source} and declares "
            "no baseurl; only a pinned baseurl is approved (expected one of: "
            f"{', '.join(sorted(declared))})"
        ]
    pinned = {normalize_baseurl(url) for url in declared}
    unpinned = [url for url in split_baseurls(baseurl) if normalize_baseurl(url) not in pinned]
    if unpinned:
        listed = ", ".join(f"'{url}'" for url in unpinned)
        return [
            f"Repository '{section_name}' is enabled in {source} with unpinned baseurl "
            f"{listed}; expected one of: {', '.join(sorted(declared))}"
        ]
    return []


def repo_security_option_errors(
    section_name: str,
    parser: configparser.ConfigParser,
    source: str,
    approved_security: dict[str, set[str]] | None = None,
) -> list[str]:
    """Name options that reroute or weaken an allowlisted repository's fetch.

    `proxy` and `sslverify=0` reroute or blind the fetch and are rejected for
    every allowlisted repository. `gpgcheck` (or its libdnf5 alias
    `pkg_gpgcheck`) and `repo_gpgcheck` disable RPM signature verification;
    they are rejected unless this repository is named in
    `[repositories.security]` with the option it is approved to leave disabled
    -- the digest-pinned utah-packages repo authenticates RPMs by its pinned
    image, and NVIDIA signs only its repomd.xml, so both are approved to drop a
    signature check that would otherwise be a gap (#345).
    """
    errors: list[str] = []
    approved = approved_security.get(section_name, set()) if approved_security else set()
    proxy = parser.get(section_name, "proxy", fallback="").strip()
    if proxy:
        errors.append(
            f"Allowlisted repository '{section_name}' is enabled in {source} with "
            f"proxy={proxy}; a proxy routes fetches through an origin the allowlist "
            "does not name"
        )
    sslverify = parser.get(section_name, "sslverify", fallback="").strip()
    if sslverify.lower() in DISABLED_VALUES:
        errors.append(
            f"Allowlisted repository '{section_name}' is enabled in {source} with "
            f"sslverify={sslverify}; disabling TLS verification accepts any certificate "
            "the origin presents"
        )
    for approval, keys in SIGNATURE_OPTION_KEYS.items():
        if approval in approved:
            continue
        # Every spelling is checked: pkg_gpgcheck=0 disables package signature
        # verification just as gpgcheck=0 does, whichever key comes last.
        for key in keys:
            value = parser.get(section_name, key, fallback="").strip()
            if value.lower() in DISABLED_VALUES:
                errors.append(
                    f"Allowlisted repository '{section_name}' is enabled in {source} with "
                    f"{key}={value}; disabling RPM signature verification accepts unsigned "
                    "metadata or packages; approve this origin's signature drift explicitly "
                    f"as '{approval}' in [repositories.security] if it is intended"
                )
    return errors


def check_repo_sections(
    parser: configparser.ConfigParser,
    source: str,
    allowed_repos: set[str],
    *,
    expected_baseurls: dict[str, tuple[str, ...]] | None,
    approved_security: dict[str, set[str]] | None = None,
) -> list[str]:
    """Apply the allowlist to every section of an already-parsed config."""
    errors: list[str] = []
    for section_name in parser.sections():
        if not is_repo_enabled(parser.get(section_name, "enabled", fallback="1")):
            if section_name in allowed_repos:
                errors.extend(
                    repo_security_option_errors(
                        section_name, parser, source, approved_security)
                )
                if expected_baseurls is not None:
                    errors.extend(repo_pin_errors(section_name, parser, source, expected_baseurls))
            continue
        if section_name in allowed_repos:
            errors.extend(
                repo_security_option_errors(section_name, parser, source, approved_security)
            )
        baseurl = parser.get(section_name, "baseurl", fallback="").lower()
        is_fedora = "fedora" in section_name.lower() or "fedora" in baseurl
        if is_fedora:
            errors.append(
                f"Fedora repository '{section_name}' is enabled in {source}; Fedora "
                "repositories are forbidden at runtime"
            )
        elif section_name not in allowed_repos:
            errors.append(
                f"Unapproved repository '{section_name}' is enabled in {source}; "
                f"allowed repositories: {sorted(allowed_repos)}"
            )
        elif expected_baseurls is not None:
            errors.extend(repo_pin_errors(section_name, parser, source, expected_baseurls))
    return errors


def builder_only_repo_files(repos_dir: Path) -> set[Path]:
    """Only skip marked files copied into builders, never into the final stage."""
    root = repos_dir.parent
    containerfile = root / "Containerfile"
    if not containerfile.is_file():
        return set()
    stages: list[set[Path]] = []
    text = containerfile.read_text(encoding="utf-8").replace("\\\n", " ")
    for line in text.splitlines():
        words = shlex.split(line, comments=True)
        if not words:
            continue
        if words[0].upper() == "FROM":
            stages.append(set())
        elif words[0].upper() == "COPY" and stages:
            # Cross-stage sources are not checkout paths and cannot authorize a skip.
            if any(word.startswith("--from=") for word in words):
                continue
            sources = [word for word in words[1:-1] if not word.startswith("--")]
            for source in sources:
                for path in root.glob(source):
                    if path.is_dir():
                        stages[-1].update(p.resolve() for p in path.rglob("*.repo"))
                    elif path.suffix == ".repo":
                        stages[-1].add(path.resolve())
    if not stages:
        return set()
    return set().union(*stages[:-1]) - stages[-1]


def verify_repository_policy(
    repos_dir: Path,
    allowed_repos: set[str],
    *,
    expected_baseurls: dict[str, tuple[str, ...]] | None,
    approved_security: dict[str, set[str]] | None = None,
    check_mode: bool = False,
) -> list[str]:
    """Prove the system exposes only explicitly allowed runtime RPM repositories.

    In check_mode, a marked builder-only file is skipped only when Containerfile
    copies it into a builder and not into the final runtime stage. A comment
    alone cannot exempt a runtime repository from policy.
    """
    errors: list[str] = []
    if not repos_dir.is_dir():
        return errors
    builder_files = builder_only_repo_files(repos_dir) if check_mode else set()
    for repo_file in sorted(repos_dir.glob("*.repo")):
        try:
            file_text = repo_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            errors.append(f"Could not read repo file {repo_file}: {e}")
            continue
        if check_mode and "# builder-only: true" in file_text:
            if repo_file.resolve() in builder_files:
                continue
            errors.append(f"Builder-only repository {repo_file.name} has no exclusive builder COPY")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(file_text)
        except Exception as e:  # noqa: BLE001 - report and continue scanning
            errors.append(f"Could not parse repo file {repo_file}: {e}")
            continue
        errors.extend(
            check_repo_sections(
                parser, str(repo_file), allowed_repos,
                expected_baseurls=expected_baseurls,
                approved_security=approved_security,
            )
        )
    return errors


def resolve_build_timestamp(environ: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Return the report's build stamp and where it was read from.

    A build that does not export SOURCE_DATE_EPOCH gets no timestamp at all.
    Stamping a fixed sentinel epoch instead was worse than recording nothing:
    the report asserted a build date (2004-12-28) that was never true, and the
    wall clock would make an otherwise reproducible report differ per build.
    """
    env = os.environ if environ is None else environ
    raw = env.get("SOURCE_DATE_EPOCH")
    if not raw:
        return None, "unset"
    try:
        epoch = int(raw)
        return (
            datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).isoformat(),
            "SOURCE_DATE_EPOCH",
        )
    except (ValueError, OverflowError, OSError):
        print(
            f"WARNING: unusable SOURCE_DATE_EPOCH={raw!r}; the package-origin report "
            "records no build timestamp",
            file=sys.stderr,
        )
        return None, "unusable"


def read_factory_pin(repo_file: Path = Path(FACTORY_REPO_PATH)) -> str | None:
    """The package factory digest stamped into the pinned repository file.

    Renovate's grouped factory-pin update moves this stamp together with the
    Containerfile's `ARG PACKAGE_IMAGE_SHA`, so it names the exact factory image
    the contract's NEVRAs were installed from.
    """
    try:
        text = repo_file.read_text(encoding="utf-8")
    except OSError:
        return None
    match = FACTORY_PIN_RE.search(text)
    return match.group("digest") if match else None


def generate_provenance_report(
    installed: dict[str, dict[str, Any]],
    flavor: str,
    allowed_repos: set[str],
    package_sections: dict[str, str],
    output_dir: Path = Path(DEFAULT_REPORT_DIR),
) -> dict[str, Any]:
    """Generate and retain the resolved package-origin/NEVRA report with build provenance."""
    packages_data: dict[str, dict[str, Any]] = {}
    factory_count = hummingbird_count = other_count = 0
    for name in sorted(installed.keys()):
        info = installed[name]
        origin = info["origin"]
        if origin == "factory":
            factory_count += 1
        elif origin == "hummingbird":
            hummingbird_count += 1
        else:
            other_count += 1
        packages_data[name] = {
            "name": info["name"], "epoch": info["epoch"], "version": info["version"],
            "release": info["release"], "arch": info["arch"], "nevra": info["nevra"],
            "origin": origin, "section": package_sections.get(name, "unknown"),
        }
        copies = installs_of(info)
        if len(copies) > 1:
            packages_data[name]["installs"] = [
                {"nevra": c["nevra"], "release": c["release"],
                 "arch": c["arch"], "origin": c["origin"]} for c in copies
            ]
    timestamp, timestamp_source = resolve_build_timestamp()
    # Which factory and which base the NEVRAs came from is the other half of
    # provenance: without it the report says what is installed but not what it
    # was composed from.
    factory_pin = read_factory_pin() or os.environ.get("PACKAGE_IMAGE_SHA") or None
    base_image = os.environ.get("BASE_IMAGE") or None
    base_image_digest = base_image.split("@", 1)[1] if base_image and "@" in base_image else None
    report: dict[str, Any] = {
        "build_provenance": {
            "flavor": flavor, "image": os.environ.get("IMAGE_NAME", "utah"),
            # The source commit, not VERSION: VERSION embeds the build date
            # (<stream>-YYYYMMDD-<sha>), so retaining it made the report, and
            # the layer carrying it, differ between otherwise identical
            # rebuilds on different days (#346).
            "commit": os.environ.get("SHA_HEAD_SHORT") or None,
            "timestamp": timestamp, "timestamp_source": timestamp_source,
            "factory_pin": factory_pin,
            "base_image": base_image, "base_image_digest": base_image_digest,
            "contract_packages": len(installed),
            "factory_packages_count": factory_count,
            "hummingbird_packages_count": hummingbird_count,
            "other_packages_count": other_count,
            "allowed_repositories": sorted(allowed_repos),
        },
        "packages": packages_data,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_file = output_dir / "package-origins.json"
    txt_file = output_dir / "package-origins.txt"
    json_file.write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Utah Package Origin and NEVRA Report", f"# Flavor: {flavor}",
        f"# Contract packages: {len(installed)}", f"# Factory rebuilds (.bfin): {factory_count}",
        f"# Hummingbird packages (.hum): {hummingbird_count}", f"# Other: {other_count}",
        f"# Factory pin: {factory_pin or 'unknown'}",
        f"# Base image: {base_image or 'unknown'}",
        f"# Generated: {timestamp or 'unstamped (' + timestamp_source + ')'}", "",
        f"{'NAME':<35} {'NEVRA':<50} {'ORIGIN':<15} {'SECTION':<15}",
        f"{'-'*35} {'-'*50} {'-'*15} {'-'*15}",
    ]
    for name, data in packages_data.items():
        for copy in data.get("installs") or [data]:
            lines.append(
                f"{data['name']:<35} {copy['nevra']:<50} "
                f"{copy['origin']:<15} {data['section']:<15}"
            )
    txt_file.write_text("\n".join(lines) + "\n")
    return report


def _parse_sections(overlay: Path, resolved: list[str] | None, manifest: Path) -> tuple[list, list, list, list, list]:
    """Split the contract into bluefin/gnome/parity/hardware/services buckets.

    When `resolved` is given (an image build), the install set is the source of
    truth -- asserting it avoids the drift where install added something this
    check never recomputed. Otherwise the manifests are the source: the
    bluefin parity packages live in the manifest's [fedora] section, and the
    desktop/parity/service packages are read straight from the overlay sections.
    """
    if resolved is not None:
        gnome_names = set(section(overlay, "gnome"))
        parity_names = set(section(overlay, "parity"))
        hardware_names = set(section(overlay, "hardware"))
        service_names = set(section(overlay, "services"))
        overlay_names = gnome_names | parity_names | hardware_names | service_names
        return (
            [p for p in resolved if p not in overlay_names],
            [p for p in resolved if p in gnome_names],
            [p for p in resolved if p in parity_names],
            [p for p in resolved if p in hardware_names],
            [p for p in resolved if p in service_names],
        )
    unavailable = set(section(overlay, "unavailable"))
    return (
        [p for p in section(manifest, "fedora") if p not in unavailable],
        section(overlay, "gnome"),
        section(overlay, "parity"),
        section(overlay, "hardware"),
        section(overlay, "services"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--no-report", action="store_true",
        help="verify only; do not write the retained provenance report.",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("overlay", type=Path, nargs="?", default=None)
    args = parser.parse_args()
    overlay = args.overlay or args.manifest.with_name("utah.toml")

    if not overlay.exists():
        print(f"ERROR: Overlay manifest '{overlay}' does not exist", file=sys.stderr)
        return 1

    flavor = os.environ.get("IMAGE_FLAVOR", "main")
    # Prefer the set install-packages.py actually resolved. Recomputing it here
    # is what let the two drift once: install added [fedora_v<major>] for the
    # running release and this check never did, so a contract package was
    # installed but never verified. The manifest path is the off-image fallback.
    resolved = Path(CONTRACT_PATH)
    if resolved.exists():
        contract = [line for line in resolved.read_text().split() if line]
        bluefin, gnome, parity, hardware, services = _parse_sections(overlay, contract, args.manifest)
    else:
        bluefin, gnome, parity, hardware, services = _parse_sections(overlay, None, args.manifest)
    nvidia = list(NVIDIA_PACKAGES) if "nvidia" in flavor else []
    expected = [*bluefin, *gnome, *parity, *hardware, *services, *nvidia]

    overlay_data = tomllib.loads(overlay.read_text())
    try:
        major_versions = overlay_data["gnome"]["versions"]
    except KeyError:
        print(f"ERROR: Overlay manifest '{overlay}' is missing [gnome.versions] section",
              file=sys.stderr)
        return 1
    try:
        allowed_repos = set(overlay_data["repositories"]["allowed"])
    except KeyError:
        print(f"ERROR: Overlay manifest '{overlay}' is missing [repositories.allowed] section",
              file=sys.stderr)
        return 1
    try:
        repo_baseurls = {
            repo_id: tuple(urls)
            for repo_id, urls in overlay_data["repositories"]["baseurls"].items()
        }
    except KeyError:
        print(f"ERROR: Overlay manifest '{overlay}' is missing [repositories.baseurls] section",
              file=sys.stderr)
        return 1
    unpinned = sorted(allowed_repos - repo_baseurls.keys())
    if unpinned:
        print(
            f"ERROR: Overlay manifest '{overlay}' allows repositories with no pinned "
            f"baseurl in [repositories.baseurls]: {', '.join(unpinned)}",
            file=sys.stderr,
        )
        return 1
    # Approved per-repository signature/TLS drift, from [repositories.security].
    # A repository named here may leave the listed option (gpgcheck/repo_gpgcheck)
    # disabled; no allowlisted repository not named here may explicitly disable
    # signature verification. A key not in the allowlist approves nothing, so reject it
    # like an unpinned baseurl (#345).
    repo_security_raw = overlay_data["repositories"].get("security", {})
    if not isinstance(repo_security_raw, dict):
        print(
            f"ERROR: Overlay manifest '{overlay}' has a non-table [repositories.security] "
            "section",
            file=sys.stderr,
        )
        return 1
    approved_security: dict[str, set[str]] = {}
    for repo_id, options in repo_security_raw.items():
        if not isinstance(options, list):
            print(
                f"ERROR: Overlay manifest '{overlay}' lists [repositories.security].{repo_id} "
                "as a non-list; name the options approved to be disabled",
                file=sys.stderr,
            )
            return 1
        unknown = sorted(
            repr(opt) for opt in options if opt not in APPROVABLE_SECURITY_OPTIONS
        )
        if unknown:
            print(
                f"ERROR: Overlay manifest '{overlay}' lists unknown options in "
                f"[repositories.security].{repo_id}: {', '.join(unknown)}; only "
                f"{', '.join(APPROVABLE_SECURITY_OPTIONS)} may be approved",
                file=sys.stderr,
            )
            return 1
        approved_security[repo_id] = set(options)
    unapproved_keys = sorted(set(approved_security) - allowed_repos)
    if unapproved_keys:
        print(
            f"ERROR: Overlay manifest '{overlay}' approves signature drift for repositories "
            f"not in [repositories.allowed]: {', '.join(unapproved_keys)}",
            file=sys.stderr,
        )
        return 1
    factory_packages = set(section(overlay, "factory"))
    factory_parity = set(section(overlay, "factory", "parity"))
    # [factory].packages is the GNOME identity contract; other sections use
    # explicit factory buckets (currently parity), never an inert declaration.
    for pkg in factory_packages:
        assert pkg in set(section(overlay, "gnome")), (
            f"Factory package '{pkg}' is not declared in the [gnome] section"
        )
    for pkg in factory_parity:
        assert pkg in set(section(overlay, "parity")), (
            f"Factory parity package '{pkg}' is not declared in the [parity] section"
        )

    package_sections: dict[str, str] = {}
    for p in bluefin:
        package_sections[p] = "bluefin"
    for p in gnome:
        package_sections[p] = "gnome"
    for p in parity:
        package_sections[p] = "parity"
    for p in hardware:
        package_sections[p] = "hardware"
    for p in services:
        package_sections[p] = "services"
    for p in nvidia:
        package_sections[p] = "nvidia"

    print(
        f"Verifying {len(bluefin)} Bluefin packages, {len(gnome)} GNOME desktop packages,"
        f" {len(parity)} parity packages, {len(hardware)} firmware packages,"
        f" {len(services)} desktop service packages, and {len(nvidia)} NVIDIA packages",
        flush=True,
    )
    if args.check:
        assert len(set(expected)) == len(expected), "RPM contract contains duplicate package names"
        for pkg in factory_packages:
            assert pkg in gnome, f"Factory package '{pkg}' is not declared in the [gnome] section"
        for pkg in factory_parity:
            assert pkg in parity, (
                f"Factory parity package '{pkg}' is not declared in the [parity] section"
            )
        # A [gnome.versions] key that names no [gnome] package asserts nothing:
        # verify_gnome_contract looks versions up by package name, so a typo
        # would silently drop that package's major-version claim on-image.
        gnome_names = set(gnome)
        for pkg in major_versions:
            assert pkg in gnome_names, (
                f"[gnome.versions] key '{pkg}' is not declared in the [gnome] section"
            )
        repo_errors = verify_repository_policy(
            args.manifest.parent, allowed_repos,
            expected_baseurls=repo_baseurls,
            approved_security=approved_security,
            check_mode=True,
        )
        if repo_errors:
            for err in repo_errors:
                print(f"ERROR: {err}", file=sys.stderr)
            return 1
        print(
            "RPM contract valid; repository policy holds for the runtime .repo files in "
            f"{args.manifest.parent}."
        )
        return 0

    missing = [pkg for pkg in expected if not is_installed(pkg)]
    if missing:
        print(f"ERROR: {len(missing)} of {len(expected)} contract packages are not installed:",
              file=sys.stderr)
        for pkg in missing:
            print(f"  - {pkg}", file=sys.stderr)
        return 1
    print(f"All {len(expected)} contract packages are present.")

    installed, missing_nevra = query_packages(expected)
    if missing_nevra:
        print(
            f"ERROR: {len(missing_nevra)} of {len(expected)} contract packages could not be "
            "queried via RPM:",
            file=sys.stderr,
        )
        for pkg in missing_nevra:
            print(f"  - {pkg}", file=sys.stderr)
        return 1

    attestation_errors: list[str] = []
    attestation_errors.extend(
        verify_gnome_contract(gnome, installed, major_versions, factory_packages)
    )
    attestation_errors.extend(verify_parity_origin(parity, installed, factory_parity))

    if attestation_errors:
        print(
            f"ERROR: {len(attestation_errors)} supply-chain / repository contract violation(s):",
            file=sys.stderr,
        )
        for err in attestation_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    # Attest the composed image's runtime RPM repositories against the same
    # allowlist --check already applies to packages/ (#454, #513). The
    # Hummingbird base image ships its own repo files; without this scan they
    # pass into the runtime unattested -- the issue's "fedora or any unapproved
    # enabled RPM repository" claim covers them too. check_mode=False because the
    # runtime image never carries a builder-only repo file; the v4l2loopback
    # stage's fedora-44.repo is never copied into this layer (Containerfile, v4l2
    # stage). The scanned dirs are derived from dnf5's actual configuration
    # rather than the three documented defaults (#513, #536): a base image can
    # override the list with `reposdir=` in /etc/dnf/dnf.conf or a libdnf5
    # drop-in (see dnf5_config_files), in which case the hardcoded list misses
    # the configured paths and a `.repo` file placed there bypasses the
    # allowlist.
    try:
        runtime_repos_dirs = runtime_reposdir_paths()
    except Dnf5ConfigError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    repo_errors: list[str] = []
    for repos_dir in runtime_repos_dirs:
        repo_errors.extend(
            verify_repository_policy(
                repos_dir, allowed_repos,
                expected_baseurls=repo_baseurls,
                approved_security=approved_security, check_mode=False,
            )
        )
    # A proxy= or sslverify=0 in the resolved [main] section of dnf.conf/libdnf5.conf
    # applies to every allowlisted repository, so the per-section check above never
    # inspects it. Resolve the [main] options the way libdnf5 does and report the
    # effective values (utah#352, adjacent to #339).
    try:
        repo_errors.extend(
            main_section_security_errors(dnf5_config_files(), "dnf5 [main] config")
        )
    except Dnf5ConfigError as err:
        repo_errors.append(str(err))
    if repo_errors:
        for err in repo_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    report_dir = Path(os.environ.get("UTAH_REPORT_DIR", DEFAULT_REPORT_DIR))
    report = None
    if not args.no_report:
        try:
            report = generate_provenance_report(
                installed, flavor, allowed_repos, package_sections, report_dir
            )
        except OSError as err:
            print(
                f"ERROR: could not retain provenance report in {report_dir}: {err}",
                file=sys.stderr,
            )
            return 1
    print(
        f"All {len(expected)} contract packages verified (GNOME versions, factory rebuilds, "
        "parity origin)."
    )
    if report is None:
        print("Skipped provenance report retention (--no-report).")
    else:
        print(
            f"Retained provenance report for {len(installed)} packages "
            f"({report['build_provenance']['factory_packages_count']} factory, "
            f"{report['build_provenance']['hummingbird_packages_count']} hummingbird) "
            f"in {report_dir / 'package-origins.json'}."
        )

    if "nvidia" not in flavor:
        return 0

    # Assert what a source build actually produces: a module for every kernel
    # the image can boot, and the userspace that goes with it.
    ogc = Path("/usr/lib/utah/ogc-kernel-release")
    ogc_release = ogc.read_text().strip() if ogc.exists() else None

    # This deliberately mirrors install-nvidia.sh, including its fallback. `rpm
    # -q kernel` is not reliable here: `kernel` is a metapackage a bootc base may
    # not carry, and on failure rpm prints "package kernel is not installed" to
    # stdout -- whose last word is "installed", which this used to accept as a
    # release string and then report a missing module for a kernel of that name.
    base = subprocess.run(["rpm", "-q", "kernel", "--qf", "%{VERSION}-%{RELEASE}.%{ARCH}\n"],
                          capture_output=True, text=True).stdout.split()
    base = base[-1].strip() if base else ""
    # Identify the kernel by its module tree, not by a build tree. A build tree
    # only exists while kernel-devel is installed, and install-nvidia.sh removes
    # that again once the module is compiled -- 215 MiB there is no reason to
    # ship. Requiring one here meant plain nvidia could never pass: the OGC
    # flavors only satisfied it because install-ogc-kernel.sh leaves its own
    # tree behind. A module tree is what says the image can boot that kernel,
    # which is the thing being asserted.
    if not base or not Path(f"/usr/lib/modules/{base}").is_dir():
        candidates = sorted(d.name for d in Path("/usr/lib/modules").glob("*")
                            if d.name != ogc_release and d.is_dir())
        if not candidates:
            print("ERROR: no kernel module tree found; cannot verify NVIDIA modules",
                  file=sys.stderr)
            return 1
        base = candidates[-1]

    releases = [base]
    if flavor == "nvidia-gaming":
        releases.append(ogc.read_text().strip())

    failed = False
    for release in releases:
        release = release.strip() if release else ""
        if not release:
            print("ERROR: empty kernel release; no kernel to check NVIDIA module against",
                  file=sys.stderr)
            failed = True
            continue
        module = Path(f"/usr/lib/modules/{release}/extra/nvidia/nvidia.ko")
        if not module.exists():
            print(f"ERROR: NVIDIA module missing for kernel {release}", file=sys.stderr)
            failed = True
    for path in (Path("/usr/bin/nvidia-smi"), Path("/usr/lib/utah/nvidia-driver-version")):
        if not path.exists():
            print(f"ERROR: NVIDIA userspace incomplete, {path} is missing", file=sys.stderr)
            failed = True
    if failed:
        return 1
    version = Path("/usr/lib/utah/nvidia-driver-version").read_text().strip()
    print(f"NVIDIA {version} present for: {', '.join(releases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
