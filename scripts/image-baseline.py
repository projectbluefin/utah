#!/usr/bin/env python3
"""Measure Utah against what Dakota actually ships.

Dakota (projectbluefin/dakota, GNOME OS built with BuildStream) is Utah's only
parity target. packages/base.toml is Utah's own frozen manifest and says nothing
about what Dakota installs, so the comparison reads the published images:

  extract IMAGE DIR   the user-visible files inside IMAGE (desktop entries,
                      autostarts, sessions, systemd units, /usr/bin and
                      /usr/sbin) with an owner for each. An image with an RPM
                      database (Utah) also gets rpms.tsv and rpm owners. One
                      without (Dakota) gets the owning element guessed from
                      DIR/elements.tsv, or "(dakota)". Needs podman. The
                      committed snapshots only pick up a new glob on the next
                      run of this command.
  dakota RUN DIR      element list from the SPDX SBOM that Dakota's publish
                      workflow uploads (artifact sbom-dakota). Run it before
                      `extract` so the owner guesses use the current list.
  gap                 write baselines/GAP.md: Dakota's user-visible paths that
                      are absent from Utah, grouped by triage key (the guessed
                      element, or the path itself when no element matched).
  check               fail when a gap key is not listed in baselines/triage.toml,
                      so a new gap cannot land silently. Listed gaps pass: the
                      file is the tracked parity debt, each entry with a status.

Refresh all snapshots with `just baselines`.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "baselines"
TRIAGE = BASE / "triage.toml"
STATUSES = {"planned", "waived", "unavailable", "untriaged"}
UNMAPPED = "(dakota)"

# Images without an RPM database (Dakota) report every owner as "(norpm)";
# extract() replaces it with the element guess.
EXTRACT = r"""
set -euo pipefail
if command -v rpm >/dev/null 2>&1; then
  rpm -qa --qf '%{NAME}\t%{VERSION}-%{RELEASE}\n' | sort -u > /out/rpms.tsv
  owner() {
    o=$(rpm -qf --qf '%{NAME}\n' "$1" 2>/dev/null | head -1) || o=""
    case "$o" in ""|*"not owned"*) o="(unowned)";; esac
    printf '%s' "$o"
  }
else
  owner() { printf '(norpm)'; }
fi
for pat in /usr/share/applications/*.desktop /etc/xdg/autostart/*.desktop \
           /usr/share/gdm/greeter/autostart/*.desktop /usr/share/wayland-sessions/*.desktop \
           /usr/share/gnome-session/sessions/*.session \
           /usr/lib/systemd/system/*.service /usr/lib/systemd/system/*.socket \
           /usr/lib/systemd/system/*.timer /usr/lib/systemd/user/*.service \
           /usr/lib/systemd/user/*.socket /usr/bin/* /usr/sbin/*; do
  for f in $pat; do
    [ -e "$f" ] || continue
    printf '%s\t%s\n' "$(owner "$f")" "$f"
  done
done | sort -u > /out/surface.tsv
"""

# Order matters: the first match names the kind in the report.
KINDS = (
    ("session", ("/wayland-sessions/", "/gnome-session/sessions/")),
    ("app", ("/usr/share/applications/",)),
    ("autostart", ("/autostart/",)),
    ("user unit", ("/systemd/user/",)),
    ("system unit", ("/systemd/system/",)),
    ("command", ("/usr/bin/", "/usr/sbin/")),
)


def kind(path: str) -> str:
    return next(k for k, marks in KINDS if any(m in path for m in marks))


def read_tsv_text(text: str) -> list[list[str]]:
    return [line.split("\t") for line in text.splitlines()
            if line and not line.startswith("#")]


def read_tsv(path: Path) -> list[list[str]]:
    return read_tsv_text(path.read_text())


def element_names(path: Path) -> list[str]:
    """Element names from an elements.tsv, longest first."""
    if not path.exists():
        return []
    names = {row[1] for row in read_tsv(path) if len(row) > 1 and row[1]}
    return sorted(names, key=lambda n: (-len(n), n))


UNIT_SUFFIXES = (".service", ".socket", ".timer", ".desktop", ".session")

# File families whose names do not start with the element that installs them.
# Each regex must match the whole stem; an element absent from elements.tsv is
# skipped, so a renamed element degrades to a path key rather than a wrong one.
ALIASES = tuple((re.compile(rx), element) for rx, element in (
    (r"aa-.*|apparmor_.*", "apparmor"),
    (r"bsd(cat|cpio|tar|unzip)", "libarchive"),
    (r"card_eventmgr|pkcs11[_-]eventmgr|pkcs11_.*|pklogin_finder", "pam-pkcs11"),
    (r"pkcs1[15]-.*|(cardos|cryptoflex|dnie|dtrust|egk|gids|goid|iasecc|netkey|openpgp|piv"
     r"|sc-hsm|westcos)-tool|eidenv|org\.opensc\..*", "opensc"),
    (r"dbus-org\.freedesktop\.impl\.portal\.desktop\.oo7", "oo7-portal"),
    (r"dbus-org\.freedesktop\.secrets", "oo7-daemon"),
    (r"hyperv.*|hv_.*|lsvmbus", "hyperv-daemons"),
    (r"vmtoolsd|vmware-.*|vmhgfs-fuse|vm-support|vmwgfxctrl", "open-vm-tools"),
    (r"snap.*|io\.snapcraft\..*", "snapd"),
    (r"iscsi.*|brcm_iscsiuio", "open-iscsi"),
    (r"isns.*", "open-isns"),
    (r"kmscon.*", "kmscon"),
    (r"nm-cloud-setup|nmtui.*", "NetworkManager"),
    (r"virsh|virt(interface|lock|log|network|nodedev|nwfilter|proxy|qemu|secret|storage|vbox)d"
     r"(-.*)?|virt-(admin|host-validate|login-shell|pki-.*|qemu-.*|ssh-helper|xml-validate"
     r"|secret-init-encryption)", "libvirt"),
    (r"dbus-org\.freedesktop\.[a-z]+1|networkctl", "systemd"),
    (r"dbus-org\.bluez\..*", "bluez"),
    (r"powerprofilesctl", "power-profiles-daemon"),
    (r"ip6?tables-.*|xtables-.*", "iptables"),
    (r"ebtables.*", "ebtables"),
    (r"speex(dec|enc)", "speex"),
    (r"pcre2(grep|test)", "pcre2"),
    (r"perl(bug|ivp|thanks)", "perl"),
    (r"fftwf?-wisdom.*", "fftw"),
    (r"jpegtran", "jpeg"),
    (r"makeconv", "icu"),
    (r"makedb", "glibc"),
    (r"podmansh", "podman"),
    (r"wget", "wget2"),
    (r"usbredirect", "usbredir"),
    (r"pngfix|png-fix-itxt", "libpng"),
    (r"a2x", "asciidoc"),
    (r"gvim", "vim"),
    (r"org\.gnome\.Mutter\.Mdk", "mutter"),
    (r"org\.projectbluefin\.dakota\.TailscaleSetup", "tailscale"),
    (r"elf2dmp", "qemu"),
    (r"wpa_supplicant.*", "wpa-supplicant"),
    (r"uutils-.*", "uutils-coreutils"),
    (r"mtp-.*", "libmtp"),
    (r"nl-.*|nf-.*|genl-ctrl-list|idiag-socket-details", "libnl"),
    (r"kc[a-z]+(mgr|test)", "kyotocabinet"),
    (r"smbios-.*", "libsmbios"),
    (r"tiff.*|fax2(ps|tiff)|pal2rgb|ppm2tiff|raw2tiff", "libtiff"),
    (r"eu-.*", "elfutils"),
    (r"gprofng.*|gp-.*", "binutils"),
    (r"x86_64-unknown-linux-gnu-.*|gfortran", "gcc"),
    (r"gst-.*", "gstreamer"),
    (r"ges-launch-.*", "gst-editing-services"),
    (r"v4l2-.*|cec-.*|dvb.*|ir-(ctl|keytable)|(media|rds|cx18|ivtv)-ctl|decode_tm6000", "v4l-utils"),
    (r"m17n-.*", "m17n-lib"),
    (r"rime_.*", "librime"),
    (r"protoc.*", "protobuf"),
    (r"xxh.*", "xxhash"),
    (r"lz(cat|cmp|diff|e?grep|fgrep|less|more|ma.*)|unlzma", "xz"),
    (r"lz4c.*|unlz4", "lz4"),
    (r"dot(2gxl|_builtins|_sandbox)?|neato|twopi|circo|s?fdp|osage|patchwork|gv(color|gen|map"
     r"|map\.sh|pack|pr)|gv2g(ml|xl)|gml2gv|graphml2gv|gxl2.*|mm2gv|acyclic"
     r"|[bc]comps|cluster|dijkstra|edgepaint|nop|prune|sccmap|tred|unflatten", "graphviz"),
    (r"addftinfo|afmtodit|chem|eqn2graph|gdiffmk|glilypond|gperl|gpinyin|grap2graph|grn"
     r"|gro(dvi|g|lbp|lj4|pdf|ps|tty|html)"
     r"|hpftodit|indxbib|lkbib|lookbib|mmroff|pdfmom|pfbtops|pic2graph|refer|tfmtodit", "groff"),
    (r"ldap.*", "openldap"),
    (r"kadmin.*|kdb5_util|kprop.*|krb5.*|gss-(client|server)|s(client|server)|sim_(client|server)"
     r"|uu(client|server)", "krb5"),
    (r"sepol_.*", "libsepol"),
    (r"compute_.*|get(conlist|defaultcon|filecon|pidcon|seuser)|policyvers|selinux_.*|setfilecon"
     r"|togglesebool", "libselinux"),
    (r"gpg.*|kbxutil|watchgnupg|addgnupghome|applygnupgdefaults", "gnupg"),
    (r"dumpsexp|hmac256|mpicalc", "libgcrypt"),
    (r"fy-.*", "libfyaml"),
    (r"g-ir-.*", "gobject-introspection"),
    (r"gi-.*|gdbus-codegen|gresource|gio-querymodules|gobject-query|gtester.*", "glib"),
    (r"gtk4-.*", "gtk"),
    (r"hb-.*", "harfbuzz"),
    (r"[cd]webp|(gif|img)2webp|webp(info|mux)", "libwebp"),
    (r"gif(build|clrmp|fix|text|tool)", "giflib"),
    (r"[cd]jpeg|(rd|wr)jpgcom|tjbench", "jpeg"),
    (r"derb|gen(brk|ccode|cfu|cmn|cnval|dict|norm2|rb|sprep)|icu.*|pkgdata|uconv|escapesrc", "icu"),
    (r".*\.erofs|erofsfuse", "erofs-utils"),
    (r"xfs_.*", "xfsprogs"),
    (r"e2scrub.*|compile_et|mk_cmds", "e2fsprogs"),
    (r"nfs-(cat|cp|ls|stat)", "libnfs"),
    (r"nfsdcl.*|nfsiostat|mountstats|rpcctl", "nfs-utils"),
    (r"(integrity|verity)setup", "cryptsetup"),
    (r"fido2-.*", "libfido2"),
    (r"pcsc-spy", "pcsc-lite"),
    (r"smbtorture|ndrdump|masktest|locktest|ldb.*|tdb.*", "samba"),
    (r"(test)?saslauthd|pluginviewer", "cyrus-sasl"),
    (r"gphoto2-.*", "libgphoto2"),
    (r"gsf.*", "libgsf"),
    (r"idn2", "libidn2"),
    (r"psl|psl-make-dafsa", "libpsl"),
    (r"(jpg|link|ps|tif|trans)icc", "lcms"),
    (r"opj_.*", "openjpeg"),
    (r"vpx(dec|enc)", "libvpx"),
    (r"aom(dec|enc)", "aom"),
    (r"SvtAv1EncApp", "svt-av1"),
    (r"(meta)?flac", "libflac"),
    (r"sbc(dec|enc|info)", "sbc"),
    (r"e?speak", "espeak-ng"),
    (r"hunzip|hzip|munch|unmunch|affixcompress|makealias|chmorph|ispellaff2myspell|wordforms"
     r"|wordlist2hunspell", "hunspell"),
    (r"trietool.*", "libdatrie"),
    (r"gxps|xpsto.*", "libgxps"),
    (r"cpan|corelist|enc2xs|h2ph|h2xs|instmodsh|json_pp|libnetcfg|pl2pm|pod2html|podchecker|prove"
     r"|ptar.*|shasum|splain|streamzip|xsubpp|zipdetails", "perl"),
    (r"dulwich|dul-.*", "python3-dulwich"),
    (r"markdown_py", "python3-markdown"),
    (r"ei-debug-events", "libei"),
    (r"plist2json", "libplist"),
    (r"iproxy|inetcat", "libusbmuxd"),
    (r"l2ping|l2test|rctest", "bluez"),
    (r"numa(demo|stat)|memhog|migratepages|migspeed", "numactl"),
    (r"pscap|netcap|filecap|captest", "libcap-ng"),
    (r"scmp_sys_resolver", "libseccomp"),
    (r"audisp-.*", "audit"),
    (r"zip(cmp|merge|tool)", "libzip"),
    (r"asn1.*", "libtasn1"),
    (r"certtool|ocsptool|p11tool|psktool|srptool", "gnutls"),
    (r"sexp-conv|pkcs1-conv", "nettle"),
    (r"rsvg-convert", "librsvg"),
    (r"gusbcmd", "libgusb"),
    (r"dvipdf|gpcl6", "ghostscript"),
    (r"ippeveprinter|lprsetup\.sh|unix-lpr\.sh|install-printerdriver", "cups"),
    (r"whiptail", "newt"),
    (r"script.*|uuidd|mkfs\.bfs", "util-linux"),
    (r"mtrace|sln|nscd|xtrace|pcprofiledump", "glibc"),
    (r"luac", "lua"),
    (r"tc|routel", "iproute2"),
    (r"faillog", "shadow"),
    (r"uncompress", "gzip"),
    (r"wcurl", "curl"),
    (r"orcc", "orc"),
    (r"vala(c.*)?|vapigen.*", "vala"),
    (r"paper", "libpaper"),
    (r"bd_.*", "libbluray"),
    (r"libcamerify", "libcamera"),
    (r"virgl_test_server", "virglrenderer"),
    (r"wlfreerdp.*|winpr-.*", "freerdp"),
    (r"flashfetch", "fastfetch"),
    (r"tldr", "tealdeer"),
    (r"mouse-dpi-tool|touchpad-edge-detector", "libinput"),
    (r"marisa-.*", "marisa-trie"),
    (r"gen_(binary_files|unigram)", "libpinyin"),
    (r"th_gen_idx\.pl", "mythes"),
    (r"(analyse|apply|list)plugin(s)?", "ladspa-sdk"),
    (r"slsh", "slang"),
    (r"pre(cat|unzip|zip.*)|word-list-compress|run-with-aspell", "aspell"),
    (r"efisecdb", "efivar"),
    (r"event_rpcgen\.py", "libevent"),
    (r"fdt.*|convert-dtsv0|dtdiff", "dtc"),
    (r"pcap-config", "libpcap"),
    (r"gnome-help", "yelp"),
    (r"bst", "buildstream2"),
    (r"cas(download|upload)|logstream.*|outputstreamer|trexe", "buildbox"),
    (r"recc", "recc-wrapper"),
    (r"sysext-.*", "sysext-utils"),
    (r"system-reinstall-bootc", "bootc"),
    (r"xmlif", "xmlto"),
))


def guess_element(path: str, names: list[str]) -> str:
    """The Dakota element that most likely installs path, or "".

    The SBOM names elements, not files. A family in ALIASES wins; otherwise
    take the longest element name that equals the file's stem or prefixes it
    up to a separator ("-", "_", ".", "@", a digit) or a daemon "d" (libvirt ->
    libvirtd, sudo -> sudo_logsrvd, but not make -> makeconv). A reverse-DNS
    desktop ID also tries its last component (com.mitchellh.ghostty -> ghostty).
    """
    stem = path.rsplit("/", 1)[-1]
    for suffix in UNIT_SUFFIXES:
        stem = stem.removesuffix(suffix)
    stem = stem.rstrip("@")
    known = set(names)
    for rx, element in ALIASES:
        if element in known and rx.fullmatch(stem):
            return element
    candidates = [stem]
    if path.endswith(".desktop") and "." in stem:
        candidates.append(stem.rsplit(".", 1)[-1].lower())
    for cand in candidates:
        for name in names:
            rest = cand[len(name):]
            if cand.startswith(name) and (rest in ("", "d") or rest[0] in "-_.@0123456789"
                                          or (rest[0] == "d" and rest[1] in "-_.@")):
                return name
    return ""


def extract(image: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    digest = subprocess.run(
        ["podman", "image", "inspect", "--format", "{{.Digest}}", image],
        capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "x.sh"
        script.write_text(EXTRACT)
        # --network=none: the script reads the image's own files and nothing
        # else, and rootless podman without slirp4netns cannot run otherwise.
        subprocess.run(["podman", "run", "--rm", "--network=none", "--entrypoint", "/bin/bash",
                        "-v", f"{script}:/x.sh:ro,z", "-v", f"{tmp}:/out:z",
                        image, "/x.sh"], check=True)
        rpms = Path(tmp) / "rpms.tsv"
        if rpms.exists():
            (out / "rpms.tsv").write_text(rpms.read_text())
        surface = (Path(tmp) / "surface.tsv").read_text()
    if "(norpm)\t" in surface:
        names = element_names(out / "elements.tsv")
        rows = []
        for owner, path in read_tsv_text(surface):
            if owner == "(norpm)":
                owner = guess_element(path, names) or UNMAPPED
            rows.append(f"{owner}\t{path}")
        surface = "".join(f"{r}\n" for r in sorted(set(rows)))
    (out / "surface.tsv").write_text(surface)
    (out / "image.txt").write_text(f"{image.split('@')[0]}@{digest}\n" if digest else f"{image}\n")


def dakota(run: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["gh", "run", "download", run, "-R", "projectbluefin/dakota",
                        "-n", "sbom-dakota", "-D", tmp], check=True)
        sbom = json.loads((Path(tmp) / "dakota.sbom.json").read_text())
    rows = set()
    for pkg in sbom["packages"]:
        ref = pkg["SPDXID"].removeprefix("SPDXRef-")
        if not ref.endswith(".bst"):
            continue  # "-N" entries are the sources of an element, not elements
        rows.add((ref, pkg.get("name") or "", pkg.get("versionInfo") or ""))
    # Drop trailing empty cells so the file carries no trailing tabs; the
    # trailing-whitespace pre-commit hook otherwise flips this file on every
    # regeneration (only 1146 of 1147 lines have a third column today).
    def _trim(*row: str) -> str:
        cells = list(row)
        while cells and not cells[-1]:
            cells.pop()
        return "\t".join(cells)
    lines = ["# element\tname\tversion"] + [_trim(*r) for r in sorted(rows)]
    (out / "elements.tsv").write_text("\n".join(lines) + "\n")
    (out / "source.txt").write_text(
        f"projectbluefin/dakota publish.yml run {run}, artifact sbom-dakota\n")


def merged(path: str) -> str:
    """/usr/sbin is a symlink to /usr/bin on both images; compare one spelling."""
    return "/usr/bin/" + path[len("/usr/sbin/"):] if path.startswith("/usr/sbin/") else path


def gaps() -> dict[str, list[str]]:
    """Dakota's user-visible paths Utah lacks, keyed by element guess or path."""
    utah_paths = {merged(r[1]) for r in read_tsv(BASE / "utah/surface.tsv")}
    out: dict[str, set[str]] = defaultdict(set)
    for owner, path in read_tsv(BASE / "dakota/surface.tsv"):
        if merged(path) not in utah_paths:
            out[merged(path) if owner == UNMAPPED else owner].add(merged(path))
    return {k: sorted(v) for k, v in sorted(out.items())}


def triage() -> dict[str, dict]:
    data = tomllib.loads(TRIAGE.read_text()) if TRIAGE.exists() else {}
    return data.get("item", {})


def write_report() -> None:
    found, notes = gaps(), triage()
    by_status: dict[str, list[str]] = defaultdict(list)
    for key in found:
        by_status[notes.get(key, {}).get("status", "not in triage.toml")].append(key)
    lines = [
        "# Utah vs Dakota: user-visible gaps",
        "",
        "Generated by `scripts/image-baseline.py gap`; do not edit.",
        "",
        f"- Dakota: `{(BASE / 'dakota/image.txt').read_text().strip()}`",
        f"- Dakota elements: {(BASE / 'dakota/source.txt').read_text().strip()}",
        f"- Utah: `{(BASE / 'utah/image.txt').read_text().strip()}`",
        "",
        f"{len(found)} items ship user-visible files in Dakota that Utah lacks. "
        "An item is the Dakota element guessed to install the files, or the "
        "path itself when no element name matched.",
        "",
        "| status | count |", "| --- | ---: |",
        *(f"| {s} | {len(k)} |" for s, k in sorted(by_status.items())),
        "",
        "| item | status | what is missing | note |",
        "| --- | --- | --- | --- |",
    ]
    for key, paths in found.items():
        entry = notes.get(key, {})
        kinds: dict[str, int] = defaultdict(int)
        for p in paths:
            kinds[kind(p)] += 1
        what = ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items())
        note = entry.get("reason", "")
        if entry.get("issue"):
            note = f"{note} ({entry['issue']})".strip()
        lines.append(f"| {key} | {entry.get('status', '**new**')} | {what} | {note} |")
    (BASE / "GAP.md").write_text("\n".join(lines) + "\n")


def check() -> int:
    found, notes = gaps(), triage()
    bad = [f"{p}: status {e.get('status')!r} is not one of {sorted(STATUSES)}"
           for p, e in notes.items() if e.get("status") not in STATUSES]
    new = [p for p in found if p not in notes]
    if new:
        bad.append("Dakota items missing from Utah and not in baselines/triage.toml: "
                   + ", ".join(new))
    for msg in bad:
        print(f"ERROR: {msg}", file=sys.stderr)
    if not bad:
        closed = [p for p in notes if p not in found]
        print(f"{len(found)} gaps, all triaged"
              + (f"; no longer gaps, drop from triage.toml: {', '.join(closed)}" if closed else ""))
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract"); e.add_argument("image"); e.add_argument("out", type=Path)
    d = sub.add_parser("dakota"); d.add_argument("run"); d.add_argument("out", type=Path)
    sub.add_parser("gap"); sub.add_parser("check")
    args = parser.parse_args()
    if args.cmd == "extract":
        extract(args.image, args.out)
    elif args.cmd == "dakota":
        dakota(args.run, args.out)
    elif args.cmd == "gap":
        write_report()
    else:
        return check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
