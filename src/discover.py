#!/usr/bin/env python3
"""Discover the latest version of an upstream project from one of several
sources. Reads inputs from env vars (set by the parent composite action) on
top of a layered JSON configuration, writes a byte-stable tracker JSON file,
diffs against the previous tracker, and emits GitHub Actions outputs plus an
audit-style run report.

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
from typing import Any, NamedTuple

import watcher_ai
import watcher_config
import watcher_metadata
import watcher_reporting

__version__ = "1.2.0"

# Default tag/version filter (SemVer with optional `v` prefix and pre-release
# / build metadata). Used by github_tags and container_image when the caller
# does not supply an explicit pattern.
DEFAULT_TAG_PATTERN = r"^v?\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$"


def _user_agent() -> str:
    return os.environ.get("USER_AGENT_OVERRIDE") or f"bos-upstream-watcher/{__version__}"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


class WatcherExit(SystemExit):
    """Fatal error that still carries its message to the report renderer."""

    def __init__(self, message: str) -> None:
        super().__init__(1)
        self.message = message

    def __str__(self) -> str:
        return self.message


def die(msg: str) -> None:
    sys.stderr.write(f"ERROR: {msg}\n")
    raise WatcherExit(msg)


def http_request(
    url: str, *, headers: dict[str, str] | None = None, accept_json: bool = False
) -> tuple[int, bytes, dict[str, str]]:
    """GET `url` with a 3-retry loop on 5xx and connection errors.
    Returns `(status, body, response_headers)`. Response headers are
    lowercased so callers can look them up case-insensitively (e.g. for
    OCI Distribution `Link:` pagination).
    """
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
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, resp.read(), resp_headers
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                # 4xx is terminal — retrying won't help.
                err_headers = (
                    {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
                )
                return exc.code, exc.read() or b"", err_headers
            last_err = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_err = exc
        if attempt < 3:
            time.sleep(2 * attempt)
    die(f"GET {url} failed after 3 attempts: {last_err}")
    return 0, b"", {}  # unreachable — die() exits


# OCI Distribution-style pagination uses an RFC 5988 `Link: <url>; rel="next"`
# header. Minimal parser — returns the `next` URL or None.
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.IGNORECASE)


def _parse_link_next(link_header: str) -> str | None:
    if not link_header:
        return None
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


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
    status, body, _ = http_request(url, headers=headers, accept_json=True)
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


def write_outputs(pairs: dict[str, str], *, multiline_keys: frozenset[str] = frozenset()) -> None:
    """Append `pairs` to `$GITHUB_OUTPUT`.

    Single-line values use `key=value`. Values whose key is in `multiline_keys`
    are emitted using GitHub's heredoc syntax so newlines are preserved
    intact. Single-line keys that contain a newline are rejected — prevents
    injection-via-newline into adjacent keys. This is the SINGLE source of
    truth for output validation; callers that build the pairs dict don't
    need to re-validate.
    """
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        die("GITHUB_OUTPUT is not set (are we running outside GitHub Actions?)")
    with open(out, "a", encoding="utf-8") as f:
        for k, v in pairs.items():
            if k in multiline_keys:
                # Pick a delimiter that does not appear in the value, even
                # adversarially. Base delimiter is namespaced; extend with
                # suffix bytes only if a collision is detected.
                delim = "BOS_UPSTREAM_EOF"
                while delim in v:
                    delim += "_X"
                f.write(f"{k}<<{delim}\n{v}\n{delim}\n")
                continue
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


def _require_repo(env: dict[str, str]) -> str:
    """Read + validate the `UPSTREAM_REPO` env var. Used by every provider
    that targets a GitHub repo."""
    repo = _require("UPSTREAM_REPO", env["UPSTREAM_REPO"])
    _validate_owner_repo(repo)
    return repo


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


def _select_release_with_prereleases(repo: str, pattern: str) -> dict[str, Any]:
    """List releases (including pre-releases), filter by `pattern`, pick the
    highest by SemVer order. Draft releases are skipped. Paginates up to 5
    pages of 100 releases (matches the github_tags limit).
    """
    releases: list[dict[str, Any]] = []
    for page in range(1, 6):
        chunk = gh_api(f"repos/{repo}/releases?per_page=100&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
        releases.extend(chunk)
        if len(chunk) < 100:
            break

    if not releases:
        die(f"{repo} has no releases")

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        die(f"input 'tag_pattern' is not a valid regex: {exc}")

    matched = [
        r
        for r in releases
        if not r.get("draft") and r.get("tag_name") and regex.match(r["tag_name"])
    ]
    if not matched:
        die(
            f"no releases matched pattern {pattern!r} "
            f"(saw {len(releases)} releases including pre-releases)"
        )

    matched.sort(key=lambda r: semver_key(r["tag_name"]))
    return matched[-1]


def provider_github_release(env: dict[str, str]) -> dict[str, Any]:
    repo = _require("UPSTREAM_REPO", env["UPSTREAM_REPO"])
    _validate_owner_repo(repo)

    if env.get("INCLUDE_PRERELEASES") == "true":
        # Caller opted into pre-release tracking: enumerate `/releases`
        # (which DOES include pre-releases, unlike `/releases/latest`),
        # filter by tag_pattern, and pick the highest by SemVer order.
        pattern = env["TAG_PATTERN"] or DEFAULT_TAG_PATTERN
        rel = _select_release_with_prereleases(repo, pattern)
    else:
        # Default: trust the upstream's own "latest" pointer.
        rel = gh_api(f"repos/{repo}/releases/latest")

    tag = rel.get("tag_name") or ""
    if not tag:
        die(f"upstream {repo} has no tag_name in the selected release")

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
        "label": repo,
        "release_url": rel.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}",
        "release_name": rel.get("name") or "",
        "release_body": rel.get("body") or "",
        "published_at": rel.get("published_at") or "",
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
    status, body, _ = http_request(url)
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
        "label": repo,
        "tracker": tracker,
    }


def provider_github_tags(env: dict[str, str]) -> dict[str, Any]:
    repo = _require_repo(env)
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
        "label": repo,
        "tracker": tracker,
    }


# ---------------------------------------------------------------------------
# container_image — multi-registry helpers
# ---------------------------------------------------------------------------


def _split_two_segments(path: str, registry: str, image_ref: str) -> tuple[str, str]:
    if path.count("/") != 1 or "" in path.split("/"):
        die(
            f"input 'image_ref' for {registry} must be '{registry}/<namespace>/<image>' "
            f"(got {image_ref!r})"
        )
    ns, _, name = path.partition("/")
    return ns, name


def _list_dockerhub_tags(ns: str, name: str) -> list[str]:
    all_tags: list[str] = []
    next_url: str | None = (
        f"https://hub.docker.com/v2/repositories/{ns}/{name}/tags/?page_size=100"
    )
    pages = 0
    while next_url and pages < 5:
        status, body, _ = http_request(next_url, accept_json=True)
        if status >= 400:
            die(f"Docker Hub returned {status} for {next_url}")
        data = json.loads(body)
        all_tags.extend(t["name"] for t in data.get("results", []) if "name" in t)
        next_url = data.get("next")
        pages += 1
    return all_tags


def _list_ghcr_tags(owner: str, image: str) -> list[str]:
    """List tags via the OCI Distribution API. Public images only — private
    images require a PAT with `read:packages` scope, deferred to a future
    revision.
    """
    # GHCR's anonymous bearer-token endpoint issues a scoped token for the
    # requested repo. For PRIVATE repos it returns 200 with an unscoped
    # token that fails on /v2 (giving us a 401 to diagnose), so the token
    # call itself doesn't need a special-case.
    token_url = (
        f"https://ghcr.io/token?service=ghcr.io"
        f"&scope=repository:{owner}/{image}:pull"
    )
    status, body, _ = http_request(token_url, accept_json=True)
    if status >= 400:
        die(f"GHCR token endpoint returned {status} for {owner}/{image}")
    try:
        token = json.loads(body).get("token", "")
    except json.JSONDecodeError as exc:
        die(f"GHCR token endpoint returned non-JSON for {owner}/{image}: {exc}")
    if not token:
        die(f"GHCR did not issue a bearer token for {owner}/{image}")

    all_tags: list[str] = []
    next_url: str | None = f"https://ghcr.io/v2/{owner}/{image}/tags/list?n=100"
    pages = 0
    auth_header = {"Authorization": f"Bearer {token}"}
    while next_url and pages < 5:
        status, body, headers = http_request(next_url, headers=auth_header, accept_json=True)
        if status == 401:
            die(
                f"GHCR returned 401 for {owner}/{image} — image may be private. "
                f"Private GHCR images are not yet supported by this action."
            )
        if status == 404:
            die(f"GHCR image not found: ghcr.io/{owner}/{image}")
        if status >= 400:
            die(f"GHCR returned {status} for {next_url}")
        data = json.loads(body)
        all_tags.extend(data.get("tags") or [])
        # OCI Distribution pagination via RFC 5988 Link header. The `next`
        # URL is path-only (relative to the registry root).
        nxt = _parse_link_next(headers.get("link", ""))
        if nxt and nxt.startswith("/"):
            nxt = f"https://ghcr.io{nxt}"
        next_url = nxt
        pages += 1
    return all_tags


def _list_quay_tags(ns: str, name: str) -> list[str]:
    """List tags via Quay's v1 REST API (not OCI-compatible). Public
    repositories only; private repos require an `Authorization: Bearer`
    header that is deferred to a future revision.
    """
    all_tags: list[str] = []
    page = 1
    while page <= 5:
        url = (
            f"https://quay.io/api/v1/repository/{ns}/{name}/tag/"
            f"?onlyActiveTags=true&limit=100&page={page}"
        )
        status, body, _ = http_request(url, accept_json=True)
        if status == 404:
            die(f"Quay repository not found: quay.io/{ns}/{name}")
        if status in (401, 403):
            die(
                f"Quay returned {status} for {ns}/{name} — repository may be private. "
                f"Private Quay repos are not yet supported by this action."
            )
        if status >= 400:
            die(f"Quay returned {status} for {url}")
        data = json.loads(body)
        chunk = data.get("tags") or []
        all_tags.extend(t["name"] for t in chunk if "name" in t)
        if not data.get("has_additional"):
            break
        page += 1
    return all_tags


class _RegistryConfig(NamedTuple):
    """Per-registry config consumed by `provider_container_image`. Adding a
    new registry = one entry in `_REGISTRIES` + one lister function above."""

    normalize_path: Callable[[str], str]
    list_tags: Callable[[str, str], list[str]]
    source_url: Callable[[str, str], str]


_REGISTRIES: dict[str, _RegistryConfig] = {
    "docker.io": _RegistryConfig(
        # Bare image names map to `library/<name>` on Docker Hub.
        normalize_path=lambda p: p if "/" in p else f"library/{p}",
        list_tags=_list_dockerhub_tags,
        source_url=lambda ns, name: f"https://hub.docker.com/r/{ns}/{name}/tags",
    ),
    "ghcr.io": _RegistryConfig(
        normalize_path=lambda p: p,
        list_tags=_list_ghcr_tags,
        # GitHub's container package URL uses the IMAGE name, not the owner.
        source_url=lambda owner, image: f"https://github.com/{owner}/{image}/pkgs/container/{image}",
    ),
    "quay.io": _RegistryConfig(
        normalize_path=lambda p: p,
        list_tags=_list_quay_tags,
        source_url=lambda ns, name: f"https://quay.io/repository/{ns}/{name}?tab=tags",
    ),
}


def _parse_image_ref(image_ref: str) -> tuple[str, str]:
    """Split `image_ref` into `(registry_host, path)`.

    Examples:
      `nginx`                            -> (`docker.io`, `nginx`)
      `library/nginx`                    -> (`docker.io`, `library/nginx`)
      `docker.io/library/nginx`          -> (`docker.io`, `library/nginx`)
      `ghcr.io/blackoutsecure/runner`    -> (`ghcr.io`, `blackoutsecure/runner`)
      `quay.io/prometheus/node-exporter` -> (`quay.io`, `prometheus/node-exporter`)
      `gcr.io/foo/bar`                   -> die (unsupported registry)

    The heuristic for "is the first segment a registry host?" matches the
    OCI spec: a registry host contains a `.` or `:` (port). Bare names and
    `<namespace>/<image>` are treated as Docker Hub.
    """
    for prefix in _REGISTRIES:
        if image_ref.startswith(f"{prefix}/"):
            return prefix, image_ref[len(prefix) + 1 :]

    first = image_ref.split("/", 1)[0]
    if "." in first or ":" in first:
        die(
            f"input 'image_ref' uses an unsupported registry "
            f"(got {image_ref!r}; supported: {', '.join(_REGISTRIES)})"
        )

    # Bare name or `ns/name` — assume Docker Hub.
    return "docker.io", image_ref


def provider_container_image(env: dict[str, str]) -> dict[str, Any]:
    image_ref = _require("IMAGE_REF", env["IMAGE_REF"])
    pattern = env["TAG_PATTERN"] or DEFAULT_TAG_PATTERN

    registry, path = _parse_image_ref(image_ref)
    cfg = _REGISTRIES[registry]  # _parse_image_ref already gated this

    path = cfg.normalize_path(path)
    ns, name = _split_two_segments(path, registry, image_ref)
    all_tags = cfg.list_tags(ns, name)
    canonical = f"{registry}/{ns}/{name}"

    if not all_tags:
        die(f"no tags found for {canonical}")

    chosen = pick_highest(all_tags, pattern)
    version = _strip_v(chosen, env["STRIP_V_PREFIX"] == "true")
    return {
        "tag": chosen,
        "version": version,
        "source_url": cfg.source_url(ns, name),
        "label": canonical,
        "tracker": {
            "image": canonical,
            "source": "container_image",
            "tag": chosen,
            "version": version,
        },
    }


def provider_npm(env: dict[str, str]) -> dict[str, Any]:
    pkg = _require("PACKAGE_NAME", env["PACKAGE_NAME"])
    # npm allows `@scope/name`; reject anything else exotic to keep the URL safe.
    if not re.match(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$", pkg):
        die(f"input 'package_name' is not a valid npm package name: {pkg!r}")

    # The npm registry expects `@scope%2Fname` for the path component.
    safe = pkg.replace("/", "%2F") if pkg.startswith("@") else pkg
    url = f"https://registry.npmjs.org/{safe}/latest"
    status, body, _ = http_request(url, accept_json=True)
    if status >= 400:
        die(f"npm registry returned {status} for {url}")
    data = json.loads(body)
    version = data.get("version") or ""
    if not version:
        die(f"no version in {url}")

    version = _strip_v(version, env["STRIP_V_PREFIX"] == "true")
    return {
        "tag": version,
        "version": version,
        "source_url": url,
        "label": pkg,
        "tracker": {"package": pkg, "source": "npm", "version": version},
    }


def provider_pypi(env: dict[str, str]) -> dict[str, Any]:
    pkg = _require("PACKAGE_NAME", env["PACKAGE_NAME"])
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", pkg):
        die(f"input 'package_name' is not a valid PyPI package name: {pkg!r}")

    url = f"https://pypi.org/pypi/{pkg}/json"
    status, body, _ = http_request(url, accept_json=True)
    if status >= 400:
        die(f"PyPI returned {status} for {url}")
    data = json.loads(body)
    version = (data.get("info") or {}).get("version") or ""
    if not version:
        die(f"no version in {url}")

    version = _strip_v(version, env["STRIP_V_PREFIX"] == "true")
    return {
        "tag": version,
        "version": version,
        "source_url": url,
        "label": pkg,
        "tracker": {"package": pkg, "source": "pypi", "version": version},
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

    status, body, _ = http_request(url)
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
    return {
        "tag": version,
        "version": version,
        "source_url": url,
        "label": url,
        "tracker": {"url": url, "source": "generic_url", "version": version},
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
    "INCLUDE_PRERELEASES",
)

# Config-cascade and reporting controls read straight from the composite
# action's `env:` block. They never reach a provider.
CONTROL_ENV_KEYS = (
    "USE_GLOBAL_CONFIG",
    "GLOBAL_CONFIG_PATH",
    "GLOBAL_CONFIG_JSON",
    "CONFIG_PATH",
    "CONFIG_JSON",
    "ENABLE_AI",
    "AI_PROVIDER",
    "ENABLE_JOB_SUMMARY",
)


# Outputs whose value MAY legitimately contain newlines (release notes and the
# AI digest are unstructured prose). All other outputs stay single-line and use
# the standard `key=value` format. `write_outputs` enforces this distinction.
_MULTILINE_OUTPUTS = frozenset({"release_body", "ai_summary"})

# Output keys whose values come from the provider's result dict. Drives the
# outputs dict in `run()` so adding a new field = one entry here + one entry
# in the provider's return dict, nothing else.
_OUTPUT_KEYS_FROM_RESULT = (
    "version",
    "tag",
    "commit",
    "source_url",
    "label",
    "release_url",
    "release_name",
    "release_body",
    "published_at",
)


def _tracker_version(text: str) -> str:
    """Best-effort read of the version recorded in a previous tracker file."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return ""
    return str(data.get("version", "")) if isinstance(data, dict) else ""


def update_type(previous: str, current: str) -> str:
    """Classify the SemVer distance between two versions."""
    if not previous or not current:
        return "unknown"
    if previous == current:
        return "none"
    old, new = _SEMVER_RE.match(previous), _SEMVER_RE.match(current)
    if not old or not new:
        return "unknown"
    for part in ("major", "minor", "patch"):
        if int(old.group(part)) != int(new.group(part)):
            return part
    return "prerelease"


def run(env: dict[str, str]) -> dict[str, str]:
    """Run a provider and return the outputs dict. Side-effect: writes the
    tracker file when `TRACKER_PATH` is set. The returned dict is what
    `main()` appends to `GITHUB_OUTPUT`; `write_outputs()` is the single
    source of truth for newline validation.
    """
    source = env["SOURCE"]
    if source not in PROVIDERS:
        die(f"unknown source {source!r}")

    result = PROVIDERS[source](env)

    # Serialise tracker JSON with stable formatting (2-space indent, trailing
    # newline, insertion-order keys) so existing tracker files stay byte-stable
    # across runs.
    tracker_text = json.dumps(result["tracker"], indent=2) + "\n"

    changed = "true"
    previous_version = ""
    if env["TRACKER_PATH"]:
        path = env["TRACKER_PATH"]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        prev = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                prev = f.read()
        previous_version = _tracker_version(prev)
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
        **{k: result.get(k, "") for k in _OUTPUT_KEYS_FROM_RESULT},
        "tracker_path": env["TRACKER_PATH"],
        "previous_version": previous_version,
        "update_type": update_type(previous_version, str(result.get("version", ""))),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_report(
    env: dict[str, str],
    outputs: dict[str, str],
    config: watcher_config.ResolvedConfig,
) -> watcher_reporting.RunReport:
    """Assess a completed run into deterministic findings."""
    report = watcher_reporting.RunReport(context=_context(env, outputs, config))
    label = outputs.get("label") or env.get("SOURCE", "")
    version = outputs.get("version", "")

    report.add(
        watcher_reporting.Finding(
            rule_id="UW-RES-001",
            category="Upstream version resolution",
            severity="pass",
            location=outputs.get("source_url") or label,
            evidence=f"Resolved {label} to version {version} (tag {outputs.get('tag') or 'n/a'}).",
            remediation="No action required.",
        )
    )

    changed = outputs.get("changed") == "true"
    delta = outputs.get("update_type", "unknown")
    previous = outputs.get("previous_version") or "unknown"
    report.add(
        watcher_reporting.Finding(
            rule_id="UW-CHG-001",
            category="Upstream change detection",
            severity="warn" if changed and delta == "major" else "pass",
            location=label,
            evidence=(
                f"Upstream changed from {previous} to {version} ({delta} update)."
                if changed
                else f"No upstream change; {version} still matches the tracker."
            ),
            remediation=(
                "Review the upstream release notes before promoting a major upgrade downstream."
                if changed and delta == "major"
                else "No action required."
            ),
        )
    )

    tracker = env.get("TRACKER_PATH", "")
    report.add(
        watcher_reporting.Finding(
            rule_id="UW-TRK-001",
            category="Tracker file state",
            severity="pass" if tracker else "skip",
            location=tracker or "tracker disabled",
            evidence=(
                f"Tracker file {tracker} is up to date."
                if tracker
                else "No tracker path configured; every run reports changed=true."
            ),
            remediation=(
                "No action required."
                if tracker
                else "Set `tracker_path` to persist upstream state and get real change detection."
            ),
        )
    )

    report.add(
        watcher_reporting.Finding(
            rule_id="UW-CFG-000",
            category="Configuration cascade",
            severity="pass",
            location=config.repository_config or "action inputs",
            evidence=f"Applied tiers: {', '.join(config.sources) or 'action inputs only'}.",
            remediation="No action required.",
        )
    )
    return report


def _context(
    env: dict[str, str],
    outputs: dict[str, str],
    config: watcher_config.ResolvedConfig,
) -> watcher_reporting.RunContext:
    return watcher_reporting.RunContext(
        command="discover",
        source=env.get("SOURCE", ""),
        label=outputs.get("label", ""),
        tracker_path=env.get("TRACKER_PATH", ""),
        repository_config=config.repository_config,
        global_config=config.global_config,
        config_sources=config.sources,
        ignored_metadata_keys=config.ignored_metadata_keys,
        package_version=__version__,
    )


def apply_ai_digest(
    report: watcher_reporting.RunReport,
    outputs: dict[str, str],
    config: watcher_config.ResolvedConfig,
) -> None:
    """Attach an advisory release digest, falling back to local heuristics."""
    settings = config.ai
    heuristic_impact = watcher_ai.heuristic_impact(
        outputs.get("update_type", "unknown"), outputs.get("release_body", "")
    )
    if not settings.enable_ai_release_summary:
        report.ai.status = "Disabled by configuration"
    elif outputs.get("changed") != "true":
        report.ai.status = "Skipped: no upstream change to summarize"
    else:
        provider = watcher_ai.detect_provider(settings.ai_release_summary_provider)
        if provider is None:
            report.ai.status = "Unavailable: no AI provider credential on this runner"
        else:
            digest = watcher_ai.release_digest(
                {**outputs, "source": report.context.source},
                provider,
                timeout=settings.timeout_seconds,
            )
            if digest is None:
                report.ai.status = f"Unavailable: {provider.name} returned no usable response"
            else:
                report.ai.status = f"Used ({provider.name})"
                report.ai.summary = digest.summary
                report.ai.impact = digest.impact
                report.ai.source = digest.source

    if not report.ai.summary and settings.local_heuristic_fallback:
        report.ai.summary = watcher_ai.heuristic_summary(
            {**outputs, "source": report.context.source}
        )
        report.ai.impact = heuristic_impact
        report.ai.source = "Blackout Secure deterministic heuristics"

    outputs["ai_summary"] = report.ai.summary
    outputs["ai_impact"] = report.ai.impact or heuristic_impact
    outputs["ai_status"] = report.ai.status


def emit_report(
    report: watcher_reporting.RunReport,
    settings: watcher_reporting.ReportingSettings,
) -> None:
    """Write the job summary and annotations; never fatal on I/O failure."""
    for line in watcher_reporting.emit_annotations(report, settings):
        print(line)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not settings.enable_job_summary or not summary_path:
        return
    text = watcher_reporting.render_summary(report, settings)
    if not watcher_reporting.append_summary(summary_path, text):
        print("warning: could not write step summary", file=sys.stderr)


def _failure_report(
    error: Exception,
    inputs: dict[str, str],
    config: watcher_config.ResolvedConfig | None,
) -> None:
    """Render the failure path with the same audit layout as a healthy run."""
    settings = config.reporting if config else watcher_reporting.ReportingSettings()
    finding = watcher_reporting.assess_error(error)
    context = watcher_reporting.RunContext(
        command="discover",
        source=inputs.get("SOURCE", ""),
        tracker_path=inputs.get("TRACKER_PATH", ""),
        repository_config=config.repository_config if config else "",
        global_config=config.global_config if config else "",
        config_sources=config.sources if config else (),
        ignored_metadata_keys=config.ignored_metadata_keys if config else (),
        package_version=__version__,
    )
    report = watcher_reporting.RunReport(context=context)
    report.add(finding)
    report.verdict = "High — upstream discovery failed"

    ai_settings = config.ai if config else watcher_ai.AISettings()
    if not ai_settings.enable_ai_error_remediation:
        report.ai.status = "Disabled by configuration"
    else:
        provider = watcher_ai.detect_provider(ai_settings.ai_error_remediation_provider)
        if provider is None:
            report.ai.status = "Unavailable: no AI provider credential on this runner"
        else:
            advice = watcher_ai.recommend_error(
                finding.ai_payload(), provider, timeout=ai_settings.timeout_seconds
            )
            if advice is None:
                report.ai.status = f"Unavailable: {provider.name} returned no usable response"
            else:
                report.ai.status = f"Used ({provider.name})"
                report.ai.recommendation = advice.recommendation
                report.ai.rationale = advice.rationale
                report.ai.confidence = advice.confidence
                report.ai.source = f"AI ({provider.name})"

    emit_report(report, settings)


def main() -> int:
    inputs = {k: os.environ.get(k, "") for k in (*ENV_KEYS, *CONTROL_ENV_KEYS)}
    config: watcher_config.ResolvedConfig | None = None
    try:
        config = watcher_config.resolve(inputs)
        env = dict(config.env)
        outputs = run(env)
        outputs["metadata"] = json.dumps(
            watcher_metadata.package_metadata(__version__), separators=(",", ":")
        )
        report = build_report(env, outputs, config)
        apply_ai_digest(report, outputs, config)
        emit_report(report, config.reporting)
    except (watcher_config.ConfigError, watcher_ai.AIError, watcher_reporting.ReportError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        _failure_report(exc, inputs, config)
        return 1
    except WatcherExit as exc:
        _failure_report(exc, inputs, config)
        return 1

    write_outputs(outputs, multiline_keys=_MULTILINE_OUTPUTS)
    return 1 if watcher_reporting.should_fail(report, config.reporting) else 0


if __name__ == "__main__":
    sys.exit(main())
