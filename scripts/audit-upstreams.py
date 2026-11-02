#!/usr/bin/env python3
"""Read-only upstream freshness report, derived from this worktree's tracked inputs.

Only git metadata, registry manifests, GitHub metadata/content and small vendor
metadata are read. No shell evaluation, checkout, fetch, image pull or disk cache.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SHA = r"[0-9a-f]{40}"
DIGEST = r"sha256:[0-9a-f]{64}"
IMAGE = re.compile(r"(?:ghcr\.io|quay\.io|docker\.io)/[A-Za-z0-9_./:-]+(?:@sha256:[0-9a-f]{64})?")
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z_0-9]*)(?::-([^{}]*))?\}|\$([A-Za-z_][A-Za-z_0-9]*)")
URL = re.compile(r"https?://[^\s\"'<>\\)]+")
ASSIGN = re.compile(r"^\s*(?:ARG |export )?([A-Za-z_][A-Za-z_0-9]*)=(.*)$")
ACCEPT_MANIFEST = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


class AuditError(Exception):
    pass


class NoRelease(AuditError):
    """The upstream publishes no stable release or comparable version tags."""


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        capture_output=True, timeout=20, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        # Do not echo stderr: a configured URL can contain credentials.
        raise AuditError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout.decode("utf-8", errors="replace")


def expand(value: str, variables: dict[str, str]) -> str:
    """Resolve literals/defaults only; never execute command substitutions."""
    for _ in range(12):
        updated = VARIABLE.sub(lambda m: variables.get(m[1] or m[3], m[2] if m[2] is not None else m[0]), value)
        if updated == value:
            break
        value = updated
    return value


def assignments(text: str) -> dict[str, str]:
    variables = {}
    for line in text.splitlines():
        match = ASSIGN.match(line)
        if match:
            value = match[2].strip().strip('"\'')
            # Overrides document a default, not the environment used for this audit.
            value = VARIABLE.sub(lambda m: m[2] if m[2] is not None else m[0], value)
            if not any(token in value for token in ("$(", "`", " && ")):
                variables[match[1]] = value
    return {key: expand(value, variables) for key, value in variables.items()}


def entry(kind, name, current, tracking, location, **extra):
    return dict(kind=kind, name=name, current=current, tracking=tracking,
                locations=[location], **extra)


def merge_inventory(items):
    merged = {}
    for item in items:
        key = json.dumps({k: v for k, v in item.items() if k != "locations"}, sort_keys=True)
        if key not in merged:
            merged[key] = item
        else:
            merged[key]["locations"] = sorted(set(merged[key]["locations"] + item["locations"]))
    return sorted(merged.values(), key=lambda item: (item["kind"], item["name"], item["current"]))


def discover(root: Path, tracked: dict[str, tuple[str, str]]) -> list[dict]:
    """Parse declarative inputs, with unsupported consumable URLs made visible."""
    items = []
    texts = {}
    for name, (mode, _) in tracked.items():
        if mode == "160000" or name.startswith(("tests/", "docs/")):
            continue
        if not (name in ("Justfile", ".gitmodules", "renovate.json") or
                "Containerfile" in Path(name).name or
                (name.startswith(("scripts/", "iso/", ".github/", "system_files/", "packages/"))
                 and Path(name).suffix in (".sh", ".py", ".yml", ".yaml", ".repo", ".just"))):
            continue
        if name == "scripts/audit-upstreams.py":
            continue
        path = root / name
        if not path.is_file():
            items.append(entry("limitation", name, "missing tracked input", "", name))
            continue
        texts[name] = path.read_text()

    for name, text in texts.items():
        if name == "renovate.json":
            continue  # Manager metadata identifies pins elsewhere; it is not consumed.
        variables = assignments(text)
        variable_sources = {}
        for number, line in enumerate(text.splitlines(), 1):
            assignment = ASSIGN.match(line)
            if assignment and assignment[1] in variables:
                variable_sources.setdefault(variables[assignment[1]], []).append(f"{name}:{number}")
        start = len(items)
        for number, line in enumerate(text.splitlines(), 1):
            location = f"{name}:{number}"
            assignment = ASSIGN.match(line)
            if assignment and assignment[1].endswith("_IMAGE_SHA"):
                pin = variables.get(assignment[1], assignment[2])
                if not re.fullmatch(DIGEST, pin):
                    items.append(entry("limitation", "malformed OCI pin " + assignment[1], pin, "", location))
                elif assignment[1].removesuffix("_SHA") not in variables:
                    items.append(entry("limitation", "unmapped OCI pin " + assignment[1], pin, "", location))
            if line.lstrip().startswith("#"):
                # The package pin stamp is an input even though it is a comment.
                stamp = re.fullmatch(r"# factory-pin: (sha256:[0-9a-f]{64})", line.strip())
                if stamp:
                    config = json.loads(texts.get("renovate.json", "{}"))
                    matched = False
                    for manager in config.get("customManagers", []):
                        if manager.get("datasourceTemplate") == "docker" and "factory-pin:" in " ".join(manager.get("matchStrings", [])):
                            items.append(entry("oci", manager["depNameTemplate"], stamp[1], manager.get("currentValueTemplate", "latest"), location))
                            matched = True
                    if not matched:
                        items.append(entry("limitation", "unmapped factory-pin", stamp[1], "", location))
                elif line.strip().startswith("# factory-pin:"):
                    items.append(entry("limitation", "malformed factory-pin", line.strip().partition(":")[2].strip(), "", location))
                continue
            expanded = expand(line, variables)
            for match in IMAGE.finditer(expanded):
                ref = match[0].rstrip(".:,;")
                remainder = expanded[match.end():]
                if remainder.startswith("@") or ("@" in ref and re.match(r"[A-Za-z0-9_:]", remainder)):
                    malformed = re.match(r"[^\s\"']+", remainder)
                    items.append(entry("limitation", "malformed OCI digest", ref + (malformed[0] if malformed else ""), "", location))
                    continue
                if "$" in expanded[match.end():match.end() + 1] or "$" in ref:
                    continue
                image, _, digest = ref.partition("@")
                repository, colon, tag = image.rpartition(":")
                if not colon:
                    repository, tag = image, "latest"
                # References to Utah's own outputs are not external inputs.
                if re.match(r"(?:ghcr\.io/projectbluefin/utah(?:[-:]|$)|localhost/)", image) and not image.startswith("ghcr.io/projectbluefin/utah-packages"):
                    continue
                if not digest:
                    # Split IMAGE / IMAGE_SHA declarations are joined from this file.
                    for key, value in variables.items():
                        if value == image and re.fullmatch(DIGEST, variables.get(key + "_SHA", "")):
                            digest = variables[key + "_SHA"]
                            break
                items.append(entry("oci", repository, digest or image, tag, location))

            use = re.search(r"\buses:\s*['\"]?([^\s'\"#]+)(?:['\"]?\s*#\s*(\S+))?", line)
            if use and not use[1].startswith(("./", "docker://")):
                reference = use[1]
                if "@" not in reference:
                    items.append(entry("limitation", reference, "unresolved action", "", location))
                else:
                    action, pin = reference.rsplit("@", 1)
                    repo = "/".join(action.split("/")[:2])
                    hint = use[2] or (pin if not re.fullmatch(SHA, pin) else "")
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?", action) or not pin:
                        items.append(entry("limitation", "malformed action reference", reference, hint, location))
                    else:
                        items.append(entry("github-ref", action, pin, hint, location, repo=repo, check_releases=True))

            for raw in URL.findall(expanded):
                url = raw.rstrip(";,}")
                release = re.match(r"https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/", url)
                archive = re.match(r"https://github\.com/([^/]+/[^/]+)/archive/refs/tags/([^/]+?)(?:\.tar\.gz|\.zip)$", url)
                if release or archive:
                    match = release or archive
                    if "$" not in match[1] + match[2]:
                        items.append(entry("github-release", match[1], match[2], "latest stable release", location, repo=match[1]))
                        continue
                clone = re.search(r"\bgit clone\b", line)
                github = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?$", url)
                if clone and github:
                    branch = re.search(r"--branch\s+[\"']?([^\s\"']+)", expanded)
                    if branch and "$" not in branch[1]:
                        pin = variables.get("OGC_COMMIT", branch[1])
                        items.append(entry("github-release", github[1], branch[1], "latest stable release", location, repo=github[1], commit=pin))
                    else:
                        items.append(entry("limitation", github[1] + " optional source build", "caller-supplied checkout pin", "", location))
                    continue
                if "download.nvidia.com/XFree86/" in url and ".run" in url:
                    version = variables.get("driver_version", "")
                    endpoint = url.split("/Linux-x86_64/", 1)[0] + "/Linux-x86_64/latest.txt"
                    items.append(entry("nvidia", "NVIDIA driver", version, endpoint, location))
                    continue
                # Only consumable URLs, not documentation / certificate identities.
                if re.search(r"\b(?:curl|wget|remote-add)\b|^\s*(?:baseurl|metalink|mirrorlist)\s*=|_URL=", line):
                    # Raw files at release versions share the release lookup above.
                    if "raw.githubusercontent.com/" in url and any("releases/download/" in other and url.split("/")[3:5] == expand(other, variables).split("/")[3:5] for other in text.splitlines()):
                        continue
                    if name == "Justfile" and "raw.githubusercontent.com/" in url and "${ref}" in url:
                        continue
                    items.append(entry("limitation", url, "HTTP/RPM/Flatpak endpoint (not individually version-queried)", "", location))
        for item in items[start:]:
            values = [item["current"], item.get("commit", "")]
            if item["kind"] == "github-release":
                values.append(item["current"].removeprefix("v"))
            for value in values:
                item["locations"].extend(variable_sources.get(value, []))
            item["locations"] = sorted(set(item["locations"]))

    modules = configparser.ConfigParser(interpolation=None)
    modules.read_string(texts.get(".gitmodules", ""))
    for section in modules.sections():
        path = modules.get(section, "path")
        url = modules.get(section, "url")
        repo = re.fullmatch(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?", url)
        branch = modules.get(section, "branch", fallback="")
        pin = tracked.get(path, ("", ""))[1]
        if repo and pin and branch and branch != ".":
            items.append(entry("github-ref", path, pin, branch, ".gitmodules:" + section, repo=repo[1]))
        else:
            items.append(entry("limitation", path, pin or "missing gitlink", branch, ".gitmodules:" + section))

    # Derive Bluefin's repository/path from the existing parity gate, not another pin table.
    just = texts.get("Justfile", "")
    parity = re.search(r"https://raw\.githubusercontent\.com/([^/]+/[^/]+)/\$\{ref\}/([^\s\"]+)", just)
    if parity:
        ref_path = re.search(r"ref=\$\(tr[^\n]+<\s*([^\s)]+)", just)
        if ref_path and (root / ref_path[1]).is_file():
            pin = (root / ref_path[1]).read_text().strip()
            local = re.search(r'diff -u "\$upstream"\s+([^\s;]+)', just)
            if local:
                items.append(entry("parity", parity[1] + "/" + parity[2], pin, "upstream default branch", "Justfile:check-parity", repo=parity[1], path=parity[2], local=local[1]))
    items.append(entry("limitation", "factory package sources", "not individually queried", "", "packages/utah-packages.repo", detail="An OCI digest establishes repository identity only; it does not establish freshness of each factory recipe, distro RPM, Flatpak or transitive dependency. Audit utah-packages source manifests separately."))
    return merge_inventory(items)


class NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class Client:
    def __init__(self, timeout=15, token=None):
        self.timeout = timeout
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.cache = {}
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(NoCredentialRedirect())

    def fetch(self, url, headers=None):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise AuditError("only HTTPS metadata endpoints are allowed")
        headers = {"User-Agent": "utah-upstream-audit", **(headers or {})}
        if urllib.parse.urlsplit(url).netloc == "api.github.com" and self.token:
            headers["Authorization"] = "Bearer " + self.token
        # Single-flight cache stores failures too; no repeated requests for duplicates.
        key = (url, tuple(sorted(headers.items())))
        with self.lock:
            future = self.cache.get(key)
            owner = future is None
            if owner:
                future = self.cache[key] = Future()
        if not owner:
            return future.result()
        try:
            request = urllib.request.Request(url, headers=headers)
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise AuditError("metadata response exceeds 8 MiB")
                result = (data, dict(response.headers))
            future.set_result(result)
            return result
        except Exception as exc:
            future.set_exception(exc)
            # Consume the exception for the owner as well as waiting callers.
            return future.result()

    def api(self, path):
        return json.loads(self.fetch("https://api.github.com/" + path, {"Accept": "application/vnd.github+json"})[0])

    def ref(self, repo, ref):
        if not ref or "$" in ref:
            raise AuditError("no resolvable declared tracking hint")
        if re.fullmatch(SHA, ref):
            return self.api(f"repos/{repo}/commits/{ref}")["sha"]
        for namespace in ("heads", "tags"):
            try:
                obj = self.api(f"repos/{repo}/git/ref/{namespace}/{urllib.parse.quote(ref, safe='')}")["object"]
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
            for _ in range(8):
                if obj["type"] == "commit":
                    return obj["sha"]
                if obj["type"] != "tag":
                    raise AuditError("tracking ref does not identify a commit")
                obj = self.api(f"repos/{repo}/git/tags/{obj['sha']}")["object"]
            raise AuditError("annotated tag nesting exceeds 8")
        raise AuditError("declared tracking branch/tag does not exist")

    def latest(self, repo):
        try:
            release = self.api(f"repos/{repo}/releases/latest")
            return release["tag_name"]
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        # Some projects (including kernels) publish tags but no GitHub releases.
        # Never assume API tag ordering is semantic version ordering.
        tags = self.api(f"repos/{repo}/tags?per_page=100")
        if len(tags) == 100:
            raise AuditError("no releases and tag list exceeds bounded 100-tag coverage")
        stable = [tag["name"] for tag in tags if version_key(tag["name"]) is not None]
        if not stable:
            raise NoRelease("no stable release or comparable version tags")
        return max(stable, key=version_key)

    def manifest(self, repository, reference):
        registry, path = repository.split("/", 1)
        host = "registry-1.docker.io" if registry == "docker.io" else registry
        url = f"https://{host}/v2/{path}/manifests/{urllib.parse.quote(reference, safe=':')}"
        headers = {"Accept": ACCEPT_MANIFEST}
        try:
            data, response = self.fetch(url, headers)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            challenge = exc.headers.get("WWW-Authenticate", "")
            if not challenge.lower().startswith("bearer "):
                raise AuditError("registry does not offer anonymous bearer auth") from None
            parts = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            realm = parts.get("realm", "")
            realm_host = urllib.parse.urlsplit(realm).hostname or ""
            # Do not send credentials to a challenge-controlled endpoint.
            if urllib.parse.urlsplit(realm).scheme != "https" or not (realm_host == host or realm_host.endswith("." + registry) or (registry == "docker.io" and realm_host == "auth.docker.io")):
                raise AuditError("untrusted registry token realm")
            query = urllib.parse.urlencode({"service": parts.get("service", host), "scope": f"repository:{path}:pull"})
            auth = json.loads(self.fetch(realm + ("&" if "?" in realm else "?") + query)[0])
            token = auth.get("token") or auth.get("access_token")
            if not token:
                raise AuditError("registry token response has no token")
            data, response = self.fetch(url, {**headers, "Authorization": "Bearer " + token})
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        declared = next((v for k, v in response.items() if k.lower() == "docker-content-digest"), digest)
        if declared != digest:
            raise AuditError("registry manifest digest/header mismatch")
        return digest, json.loads(data)


def version_key(tag):
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:-ogc(\d+))?", tag)
    if not match:
        return None
    return tuple(int(part) for part in match[1].split(".")) + (int(match[2] or 0),)


def failure_message(exc):
    # Never include URL, response bodies, auth headers or subprocess stderr.
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}; check endpoint availability/authentication/rate limits"
    if isinstance(exc, AuditError):
        return str(exc)
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "metadata lookup timed out"
    if isinstance(exc, urllib.error.URLError):
        return "network/TLS lookup failed"
    return f"invalid or unavailable metadata ({type(exc).__name__})"


def resolve(item, client, root):
    result = {**item, "candidate": None, "status": "unknown", "detail": item.get("detail", "")}
    kind = item["kind"]
    result["next_step"] = {
        "oci": "Review Renovate digest update (package ARG and factory-pin stamp together); run just check-repos, rebuild and real runtime/ISO acceptance.",
        "github-ref": "Update the exact commit pin at every source location within the declared tracking policy; run just check and affected CI/runtime acceptance.",
        "github-release": "Update version/commit and all asset checksums together; run just check, rebuild and affected runtime/ISO acceptance.",
        "parity": "Sync Bluefin content verbatim and its parity ref together; run just check-parity, just check-repos and desktop runtime acceptance.",
        "nvidia": "Update driver version and vendor asset checksum together; rebuild kernel cache and run real NVIDIA runtime acceptance.",
        "limitation": "Query the owning vendor/package-source inventory separately; do not infer package freshness from an image digest.",
    }[kind]
    try:
        if kind == "limitation":
            result["detail"] = result["detail"] or "No bounded version comparison is declared for this endpoint; coverage is incomplete."
        elif kind == "oci":
            digest, manifest = client.manifest(item["name"], item["tracking"])
            result["candidate"] = digest
            if re.fullmatch(DIGEST, item["current"]):
                result["status"] = "current" if item["current"] == digest else "update"
            result["detail"] = "Exact top-level manifest/index digest comparison, not a platform-selected child digest. No layers downloaded."
            if not re.fullmatch(DIGEST, item["current"]):
                result["detail"] += " Floating image has no committed digest to compare."
        elif kind == "github-ref":
            candidate = client.ref(item["repo"], item["tracking"])
            current = item["current"]
            result["candidate"] = candidate
            if re.fullmatch(SHA, current):
                result["status"] = "current" if current == candidate else "update"
            else:
                result["detail"] = "Mutable action ref has no committed exact-head pin to compare."
            result["tracked_status"] = result["status"]
            result["detail"] += f" Tracking declared {item['tracking']}; branch/major boundary is preserved."
            if item.get("check_releases"):
                try:
                    latest_tag = client.latest(item["repo"])
                    latest_commit = client.ref(item["repo"], latest_tag)
                    hint_key = version_key(item["tracking"])
                    latest_key = version_key(latest_tag)
                    newer = bool(hint_key and latest_key and latest_key > hint_key)
                    moving_major = bool(re.fullmatch(r"v?\d+", item["tracking"]))
                    available = newer and (not moving_major or latest_commit != candidate)
                    result["release_review"] = {
                        "tag": latest_tag, "commit": latest_commit, "review_only": True,
                        "newer_available": available, "same_as_tracking_head": latest_commit == candidate,
                    }
                    if available:
                        result["status"] = "update"
                        result["detail"] += f" Newer stable release {latest_tag} is available for review only; do not automatically change tracking policy."
                except NoRelease:
                    result["release_review"] = {"published": False, "review_only": True}
        elif kind == "github-release":
            tag = client.latest(item["repo"])
            commit = client.ref(item["repo"], tag)
            result["candidate"] = {"tag": tag, "commit": commit}
            pinned_commit = client.ref(item["repo"], item["current"])
            if item.get("commit") and item["commit"] != pinned_commit:
                result["status"] = "update"
                result["detail"] = "Pinned source tag has moved away from its committed commit pin."
            else:
                result["status"] = "current" if tag == item["current"] else "update"
            result["detail"] += " Release/tag freshness only; executable asset checksums still require verification."
        elif kind == "nvidia":
            text = client.fetch(item["tracking"])[0].decode()
            match = re.match(r"\s*(\d+(?:\.\d+)+)\s", text)
            if not match:
                raise AuditError("NVIDIA latest.txt has no driver version")
            result["candidate"] = match[1]
            result["status"] = "current" if item["current"] == match[1] else "update"
        elif kind == "parity":
            if not re.fullmatch(SHA, item["current"]):
                raise AuditError("parity reference is not an exact commit pin")
            branch = client.api(f"repos/{item['repo']}")["default_branch"]
            head = client.ref(item["repo"], branch)
            result["candidate"] = {"branch": branch, "commit": head}
            path = urllib.parse.quote(item["path"], safe="/")
            content = client.api(f"repos/{item['repo']}/contents/{path}?ref={head}")
            if content.get("encoding") != "base64":
                raise AuditError("parity content is not inline base64")
            upstream = base64.b64decode(content["content"])
            local = (root / item["local"]).read_bytes()
            result["candidate"] = {"branch": branch, "commit": head, "sha256": hashlib.sha256(upstream).hexdigest()}
            result["status"] = "current" if local == upstream else "update"
            result["detail"] = f"Compared local bytes with live {branch}@{head}; an older parity pin alone does not imply content drift."
    except Exception as exc:
        result["status"] = "unknown" if isinstance(exc, NoRelease) or (isinstance(exc, AuditError) and "tracking hint" in str(exc)) else "error"
        result["detail"] = failure_message(exc)
    return result


def tracked_files(root):
    files = {}
    for record in git(root, "ls-files", "--stage", "-z").split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        mode, sha, stage = metadata.split()
        if stage != "0":
            raise AuditError("unmerged index; resolve conflicts before auditing")
        files[path] = (mode, sha)
    return files


def checkout_identity(root):
    head = git(root, "rev-parse", "HEAD").strip()
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    changes = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    # Status's raw NUL records preserve names without evaluating them.
    return {"root": str(root), "head": head, "branch": branch,
            "dirty": bool(changes), "changes": [part for part in changes.split("\0") if part],
            "scope": "tracked manifests read from local worktree; gitlinks read from index; not remote main"}


def exit_code(results, check=False):
    statuses = {item["status"] for item in results}
    if statuses & {"unknown", "error"}:
        return 2
    return 1 if check and "update" in statuses else 0


def audit(root, client, jobs=4):
    identity = checkout_identity(root)
    items = discover(root, tracked_files(root))
    remote_failed = False
    # Remote checkout identity is read from GitHub, never fetch/update a local ref.
    try:
        origin = git(root, "remote", "get-url", "origin").strip()
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?", origin)
        if not match:
            raise AuditError("origin is not a credential-free GitHub URL")
        remote_main = client.ref(match[1], "main")
        identity["remote_main"] = remote_main
        identity["matches_remote_main"] = remote_main == identity["head"]
    except Exception as exc:
        identity["remote_main_error"] = failure_message(exc)
        remote_failed = not isinstance(exc, AuditError)
        items.append(entry("limitation", "checkout remote main", "unresolved", "main", "git:origin", detail=identity["remote_main_error"]))
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        results = list(executor.map(lambda item: resolve(item, client, root), items))
    if remote_failed:
        for result in results:
            if result["name"] == "checkout remote main":
                result["status"] = "error"
    counts = {status: sum(item["status"] == status for item in results) for status in ("current", "update", "unknown", "error")}
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "checkout": identity, "summary": counts, "coverage_complete": not (counts["unknown"] or counts["error"]),
            "coverage": {
                "limitations": [item["name"] for item in results if item["kind"] == "limitation" and item["status"] == "unknown"],
                "failed_lookups": [item["name"] for item in results if item["status"] == "error"],
            },
            "results": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit one machine-readable JSON document")
    parser.add_argument("--check", action="store_true", help="exit 1 for updates; incomplete/errors always exit 2")
    parser.add_argument("--timeout", type=float, default=15, help="per-request timeout in seconds (1–60; default 15)")
    parser.add_argument("--jobs", type=int, default=4, help="concurrent input resolutions (1–8; default 4)")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 60 or not 1 <= args.jobs <= 8:
        parser.error("--timeout must be 1–60 and --jobs must be 1–8")
    try:
        report = audit(ROOT, Client(args.timeout), args.jobs)
    except Exception as exc:
        report = {"schema_version": 1, "coverage_complete": False, "results": [
            {"kind": "inventory", "name": "checkout inventory", "status": "error", "current": None,
             "candidate": None, "locations": [], "detail": failure_message(exc),
             "next_step": "Restore readable tracked manifests and git metadata, then rerun."}]}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        identity = report.get("checkout", {})
        print(f"Checkout: {identity.get('head', 'unknown')} ({identity.get('branch', 'unknown')}); dirty={identity.get('dirty', 'unknown')}")
        print(f"Remote main: {identity.get('remote_main', 'unresolved')}; matches={identity.get('matches_remote_main', False)}")
        print("Scope: local worktree, not a claim about remote main or installed package freshness")
        for item in report["results"]:
            print(f"\n{item['status'].upper():7} {item['kind']}: {item['name']}")
            print(f"  current: {item['current']}\n  candidate: {json.dumps(item['candidate'], sort_keys=True)}")
            print(f"  sources: {', '.join(item['locations'])}")
            if "tracked_status" in item:
                print(f"  tracked channel: {item['tracked_status']}")
            if isinstance(item.get("release_review"), dict):
                print("  release review: " + json.dumps(item["release_review"], sort_keys=True))
            print(f"  {item['detail']}\n  next: {item['next_step']}")
        print("\nSummary: " + json.dumps(report.get("summary", {}), sort_keys=True))
        print("Exit: 0=complete report (updates allowed without --check), 1=updates in --check, 2=incomplete/error coverage")
    return exit_code(report["results"], args.check)


if __name__ == "__main__":
    raise SystemExit(main())
