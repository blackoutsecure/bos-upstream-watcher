#!/usr/bin/env python3
"""Discover the latest version of an upstream project from one of several
sources. Reads inputs from env vars (set by the parent composite action),
writes a byte-stable tracker JSON file, diffs against the previous tracker,
and emits GitHub Actions outputs.

Stdlib-only: urllib + json + re + difflib. No third-party dependencies.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

__version__ = "1.0.0"

# Default tag/version filter (SemVer with optional `v` prefix and pre-release
# / build metadata). Used by github_tags and container_image when the caller
# does not supply an explicit pattern.
DEFAULT_TAG_PATTERN = r"^v?\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$"


def _user_agent() -> str:
    return os.environ.get("USER_AGENT_OVERRIDE") or f"bos-upstream-watcher/{__version__}"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def die(msg: str) -> None:
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.exit(1)


def http_get(
    url: str, *, headers: dict[str, str] | None = None, accept_json: bool = False
) -> tuple[int, bytes]:
    """GET `url` with a 3-retry loop on 5xx and connection errors."""
    req_headers = {"User-Agent": _user_agent()}
    if accept_json:
        req_headers["Accept"] = "application/json"
    if headers:
        req_headers.update(headers)

    last_err: Exception | None = None
    for attempt in range(1, 4):
        req = urllib.request.Request(url, headers=req_headers)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                # 4xx is terminal — retrying won't help.
                return exc.code, exc.read() or b""
            last_err = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_err = exc
        if attempt < 3:
            time.sleep(2 * attempt)
    die(f"GET {url} failed after 3 attempts: {last_err}")
    return 0, b""  # unreachable — die() exits


def gh_api(path: str) -> Any:
    """Authenticated GitHub REST API call. Honours `GH_TOKEN` from env."""
    url = f"https://api.github.com/{path.lstrip('/')}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = http_get(url, headers=headers, accept_json=True)
    if status >= 400:
        snippet = body[:400].decode("utf-8", "replace")
        hint = ""
        if status == 403 and "SAML enforcement" in snippet:
            hint = (
                "\nHINT: The token is valid but has not been authorized for the "
                "organization's SAML SSO. Open https://github.com/settings/tokens, "
                "find the PAT, click 'Configure SSO', and Authorize it for the "
                "target org. Fine-grained PATs and GitHub App installation "
                "tokens also work without per-PAT SSO authorization."
            )
        elif status in (401, 403):
            hint = (
                "\nHINT: Check that the supplied token has `contents: read` on "
                "the upstream repo and that it has not expired."
            )
        die(f"GitHub API {status} for {url}: {snippet}{hint}")
    return json.loads(body)


def write_outputs(pairs: dict[str, str]) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        die("GITHUB_OUTPUT is not set (are we running outside GitHub Actions?)")
    with open(out, "a", encoding="utf-8") as f:
        for k, v in pairs.items():
            if "\n" in v or "\r" in v:
                die(f"output '{k}' contains a newline (value={v!r})")
            f.write(f"{k}={v}\n")


# ---------------------------------------------------------------------------
# Version comparison (SemVer-ish)
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


def semver_key(tag: str) -> tuple:
    """Sort key that ranks SemVers correctly with pre-release ordering.

    A pre-release version compares less than the same base release
    (1.2.3-rc1 < 1.2.3). Pre-release identifiers are compared per the
    SemVer spec (numeric < non-numeric within each dot-segment). Build
    metadata is ignored for ordering, matching the spec.
    """
    m = _SEMVER_RE.match(tag)
    if not m:
        # Non-SemVer tags sort below everything else so they cannot win.
        return (-1, -1, -1, ())
    base = (int(m["major"]), int(m["minor"]), int(m["patch"]))
    pre = m["pre"]
    if pre is None:
        # No pre-release ranks HIGHER than any pre-release of the same base.
        return base + (1, ())
    parts: list[tuple[int, int | str]] = []
    for ident in pre.split("."):
        if ident.isdigit():
            parts.append((0, int(ident)))  # numeric < alphanumeric
        else:
            parts.append((1, ident))
    return base + (0, tuple(parts))


def pick_highest(tags: list[str], pattern: str) -> str:
    """Filter `tags` by `pattern`, then pick the highest by SemVer order."""
    regex = re.compile(pattern)
    candidates = [t for t in tags if regex.match(t)]
    if not candidates:
        die(f"no tags matched pattern {pattern!r} (saw {len(tags)} candidates)")
    candidates.sort(key=semver_key)
    return candidates[-1]


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


def _require(name: str, value: str) -> str:
    if not value:
        die(f"input '{name.lower()}' is required for source '{os.environ['SOURCE']}'")
    return value


def _validate_owner_repo(repo: str) -> None:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$", repo):
        die(f"input 'upstream_repo' must be 'owner/name' (got {repo!r})")


def _strip_v(version: str, strip: bool) -> str:
    return version[1:] if strip and version.startswith("v") else version


# Accepts X, X.Y, or X.Y.Z (optionally with SemVer pre-release / build suffix)
# and pads short forms to X.Y.Z. Useful for upstreams that ship Debian-style
# versions like `8.1` or `11.0` rather than `8.1.0`.
_SHORT_SEMVER_RE = re.compile(r"^(?P<base>\d+(?:\.\d+){0,2})(?P<suffix>[-+][0-9A-Za-z.-]+)?$")


def _normalize_semver(version: str) -> str:
    m = _SHORT_SEMVER_RE.match(version)
    if not m:
        return version
    parts = m["base"].split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts) + (m["suffix"] or "")


def provider_github_release(env: dict[str, str]) -> dict[str, Any]:
    repo = _require("UPSTREAM_REPO", env["UPSTREAM_REPO"])
    _validate_owner_repo(repo)

    rel = gh_api(f"repos/{repo}/releases/latest")
    tag = rel.get("tag_name") or ""
    if not tag:
        die(f"upstream {repo} has no latest-release tag_name")

    # `commits/<tag>` resolves both lightweight and annotated tags to the
    # underlying commit SHA in a single call. The Git Refs API would return
    # the tag-OBJECT SHA for annotated tags, which is NOT the commit.
    commit_info = gh_api(f"repos/{repo}/commits/{tag}")
    commit = commit_info.get("sha") or ""
    if not commit:
        die(f"could not resolve commit for {repo}@{tag}")

    version = _strip_v(tag, env["STRIP_V_PREFIX"] == "true")
    tracker = {"repo": repo, "tag": tag, "version": version, "commit": commit}
    return {
        "tag": tag,
        "version": version,
        "commit": commit,
        "source_url": f"https://github.com/{repo}/releases/tag/{tag}",
        "tracker": tracker,
    }


def provider_github_branch_file(env: dict[str, str]) -> dict[str, Any]:
    repo = _require("UPSTREAM_REPO", env["UPSTREAM_REPO"])
    branch = _require("UPSTREAM_BRANCH", env["UPSTREAM_BRANCH"])
    path = _require("VERSION_FILE_PATH", env["VERSION_FILE_PATH"])
    _validate_owner_repo(repo)

    if re.search(r"(^-|[\s]|\.\.|~|\^|:|\?|\*|\[|\\)", branch):
        die(f"input 'upstream_branch' contains characters Git rejects: {branch!r}")
    if path.startswith("/") or ".." in path:
        die(f"input 'version_file_path' must be repo-relative: {path!r}")

    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
    status, body = http_get(url)
    if status >= 400:
        die(f"could not read {url} (HTTP {status})")
    body_text = body.decode("utf-8", "replace")

    regex_src = env["VERSION_REGEX"]
    if regex_src:
        try:
            regex = re.compile(regex_src)
        except re.error as exc:
            die(f"input 'version_regex' is not a valid regex: {exc}")
        if regex.groups < 1:
            die("input 'version_regex' must have at least one capture group")
        m = regex.search(body_text)
        if not m:
            die(f"regex {regex_src!r} did not match body of {url}")
        version_raw = m.group(1).strip()
        if not version_raw:
            die(f"capture group from {url} is empty")
    else:
        version_raw = body_text.strip()
        if not version_raw:
            die(f"empty version string at {url}")

    if not re.match(r"^[0-9]+(\.[0-9]+){0,2}([-+][0-9A-Za-z.-]+)?$", version_raw):
        die(f"version {version_raw!r} at {url} is not SemVer-shaped (X[.Y[.Z]][-suffix])")

    head = gh_api(f"repos/{repo}/commits/{branch}")
    commit = head.get("sha") or ""
    if not commit:
        die(f"branch {branch!r} not found in {repo}")

    version = _normalize_semver(_strip_v(version_raw, env["STRIP_V_PREFIX"] == "true"))
    tracker = {
        "repo": repo,
        "source": "github_branch_file",
        "branch": branch,
        "version": version,
        "commit": commit,
    }
    return {
        "tag": version,
        "version": version,
        "commit": commit,
        "source_url": url,
        "tracker": tracker,
    }


def provider_github_tags(env: dict[str, str]) -> dict[str, Any]:
    repo = _require("UPSTREAM_REPO", env["UPSTREAM_REPO"])
    _validate_owner_repo(repo)
    pattern = env["TAG_PATTERN"] or DEFAULT_TAG_PATTERN

    # Paginate up to 5 pages of 100 tags each (sufficient for any reasonable
    # release cadence). Bail early if a page returns fewer than per_page items.
    all_tags: list[dict[str, Any]] = []
    for page in range(1, 6):
        chunk = gh_api(f"repos/{repo}/tags?per_page=100&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
        all_tags.extend(chunk)
        if len(chunk) < 100:
            break

    if not all_tags:
        die(f"{repo} has no tags")

    tag_names = [t["name"] for t in all_tags if "name" in t]
    chosen = pick_highest(tag_names, pattern)
    commit = next(
        (t["commit"]["sha"] for t in all_tags if t["name"] == chosen and "commit" in t),
        "",
    )
    if not commit:
        die(f"could not resolve commit SHA for {repo}@{chosen}")

    version = _strip_v(chosen, env["STRIP_V_PREFIX"] == "true")
    tracker = {
        "repo": repo,
        "source": "github_tags",
        "tag": chosen,
        "version": version,
        "commit": commit,
    }
    return {
        "tag": chosen,
        "version": version,
        "commit": commit,
        "source_url": f"https://github.com/{repo}/releases/tag/{chosen}",
        "tracker": tracker,
    }


def provider_container_image(env: dict[str, str]) -> dict[str, Any]:
    image_ref = _require("IMAGE_REF", env["IMAGE_REF"])
    pattern = env["TAG_PATTERN"] or DEFAULT_TAG_PATTERN

    # Accept `docker.io/<ns>/<img>` and bare `<ns>/<img>` (assumed docker.io).
    # Other registries (ghcr.io, mcr.microsoft.com, etc.) are out of scope
    # in this revision — bearer-token bootstrap differs per registry.
    ref = image_ref
    if ref.startswith("docker.io/"):
        ref = ref[len("docker.io/") :]
    elif "/" not in ref:
        die(f"input 'image_ref' must be 'namespace/image' (got {image_ref!r})")
    elif "." in ref.split("/")[0] or ":" in ref.split("/")[0]:
        die(f"input 'image_ref' only supports docker.io in this revision (got {image_ref!r})")

    # `library/<name>` is the canonical Docker Hub path for official images.
    if ref.count("/") == 0:
        ref = f"library/{ref}"
    ns, _, name = ref.partition("/")
    if not ns or not name:
        die(f"could not parse namespace/image from {image_ref!r}")

    all_tags: list[str] = []
    next_url: str | None = f"https://hub.docker.com/v2/repositories/{ns}/{name}/tags/?page_size=100"
    pages = 0
    while next_url and pages < 5:
        status, body = http_get(next_url, accept_json=True)
        if status >= 400:
            die(f"Docker Hub returned {status} for {next_url}")
        data = json.loads(body)
        all_tags.extend(t["name"] for t in data.get("results", []) if "name" in t)
        next_url = data.get("next")
        pages += 1

    if not all_tags:
        die(f"no tags found at hub.docker.com/{ns}/{name}")

    chosen = pick_highest(all_tags, pattern)
    version = _strip_v(chosen, env["STRIP_V_PREFIX"] == "true")
    tracker = {
        "image": f"docker.io/{ns}/{name}",
        "source": "container_image",
        "tag": chosen,
        "version": version,
    }
    return {
        "tag": chosen,
        "version": version,
        "commit": "",
        "source_url": f"https://hub.docker.com/r/{ns}/{name}/tags",
        "tracker": tracker,
    }


def provider_npm(env: dict[str, str]) -> dict[str, Any]:
    pkg = _require("PACKAGE_NAME", env["PACKAGE_NAME"])
    # npm allows `@scope/name`; reject anything else exotic to keep the URL safe.
    if not re.match(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$", pkg):
        die(f"input 'package_name' is not a valid npm package name: {pkg!r}")

    # The npm registry expects `@scope%2Fname` for the path component.
    safe = pkg.replace("/", "%2F") if pkg.startswith("@") else pkg
    url = f"https://registry.npmjs.org/{safe}/latest"
    status, body = http_get(url, accept_json=True)
    if status >= 400:
        die(f"npm registry returned {status} for {url}")
    data = json.loads(body)
    version = data.get("version") or ""
    if not version:
        die(f"no version in {url}")

    version = _strip_v(version, env["STRIP_V_PREFIX"] == "true")
    tracker = {"package": pkg, "source": "npm", "version": version}
    return {
        "tag": version,
        "version": version,
        "commit": "",
        "source_url": url,
        "tracker": tracker,
    }


def provider_pypi(env: dict[str, str]) -> dict[str, Any]:
    pkg = _require("PACKAGE_NAME", env["PACKAGE_NAME"])
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", pkg):
        die(f"input 'package_name' is not a valid PyPI package name: {pkg!r}")

    url = f"https://pypi.org/pypi/{pkg}/json"
    status, body = http_get(url, accept_json=True)
    if status >= 400:
        die(f"PyPI returned {status} for {url}")
    data = json.loads(body)
    version = (data.get("info") or {}).get("version") or ""
    if not version:
        die(f"no version in {url}")

    version = _strip_v(version, env["STRIP_V_PREFIX"] == "true")
    tracker = {"package": pkg, "source": "pypi", "version": version}
    return {
        "tag": version,
        "version": version,
        "commit": "",
        "source_url": url,
        "tracker": tracker,
    }


def provider_generic_url(env: dict[str, str]) -> dict[str, Any]:
    url = _require("VERSION_URL", env["VERSION_URL"])
    pattern = _require("VERSION_REGEX", env["VERSION_REGEX"])
    if not url.startswith(("https://", "http://")):
        die(f"input 'version_url' must be http(s)://: {url!r}")

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        die(f"input 'version_regex' is not a valid regex: {exc}")
    if regex.groups < 1:
        die("input 'version_regex' must have at least one capture group")

    status, body = http_get(url)
    if status >= 400:
        die(f"GET {url} returned HTTP {status}")
    text = body.decode("utf-8", "replace")
    m = regex.search(text)
    if not m:
        die(f"regex {pattern!r} did not match body of {url}")

    raw = m.group(1).strip()
    if not raw:
        die(f"capture group from {url} is empty")

    version = _normalize_semver(_strip_v(raw, env["STRIP_V_PREFIX"] == "true"))
    tracker = {"url": url, "source": "generic_url", "version": version}
    return {
        "tag": version,
        "version": version,
        "commit": "",
        "source_url": url,
        "tracker": tracker,
    }


PROVIDERS: dict[str, Callable[[dict[str, str]], dict[str, Any]]] = {
    "github_release": provider_github_release,
    "github_branch_file": provider_github_branch_file,
    "github_tags": provider_github_tags,
    "container_image": provider_container_image,
    "npm": provider_npm,
    "pypi": provider_pypi,
    "generic_url": provider_generic_url,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

ENV_KEYS = (
    "SOURCE",
    "UPSTREAM_REPO",
    "UPSTREAM_BRANCH",
    "VERSION_FILE_PATH",
    "IMAGE_REF",
    "PACKAGE_NAME",
    "VERSION_URL",
    "VERSION_REGEX",
    "TAG_PATTERN",
    "STRIP_V_PREFIX",
    "TRACKER_PATH",
)


def run(env: dict[str, str]) -> dict[str, str]:
    """Run a provider and return the outputs dict. Side-effect: writes the
    tracker file when `TRACKER_PATH` is set. Returns the dict that would be
    appended to `GITHUB_OUTPUT`.
    """
    source = env["SOURCE"]
    if source not in PROVIDERS:
        die(f"unknown source {source!r}")

    result = PROVIDERS[source](env)

    for k in ("tag", "version", "commit", "source_url"):
        v = str(result.get(k, ""))
        if any(c in v for c in "\r\n"):
            die(f"resolved {k} contains a newline: {v!r}")

    # Serialise tracker JSON with stable formatting (2-space indent, trailing
    # newline, insertion-order keys) so existing tracker files stay byte-stable
    # across runs.
    tracker_text = json.dumps(result["tracker"], indent=2) + "\n"

    changed = "true"
    if env["TRACKER_PATH"]:
        path = env["TRACKER_PATH"]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        prev = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                prev = f.read()
        if prev == tracker_text:
            changed = "false"
            print(f"No change: {source} {result['version']} matches existing tracker.")
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(tracker_text)
            if prev:
                print(f"Change detected ({source}):")
                sys.stdout.writelines(
                    difflib.unified_diff(
                        prev.splitlines(keepends=True),
                        tracker_text.splitlines(keepends=True),
                        fromfile=path,
                        tofile=f"{path}.new",
                    )
                )
            else:
                print(f"First run for {source} {result['version']} — wrote {path}")
    else:
        print(
            f"No tracker_path configured; reporting changed=true for {source} {result['version']}"
        )

    return {
        "changed": changed,
        "version": result["version"],
        "tag": result["tag"],
        "commit": result["commit"],
        "source_url": result["source_url"],
        "tracker_path": env["TRACKER_PATH"],
    }


def main() -> int:
    env = {k: os.environ.get(k, "") for k in ENV_KEYS}
    outputs = run(env)
    write_outputs(outputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
