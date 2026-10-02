"""The Bluefin-vs-Utah image parity audit partitions gaps by source repository.

The audit's contract:

- Names Bluefin ships in its [fedora] / [fedora_v<N>] sections, minus every
  name Utah's overlay already lists under [gnome], [parity], [hardware],
  [services], [build], and [unavailable], form the gap.
- Each gap name is partitioned by where it resolves: Hummingbird's repo
  first (the smallest target -- a one-line move to the overlay), the
  factory repo second (a manifest gap, not a factory gap), and "nowhere"
  last (a factory recipe must land first).
- The same name in the same partition is silent on re-run; the same name
  in a different partition is celebrated as a migration; a new name in
  any partition is a regression the gate fails on.
"""

import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit-bluefin-parity.py"

spec = importlib.util.spec_from_file_location("audit_bluefin_parity", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def primary(names: dict[str, str], repo: str = "factory") -> bytes:
    """Render a minimal repodata primary.xml from a name -> evr map.

    One <package> per name, so the audit's "latest wins" tie-break has
    nothing to compete with. The XML namespace matches what yum-createrepo
    emits, because real fetches use that namespace and the parser must
    match.
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<metadata xmlns="http://linux.duke.edu/metadata/rpm" packages="' + str(len(names)) + '">',
    ]
    for name, evr in names.items():
        arch = evr.rsplit(".", 1)[-1] if "." in evr else "x86_64"
        body = evr[: -len("." + arch)] if "." in evr else evr
        if ":" in body:
            epoch, version_release = body.split(":", 1)
        else:
            epoch, version_release = "0", body
        version, release = version_release.rsplit("-", 1) if "-" in version_release else (version_release, "0")
        lines.append(
            f'<package type="rpm"><name>{name}</name><arch>{arch}</arch>'
            f'<version epoch="{epoch}">{version}</version>'
            f'<release>{release}</release></package>'
        )
    lines.append("</metadata>")
    return "\n".join(lines).encode()


def repomd_for(primary_href: str = "repodata/primary.xml") -> bytes:
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <repomd xmlns="http://linux.duke.edu/metadata/repo">
          <data type="primary">
            <location href="{primary_href}"/>
            <checksum type="sha256">unused</checksum>
          </data>
        </repomd>
        """).encode()


class BluefinManifestTests(unittest.TestCase):
    def test_bluefin_packages_unions_fedora_sections(self):
        text = textwrap.dedent("""\
            [multimedia_overrides]
            packages = ["libva"]

            [fedora]
            packages = ["coreutils", "wpa_supplicant"]

            [fedora_v44]
            packages = ["evolution"]

            [fedora_v43]
            packages = ["firefox"]

            [excluded]
            packages = ["deprecated-thing"]
            """)
        names = audit.bluefin_packages(text)
        self.assertEqual(names, {"coreutils", "wpa_supplicant", "evolution", "firefox"})

    def test_bluefin_packages_ignores_multimedia_overrides_and_excluded(self):
        # multimedia_overrides is a swap of a name Fedora already ships;
        # excluded is a removal. Both must not appear in the gap candidate
        # set, because adding either to a partition would be a misread of
        # the contract.
        text = textwrap.dedent("""\
            [multimedia_overrides]
            packages = ["mesa-libGL", "libva"]
            [excluded]
            packages = ["abrt"]
            [fedora]
            packages = ["coreutils"]
            """)
        self.assertEqual(audit.bluefin_packages(text), {"coreutils"})

    def test_bluefin_packages_picks_up_new_fedora_v_section_without_enumeration(self):
        # A future [fedora_v45] (or any per-Fedora-version section Bluefin
        # adds) must be in the union automatically; enumerating versions
        # in bluefin_packages() makes the audit miss the gap the day
        # Fedora ships a new release.
        text = textwrap.dedent("""\
            [fedora]
            packages = ["coreutils"]
            [fedora_v45]
            packages = ["future-thing"]
            [fedora_v44]
            packages = ["evolution"]
            """)
        self.assertEqual(
            audit.bluefin_packages(text),
            {"coreutils", "future-thing", "evolution"},
        )


class HummingbirdRepomdTests(unittest.TestCase):
    """The Hummingbird repomd parser must read <checksum>, not <open-checksum>.

    A gzipped primary.xml.gz ships both a <checksum> for the compressed
    bytes (the ones the script just downloaded) and an <open-checksum>
    for the uncompressed form. A bare endswith("checksum") match picks up
    the open-checksum (iteration order) and fails every fetch against the
    live repository; matching the local XML name is what keeps the gate
    green.
    """

    def _fetch(self, repomd_xml: bytes, primary_bytes: bytes):
        """Drive `fetch_hummingbird_repodata` against mocked network I/O."""
        from unittest.mock import patch

        def fake_urlopen(url, *args, **kwargs):
            if url.endswith("repomd.xml"):
                return io.BytesIO(repomd_xml)
            if url.endswith("primary.xml.gz"):
                return io.BytesIO(primary_bytes)
            raise AssertionError(f"unexpected fetch: {url}")

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(audit.urllib.request, "urlopen",
                             side_effect=fake_urlopen), \
                patch.object(audit, "HUMMINGBIRD_REPO_FILES",
                             [Path(tmp) / "hummingbird.repo"]):
            (Path(tmp) / "hummingbird.repo").write_text(
                "[public-hummingbird-x86_64-rpms]\n"
                "baseurl = https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/\n"
            )
            destination = Path(tmp) / "out"
            destination.mkdir()
            return audit.fetch_hummingbird_repodata(destination)

    def test_picks_compressed_checksum_not_open_checksum(self) -> None:
        """The parser takes <checksum>, not <open-checksum>.

        Both elements are present in the repomd. We construct primary_bytes
        so that sha256(primary_bytes) matches the <checksum> value we put
        in the repomd; with the local-name fix the function returns cleanly,
        with the broken endswith("checksum") match it would have read the
        <open-checksum> value, computed sha256(primary_bytes), and raised
        'Hummingbird primary.xml failed checksum'.
        """
        primary_bytes = b"compressed-primary-bytes-for-checksum-test"
        target_digest = hashlib.sha256(primary_bytes).hexdigest()
        repomd = textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <repomd xmlns="http://linux.duke.edu/metadata/repo">
              <data type="primary">
                <location href="repodata/primary.xml.gz"/>
                <checksum type="sha256">{target_digest}</checksum>
                <open-checksum type="sha256">feedface00000000000000000000000000000000000000000000000000000000</open-checksum>
              </data>
            </repomd>
            """).encode()
        baseurl, basename = self._fetch(repomd, primary_bytes)
        self.assertTrue(basename.endswith("primary.xml.gz"))
        self.assertIn("packages.redhat.com", baseurl)

    def test_rejects_open_checksum_when_only_open_checksum_matches(self) -> None:
        """The function fails loud if <checksum> disagrees with the live sha256.

        If the parser were still picking <open-checksum>, the broken branch
        would have read the live sha256 of `primary_bytes`, found it matched,
        and silently returned. With the local-name fix the parser takes the
        zero-digest <checksum>, finds it does NOT match the live sha256,
        and raises ValueError.
        """
        primary_bytes = b"uncompressed-bytes-for-open-checksum"
        live_sha = hashlib.sha256(primary_bytes).hexdigest()
        repomd = textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <repomd xmlns="http://linux.duke.edu/metadata/repo">
              <data type="primary">
                <location href="repodata/primary.xml.gz"/>
                <checksum type="sha256">{"0" * 64}</checksum>
                <open-checksum type="sha256">{live_sha}</open-checksum>
              </data>
            </repomd>
            """).encode()
        with self.assertRaises(ValueError) as ctx:
            self._fetch(repomd, primary_bytes)
        self.assertIn("checksum", str(ctx.exception).lower())


class GapTests(unittest.TestCase):
    def test_gap_names_exclude_every_overlay_section_and_unavailable(self):
        bluefin = {"coreutils", "wpa_supplicant", "evolution", "firefox", "roaming-things"}
        utah = {
            "installed": {"wpa_supplicant", "coreutils", "evolution"},
            "unavailable": {"firefox"},
        }
        self.assertEqual(audit.gap_names(bluefin, utah), ["roaming-things"])

    def test_gap_names_are_sorted(self):
        # Sorted output is what makes the JSON baseline a meaningful object
        # across two consecutive runs -- list-insertion order would drift.
        bluefin = {"zeta", "alpha", "mu"}
        utah = {"installed": set(), "unavailable": set()}
        self.assertEqual(audit.gap_names(bluefin, utah), ["alpha", "mu", "zeta"])


class PartitionTests(unittest.TestCase):
    def test_partition_prefers_hummingbird_over_factory(self):
        # A name that both repositories offer must land in
        # hummingbird-available: the operator's smallest move is to add
        # the name to the overlay, and that move only works against the
        # smaller Hummingbird rebuild. Factory-built is the second-best
        # answer, so a name we can already get from Hummingbird does not
        # belong there.
        gap = ["shelled", "router", "obscure"]
        hummingbird = {"shelled": "1.0-1.fc44.x86_64"}
        factory = {"shelled": "2.0-1.hum1.x86_64", "router": "0.1-1.bfin.x86_64"}
        parts = audit.partition(gap, hummingbird, factory)
        self.assertEqual(parts["hummingbird-available"], ["shelled"])
        self.assertEqual(parts["factory-built"], ["router"])
        self.assertEqual(parts["nowhere"], ["obscure"])

    def test_partition_nowhere_when_neither_repository_has_it(self):
        # evolution-ews-core IS in the factory repo: the audit sees it as
        # factory-built, not nowhere. The "nowhere" partition is for
        # names neither repository offers; this asserts the routing.
        gap = ["libgda", "libgda-sqlite", "evolution-ews-core"]
        hummingbird = {}
        factory = {"evolution-ews-core": "3.61.3-1.bfin.x86_64"}
        parts = audit.partition(gap, hummingbird, factory)
        self.assertEqual(parts["factory-built"], ["evolution-ews-core"])
        self.assertEqual(parts["nowhere"], ["libgda", "libgda-sqlite"])
        self.assertEqual(parts["hummingbird-available"], [])


class BaselineTests(unittest.TestCase):
    def test_compare_silent_on_partition_migration(self):
        # A name moving from factory-built to hummingbird-available is a
        # Hummingbird rebuild landing; it must NOT be reported as drift.
        # A brand-new name that did not exist anywhere in the baseline is
        # the regression the gate must fail on; here, libgda.
        baseline = {
            "hummingbird-available": ["shell"],
            "factory-built": ["router", "modem"],
            "nowhere": ["obscure"],
        }
        new = {
            "hummingbird-available": ["shell", "router"],
            "factory-built": ["modem"],
            "nowhere": ["obscure", "libgda"],
        }
        msgs = audit.compare_to_baseline(new, baseline, "deadbeef", "registry/name@sha256:abc")
        self.assertEqual(msgs, ["nowhere: +1 ['libgda']"])

    def test_compare_silent_when_no_growth(self):
        baseline = {
            "hummingbird-available": ["shell"],
            "factory-built": ["router"],
            "nowhere": ["obscure"],
        }
        new = {
            "hummingbird-available": ["shell"],
            "factory-built": ["router"],
            "nowhere": ["obscure"],
        }
        self.assertEqual(
            audit.compare_to_baseline(new, baseline, "deadbeef", "registry/name@sha256:abc"), []
        )

    def test_compare_reports_shrink_as_noop(self):
        # A name dropping from a partition because the operator closed
        # the gap (moved to [parity] / [hardware] / etc.) is silent.
        baseline = {
            "hummingbird-available": ["shell", "router"],
            "factory-built": ["modem"],
            "nowhere": ["obscure"],
        }
        new = {
            "hummingbird-available": ["shell"],
            "factory-built": ["modem"],
            "nowhere": ["obscure"],
        }
        self.assertEqual(
            audit.compare_to_baseline(new, baseline, "deadbeef", "registry/name@sha256:abc"), []
        )

    def test_compare_reports_regression_in_every_partition(self):
        # A brand-new name in any partition is a regression; the gate
        # fails once per affected partition. This pins that the regression
        # filter operates per-partition rather than as one union.
        baseline = {
            "hummingbird-available": ["shell"],
            "factory-built": ["modem"],
            "nowhere": ["obscure"],
        }
        new = {
            "hummingbird-available": ["shell", "shell-new"],
            "factory-built": ["modem", "modem-new"],
            "nowhere": ["obscure", "nowhere-new"],
        }
        msgs = audit.compare_to_baseline(new, baseline, "deadbeef", "registry/name@sha256:abc")
        self.assertEqual(len(msgs), 3)
        self.assertIn("hummingbird-available: +1 ['shell-new']", msgs)
        self.assertIn("factory-built: +1 ['modem-new']", msgs)
        self.assertIn("nowhere: +1 ['nowhere-new']", msgs)

    def test_baseline_record_round_trips_through_json(self):
        parts = {"hummingbird-available": ["shell"], "factory-built": ["router"], "nowhere": ["obscure"]}
        record = audit.baseline_record(parts, "deadbeef", "registry/name@sha256:abc", "https://hummingbird/")
        # Sorted, so a name reordering between runs does not produce a
        # diff in the file.
        self.assertEqual(record["hummingbird-available"], ["shell"])
        # JSON round-trips: the recorded baseline is the file on disk.
        self.assertEqual(json.loads(json.dumps(record))["ref"], "deadbeef")


class PrimaryParsingTests(unittest.TestCase):
    def test_parse_primary_uses_latest_evr_for_repeated_names(self):
        # yum primary.xml rarely repeats a name, but a malformed repo can;
        # the audit must take the latest, not the last entry seen.
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <metadata xmlns="http://linux.duke.edu/metadata/rpm" packages="2">
              <package type="rpm"><name>shell</name><arch>x86_64</arch>
                <version epoch="0">1.0</version><release>1.fc44</release></package>
              <package type="rpm"><name>shell</name><arch>x86_64</arch>
                <version epoch="0">1.1</version><release>1.fc44</release></package>
            </metadata>
            """).encode()
        with tempfile.NamedTemporaryFile(suffix=".xml") as tmp:
            Path(tmp.name).write_bytes(xml)
            parsed = audit.parse_primary(Path(tmp.name))
        self.assertEqual(parsed["shell"], "1.1-1.fc44.x86_64")

    def test_parse_primary_decompresses_gzipped_primary(self):
        # Hummingbird publishes primary.xml.gz (repomd.xml's href ends in
        # .gz). parse_primary() keys on the basename to decide whether to
        # decompress, so a script that runs the real Hummingbird layout
        # must succeed; a script that only handles plain XML would crash
        # with xml.etree.ElementTree.ParseError at the first byte.
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <metadata xmlns="http://linux.duke.edu/metadata/rpm" packages="1">
              <package type="rpm"><name>shell</name><arch>x86_64</arch>
                <version epoch="0">1.1</version><release>1.fc44</release></package>
            </metadata>
            """).encode()
        with tempfile.NamedTemporaryFile(suffix=".xml.gz") as tmp:
            Path(tmp.name).write_bytes(gzip.compress(xml))
            parsed = audit.parse_primary(Path(tmp.name))
        self.assertEqual(parsed["shell"], "1.1-1.fc44.x86_64")

    def test_dnf_list_line_formats_name_arch_and_evr(self):
        line = audit.dnf_list_line("shell", "1.1-1.fc44.x86_64")
        self.assertEqual(line, "shell.x86_64\t1.1-1.fc44")


class MetadataUnpackTests(unittest.TestCase):
    def test_unpack_metadata_rejects_absolute_paths(self):
        # An attacker who can republish the factory image would otherwise
        # be able to overwrite the audit's working directory on disk; the
        # unpacker must refuse anything outside repository/repodata/.
        import tarfile

        def archive_with(name: str) -> bytes:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as archive:
                entry = tarfile.TarInfo(name)
                entry.size = len(b"<repomd/>")
                archive.addfile(entry, io.BytesIO(b"<repomd/>"))
            return buf.getvalue()

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                audit.unpack_metadata(archive_with("/absolute"), Path(tmp))
            with self.assertRaises(ValueError):
                audit.unpack_metadata(archive_with("../escape"), Path(tmp))

    def test_unpack_metadata_rejects_non_repodata_entries(self):
        # The factory publishes repodata first and only; a payload layer
        # arriving instead is a publish bug, and the unpacker must not
        # silently extract an RPM.
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            entry = tarfile.TarInfo("repository/payload.rpm")
            entry.size = len(b"not-an-rpm")
            archive.addfile(entry, io.BytesIO(b"not-an-rpm"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                audit.unpack_metadata(buf.getvalue(), Path(tmp))


class RefValidationTests(unittest.TestCase):
    def test_bluefin_ref_accepts_full_sha_and_branch_names(self):
        # The default ref comes from packages/.bluefin-parity-ref (a
        # 40-character SHA) but the audit also takes a branch or tag for
        # a one-off review of a candidate Bluefin release. The validator
        # lives in the script (audit.validate_bluefin_ref); the test
        # calls it so a regex drift surfaces here, not just in production.
        for ref in ("a" * 40, "main", "release/2026-09", "v1.2.3"):
            with self.subTest(ref=ref):
                # No exception: the ref is valid.
                audit.validate_bluefin_ref(ref)

    def test_bluefin_ref_rejects_path_traversal(self):
        # A ref is a git revision identifier; paths and weird characters
        # cannot be one, so the validator must refuse them rather than
        # silently construct an unsafe URL. The `..` check is what catches
        # ../etc/passwd; shell metacharacters and newlines fall outside
        # the [A-Za-z0-9._/-] alphabet.
        for bad in ("../etc/passwd", "main; rm -rf /", "head\ninjected"):
            with self.subTest(ref=bad):
                with self.assertRaises(ValueError):
                    audit.validate_bluefin_ref(bad)


class SubcommandTests(unittest.TestCase):
    def _bluefin_toml(self, tmp: Path) -> Path:
        text = textwrap.dedent("""\
            [fedora]
            packages = ["wpa_supplicant", "shell", "obscure"]
            [fedora_v44]
            packages = ["bluefin-only-44"]
            """)
        path = tmp / "bluefin.toml"
        path.write_text(text)
        return path

    def _utah_toml(self, tmp: Path) -> Path:
        text = textwrap.dedent("""\
            [parity]
            packages = ["shell"]
            [unavailable]
            packages = ["wpa_supplicant"]
            """)
        path = tmp / "utah.toml"
        path.write_text(text)
        return path

    def _containerfile(self, tmp: Path) -> Path:
        text = ("ARG BASE_IMAGE=example/base:latest\n"
                "ARG PACKAGE_IMAGE=example/packages\n"
                "ARG PACKAGE_IMAGE_SHA=sha256:" + "a" * 64 + "\n")
        path = tmp / "Containerfile"
        path.write_text(text)
        return path

    def test_check_returns_2_when_baseline_missing(self):
        # The check repo wants the baseline to be readable before it asks
        # the network to do anything: a missing file is a configuration
        # error, not a verdict, and must surface with exit code 2 -- the
        # contract other repo gates reserve for "this gate has never run".
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            utah = self._utah_toml(root)
            containerfile = self._containerfile(root)
            baseline = root / "missing.json"
            saved = (audit.UTAH_TOML, audit.CONTAINERFILE, audit.BASELINE, audit.PARITY_REF)
            audit.UTAH_TOML = utah
            audit.CONTAINERFILE = containerfile
            audit.BASELINE = baseline
            audit.PARITY_REF = root / ".bluefin-parity-ref"
            audit.PARITY_REF.write_text("d" * 40)
            with patch.object(audit, "fetch_partition") as fetch, \
                    contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                fetch.side_effect = AssertionError("network must not be touched before baseline is loaded")
                from argparse import Namespace
                code = audit.cmd_check(Namespace(ref=None))
            self.assertEqual(code, 2)
            audit.UTAH_TOML, audit.CONTAINERFILE, audit.BASELINE, audit.PARITY_REF = saved


class RecipeArgForwardingTests(unittest.TestCase):
    """The audit recipes must interpolate args; shebang recipes see no `$@`.

    `just` runs a shebang recipe by handing its body to the interpreter;
    the recipe's arguments are not positional parameters to that script
    (`$# = 0`). A body that reads `"$@"` is a no-op, so
    `just audit-bluefin-parity --check` silently ran the report-only
    `run` subcommand. The flags must be interpolated into the body with
    `{{args}}` instead. `just --dry-run` renders the interpolated body,
    which is what these tests assert.
    """

    @classmethod
    def setUpClass(cls):
        cls.just = shutil.which("just")
        if not cls.just:
            raise unittest.SkipTest("just is not installed")

    def _rendered_body(self, *argv: str) -> str:
        result = subprocess.run(
            [self.just, "--justfile", str(ROOT / "Justfile"),
             "--working-directory", str(ROOT), "--dry-run", *argv],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return result.stdout

    def test_audit_recipe_carries_check_flag_into_body(self):
        body = self._rendered_body("audit-bluefin-parity", "--check")
        self.assertIn("for arg in --check", body)
        self.assertNotIn('"$@"', body)

    def test_audit_recipe_carries_write_and_ref_into_body(self):
        body = self._rendered_body("audit-bluefin-parity", "--write", "--ref=HEAD")
        self.assertIn("for arg in --write --ref=HEAD", body)

    def test_check_recipe_carries_ref_into_body(self):
        body = self._rendered_body("check-audit-parity", "--ref=HEAD")
        self.assertIn("for arg in --ref=HEAD", body)
        self.assertNotIn('"$@"', body)


class StaleBaselineTests(unittest.TestCase):
    # The gate this issue fixes: compare_to_baseline must fail on a baseline
    # captured against a different Bluefin ref or factory pin, not pass
    # silently against an unchanged package set.

    BASELINE = {
        "ref": "old-bluefin",
        "factory_ref": "old-factory",
        "hummingbird-available": ["a", "b"],
        "factory-built": ["c"],
        "nowhere": [],
    }

    # The same package set a later audit reads.
    SAME = {
        "hummingbird-available": ["a", "b"],
        "factory-built": ["c"],
        "nowhere": [],
    }

    def test_matching_refs_pass_with_no_messages(self):
        self.assertEqual(
            audit.compare_to_baseline(self.SAME, self.BASELINE, "old-bluefin", "old-factory"),
            [],
        )

    def test_a_bluefin_ref_bump_is_reported_not_a_clean_run(self):
        # The bug: same packages, only the Bluefin ref changed. Without the
        # ref comparison this returns [] and the stale baseline passes.
        msgs = audit.compare_to_baseline(
            self.SAME, self.BASELINE, "new-bluefin", "old-factory"
        )
        self.assertEqual(len(msgs), 1)
        self.assertIn("stale baseline: ref", msgs[0])
        self.assertIn("new-bluefin", msgs[0])

    def test_a_factory_pin_bump_is_reported(self):
        msgs = audit.compare_to_baseline(
            self.SAME, self.BASELINE, "old-bluefin", "new-factory"
        )
        self.assertEqual(len(msgs), 1)
        self.assertIn("factory_ref", msgs[0])

    def test_both_ref_and_factory_bumps_are_both_reported(self):
        msgs = audit.compare_to_baseline(
            self.SAME, self.BASELINE, "new-bluefin", "new-factory"
        )
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all("stale baseline" in m for m in msgs))

    def test_an_old_baseline_without_meta_is_not_flagged_stale(self):
        # A baseline written before the ref/factory_ref meta existed has no
        # keys to compare; the gate must not invent a mismatch against them.
        legacy = {k: v for k, v in self.BASELINE.items() if k not in ("ref", "factory_ref")}
        self.assertEqual(
            audit.compare_to_baseline(self.SAME, legacy, "any-ref", "any-factory"), []
        )

    def test_a_real_regression_still_fails_when_refs_match(self):
        grew = dict(self.SAME)
        grew["hummingbird-available"] = ["a", "b", "brand-new"]
        msgs = audit.compare_to_baseline(
            grew, self.BASELINE, "old-bluefin", "old-factory"
        )
        self.assertEqual(len(msgs), 1)
        self.assertIn("brand-new", msgs[0])

    def test_a_stale_baseline_is_reported_before_any_partition_growth(self):
        # The stale-baseline verdict must never be masked by, or buried under,
        # a growth report: it comes first in the message list.
        grew = dict(self.SAME)
        grew["nowhere"] = ["brand-new"]
        msgs = audit.compare_to_baseline(
            grew, self.BASELINE, "new-bluefin", "old-factory"
        )
        self.assertIn("stale baseline: ref", msgs[0])
        self.assertEqual(len(msgs), 2)

    def _run_check(self, parts: dict, ref: str, factory_ref: str) -> tuple[int, str]:
        """Run cmd_check against BASELINE with the network stubbed out."""
        from argparse import Namespace
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            baseline_file = Path(tmp) / "audit-baseline.json"
            baseline_file.write_text(json.dumps(self.BASELINE))
            saved = audit.BASELINE
            audit.BASELINE = baseline_file
            err = io.StringIO()
            try:
                with patch.object(audit, "fetch_partition") as fetch, \
                        contextlib.redirect_stderr(err), \
                        contextlib.redirect_stdout(io.StringIO()):
                    fetch.return_value = (ref, parts, None, None, factory_ref, None)
                    code = audit.cmd_check(Namespace(ref=None))
            finally:
                audit.BASELINE = saved
        return code, err.getvalue()

    def test_check_trailer_on_a_stale_only_verdict_does_not_claim_growth(self):
        # The operator text must match the verdict: nothing grew, the
        # baseline was captured against a different ref, and the only
        # remedy is to re-record it.
        code, err = self._run_check(self.SAME, "new-bluefin", "old-factory")
        self.assertEqual(code, 1)
        self.assertNotIn("partitions grew", err)
        self.assertIn("captured against different refs", err)
        self.assertIn("--write", err)

    def test_check_trailer_on_growth_alone_still_says_partitions_grew(self):
        grew = dict(self.SAME)
        grew["nowhere"] = ["brand-new"]
        code, err = self._run_check(grew, "old-bluefin", "old-factory")
        self.assertEqual(code, 1)
        self.assertIn("partitions grew past", err)
        self.assertNotIn("captured against different refs", err)

    def test_check_trailer_reports_both_when_stale_and_grown(self):
        grew = dict(self.SAME)
        grew["nowhere"] = ["brand-new"]
        code, err = self._run_check(grew, "new-bluefin", "new-factory")
        self.assertEqual(code, 1)
        self.assertIn("is stale and partitions grew past it", err)

    def test_check_is_silent_and_passes_when_refs_match_and_nothing_grew(self):
        code, err = self._run_check(self.SAME, "old-bluefin", "old-factory")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
