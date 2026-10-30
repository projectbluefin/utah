# Contract CI gate

`build.yml` opens with a container-based gate so a contract package that none of
Utah's repositories provide fails in seconds instead of surfacing as an
opaque `exit status 71` from the image build (comment,
`.github/workflows/build.yml`). It runs three checks:

- `just check` -- manifest validation, including the workflow output check
  (`scripts/check_workflow_outputs.py`), the download integrity guard
  (`scripts/check-download-integrity.py`), the syntax gate
  (`scripts/check-script-syntax.py`), host-side unit tests (`just test`),
  and the ban on flavor literals in workflows.
- `just check-parity` -- `packages/bluefin.toml` against Bluefin's upstream
  pinned at `packages/.bluefin-parity-ref`; the nightly parity workflow opens
  a dedicated review when Bluefin `main` changes it.
- `just check-repos` -- the complete installation transaction against the
  digest-pinned base and package repository, including extension build tools.

Host-side unit tests in this gate must be wall-clock independent: a loaded
runner can take over a second between setup and assertion, so any test that
renders relative times freezes the clock in the harness (the `ago()` site
test pins `Date.now`) rather than trusting setup-to-assert to stay within
one unit.

### Python install pins

`tests/test_python_install_pins.py` runs explicitly in `just check` and also
through `just test`. It inspects workflow `run` steps (`.yml` and `.yaml`) and
Justfile recipes for `pip`, `pip3`, and `python -m pip` installs, including
backslash continuations and shell command chains. Every direct package must
have an exact `==` version; wildcard pins and version ranges fail. Requirements
files passed with `-r` or `--requirement` are checked recursively, and missing
or cyclic files fail. Keep paths relative to the repository root.

Declare host test dependencies in `tests/requirements.txt`, where Renovate
can update the pins. Do not silently install missing modules in `just check`;
`just test` reports how to install the declared dependencies. This gate covers
the pip command forms above, not arbitrary shell wrappers or other Python
installers; extend its fixtures when introducing a new install form.

### CI guard scripts and test coverage

The front-matter description budget measures the content after stripping the
leading YAML block indicator (`>`, `|`, and their chomping modifiers). Cover
both 256 and 257 characters for each indicator so a folded description cannot
fail because the checker counts YAML syntax. `.github/actionlint.yaml` admits
the GitHub-hosted `ubuntu-26.04` label while the pinned actionlint predates it;
all other labels remain checked. Remove that compatibility entry when a newer
actionlint release recognizes the label.



The fast gate relies on pure-verdict Python scripts under `scripts/` to halt
the build before expensive compilation or container builds run:

- `scripts/check-download-integrity.py`: enforces that composition recipes do
  not resolve mutable `releases/latest` URLs, and that any executable download
  (`.run`, `.tar.gz`, `.tgz`, `.rpm`, `.flatpak`, `.service`, `.timer`) via `curl`
  or `wget` is verified against a digest (`sha256sum`, `sha512sum`) or
  signature (`cosign`, `gpg --verify`). Clearance is scoped to the fetch: the
  verifier must sit in the fetch's own `&&` chain (a backslash-continuation
  run is one command) or within the ten lines that follow it, must be code
  rather than comment text, and a bare `--check` never clears on its own --
  `sha256sum --check` clears through `sha256sum`. Flathub descriptor
  downloads (`flathub.flatpakrepo`, `appstream`) and comment lines are exempt
  from the extension heuristic only; `scripts/configure-services.sh` still
  pins `flathub.flatpakrepo` by sha256, because the descriptor carries the
  `Url=` and `GPGKey=` every Flatpak on the image is verified against.
  Verify the trust behavior by running `configure-services.sh` in a disposable
  image: a matching descriptor must install unchanged, while a hash mismatch
  must exit nonzero without replacing an existing remote. Source ordering or
  string assertions do not prove that rejection path.
  `tests/test_flathub_descriptor.py` runs the whole script against scratch
  filesystem roots and a committed descriptor fixture, with real hashing and
  installation. It covers matching bytes and rejection without remote replacement.
  Scanning is per *logical* line: backslash continuations are joined before
  matching, so a `curl` whose URL sits on a continuation line is still inspected
  and is reported at the line the command starts on. Matching raw lines instead
  made every multi-line download invisible to the gate. Comment lines never
  start or end a run: a `#` line with a trailing backslash does not swallow the
  command below it, and a `#` line inside a `RUN` continuation is dropped the
  way the Dockerfile parser drops it, so the run keeps going.
  Exercised by black-box tests in `tests/test_check_download_integrity.py`.
- `scripts/check_workflow_outputs.py`: parses workflows under `.github/workflows/`
  and ensures that every job output referencing `steps.<id>.outputs` points to a
  step id defined in that same job. Step ids never leak across jobs, non-step
  expressions (`inputs.*`, `github.*`, `env.*`) are accepted, and all dangling
  references across all workflow files are reported.
  Exercised by black-box tests in `tests/test_check_workflow_outputs.py`.

The same job resolves the flavor set and splits it in two by what each
flavor builds on -- `main` on the pristine Hummingbird base, the rest on the
kernel cache image -- emitting `main_flavors`, `kernel_flavors`, and
`needs_kernel` (step "Resolve the image flavors",
`.github/workflows/build.yml`). The split rationale and the flavors.py
commands live in [flavors.md](../../flavors.md). An empty resolved set fails
loudly there, because an invalid matrix creates no image job at all and the
only symptom is `build_container: failure` from the aggregator (comment,
`.github/workflows/build.yml`).
