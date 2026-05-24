# Blackout Secure Upstream Watcher

**Copyright © 2025-2026 Blackout Secure | Apache License 2.0**

[![Marketplace](https://img.shields.io/badge/GitHub%20Marketplace-blue?logo=github)](https://github.com/marketplace/actions/blackout-secure-upstream-watcher)
[![GitHub release](https://img.shields.io/github/v/release/blackoutsecure/bos-upstream-watcher?sort=semver)](https://github.com/blackoutsecure/bos-upstream-watcher/releases)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Made by BlackoutSecure](https://img.shields.io/badge/made%20by-BlackoutSecure-1f1f1f)](https://github.com/blackoutsecure)

Detect the latest version of an upstream project from a pluggable set of
sources and report whether it changed since the last run. Designed for the
"poll upstream, rebuild downstream" pattern that drives container rebuilds,
package re-publishes, and downstream release pipelines.

## ✨ Features

- **Seven providers**: GitHub Releases (incl. pre-releases), GitHub
  branch HEAD file, GitHub tags, container tags (docker.io, ghcr.io,
  quay.io — public images), npm, PyPI, and any generic URL with a
  regex.
- **Stdlib-only**: pure-Python implementation runs on the bundled
  `python3` of every GitHub-hosted runner. No `pip install`, no
  third-party dependencies.
- **Byte-stable tracker file**: deterministic JSON output (2-space
  indent, insertion-order keys, trailing newline) so diffs only appear
  when the upstream actually changes.
- **SemVer-aware ranking**: pre-release identifiers (`-rc1`, `-beta.2`)
  sort below their base release per the SemVer spec; non-SemVer tags
  are filtered out by a configurable regex.
- **GitHub API retry + clear errors**: 3-attempt linear backoff on 5xx
  / connection errors, with hints for SAML SSO and PAT scope failures.
- **Safe outputs**: resolved values are validated for newlines before
  being written to `GITHUB_OUTPUT`. All inputs flow through `env:`;
  nothing is interpolated into a `run:` body.

## 📋 Prerequisites

- A GitHub Actions runner with `python3` (every GitHub-hosted runner
  qualifies; self-hosted runners need `python3` on `PATH`).
- For private upstream repos: a token with `Contents: read` scope.

## 🚀 Quick start

```yaml
name: Watch upstream

on:
  schedule:
    - cron: '17 */6 * * *'   # every 6h
  workflow_dispatch:

permissions:
  contents: write   # to commit the tracker file

jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - id: discover
        uses: blackoutsecure/bos-upstream-watcher@v1
        with:
          source: github_release
          upstream_repo: nginx/nginx

      - name: Commit tracker if changed
        if: steps.discover.outputs.changed == 'true'
        run: |
          git config user.name  'github-actions[bot]'
          git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
          git add ${{ steps.discover.outputs.tracker_path }}
          git commit -m "chore: track nginx ${{ steps.discover.outputs.version }}"
          git push
```

## 📖 Provider examples

### 1. GitHub Releases (`github_release`)

Tracks `repos/{owner}/{name}/releases/latest`. Resolves both the tag
name and the underlying commit SHA (works for lightweight and annotated
tags).

```yaml
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: github_release
    upstream_repo: nginx/nginx
```

### 2. GitHub branch HEAD file (`github_branch_file`)

Reads a raw file from a branch HEAD. Use for rolling upstreams that
ship versions on a `dev` / `main` branch instead of cutting Releases
(e.g. `wiedehopf/readsb`). Optional `version_regex` extracts the
version from structured files like `debian/changelog`.

```yaml
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: github_branch_file
    upstream_repo: wiedehopf/readsb
    upstream_branch: dev
    version_file_path: version
```

With a regex extraction:

```yaml
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: github_branch_file
    upstream_repo: flightaware/dump978
    upstream_branch: master
    version_file_path: debian/changelog
    version_regex: '^[a-zA-Z0-9.-]+ \(([0-9.]+)'
```

### 3. GitHub tags (`github_tags`)

Lists `repos/{owner}/{name}/tags` and picks the highest SemVer match.

```yaml
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: github_tags
    upstream_repo: kubernetes/kubernetes
    tag_pattern: '^v\d+\.\d+\.\d+$'   # exclude alpha/beta/rc
```

### 4. Container registry (`container_image`)

Polls a container registry for the highest SemVer tag of an image.
Supports three registries; others are rejected with an explicit error:

* **Docker Hub** — `docker.io/<namespace>/<image>` (or bare
  `<namespace>/<image>`, or bare `<image>` for `library/*` officials)
* **GitHub Container Registry** — `ghcr.io/<owner>/<image>` (public
  images only; anonymous bearer token from `ghcr.io/token`)
* **Quay** — `quay.io/<namespace>/<image>` (public repos only)

```yaml
# Docker Hub
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: container_image
    image_ref: docker.io/library/nginx
```

```yaml
# GitHub Container Registry (public image)
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: container_image
    image_ref: ghcr.io/blackoutsecure/docker-github-runner
```

```yaml
# Quay (public repo)
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: container_image
    image_ref: quay.io/prometheus/node-exporter
```

> Private images on `ghcr.io` and `quay.io` are not yet supported — they
> require per-registry credential bootstrap. The action returns an
> explicit error pointing at this limitation when it hits a `401`/`403`.
> Other registries (`gcr.io`, `mcr.microsoft.com`, ECR, ACR) are
> rejected up-front; PRs adding them are welcome.

### 5. npm (`npm`)

Reads `registry.npmjs.org/{pkg}/latest`. Scoped names are supported.

```yaml
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: npm
    package_name: '@actions/core'
```

### 6. PyPI (`pypi`)

Reads `pypi.org/pypi/{pkg}/json`.

```yaml
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: pypi
    package_name: requests
```

### 7. Generic URL (`generic_url`)

Fetches an arbitrary URL and extracts a version via regex. Useful for
project websites and unstructured endpoints.

```yaml
- uses: blackoutsecure/bos-upstream-watcher@v1
  with:
    source: generic_url
    version_url: https://nginx.org/en/download.html
    version_regex: 'nginx-([0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz'
```

## ⚙️ Configuration

### Required inputs

| Input | Description |
|-------|-------------|
| `source` | Provider name (see [Provider examples](#-provider-examples)). |

### Provider-specific inputs

| Input | Required for | Description |
|-------|--------------|-------------|
| `upstream_repo` | `github_release`, `github_branch_file`, `github_tags` | Upstream as `owner/name`. |
| `upstream_branch` | `github_branch_file` | Branch name. |
| `version_file_path` | `github_branch_file` | Repo-relative path to the version file. Default `version`. |
| `image_ref` | `container_image` | `docker.io/<ns>/<image>`, `ghcr.io/<owner>/<image>`, or `quay.io/<ns>/<image>`. Bare names assume Docker Hub. |
| `package_name` | `npm`, `pypi` | Package name. npm scoped names allowed. |
| `version_url` | `generic_url` | URL to fetch. |
| `version_regex` | `generic_url` (required), `github_branch_file` (optional) | Python regex; first capture group is the version. |

### Common inputs

| Input | Default | Description |
|-------|---------|-------------|
| `tag_pattern` | `^v?\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$` | Filter applied to candidate tags before SemVer ranking. Used by `github_tags`, `container_image`, and `github_release` when `include_prereleases: true`. |
| `include_prereleases` | `'false'` | When `'true'`, `github_release` lists `repos/{repo}/releases` instead of `releases/latest` and picks the highest SemVer (including `-rc`/`-beta`). Required for upstreams that ship only pre-releases (e.g. `actions/runner`). Ignored for other providers. |
| `strip_v_prefix` | `true` | Strip a leading `v` from the resolved version. |
| `tracker_path` | `.github/upstream/tracked-release.json` | Where the tracker JSON is written. Empty disables the file. |
| `github_token` | `${{ github.token }}` | Token for authenticated GitHub REST calls. |
| `user_agent` | `bos-upstream-watcher/<version>` | Override the outbound `User-Agent` header. |

## 📤 Outputs

| Output | Description |
|--------|-------------|
| `changed` | `true` when the upstream version differs from the tracker file. |
| `version` | Resolved version (with `v` stripped when `strip_v_prefix: true`). |
| `tag` | Raw upstream tag name (preserves any leading `v`). |
| `commit` | Upstream commit SHA. Empty for `npm`, `pypi`, `container_image`, and `generic_url`. |
| `source_url` | URL that was consulted, for traceability. |
| `tracker_path` | Repo-relative path of the tracker file (echoes the input). |
| `label` | Canonical identifier for the upstream — `owner/name` for GitHub sources, package name for `npm`/`pypi`, `<registry>/<ns>/<image>` for `container_image`, or the URL for `generic_url`. Use this in commit messages and Slack notifications instead of a fallback chain across inputs. |
| `release_url` | `github_release` only. HTML URL of the GitHub Release. |
| `release_name` | `github_release` only. Release display name (may differ from the tag). |
| `release_body` | `github_release` only. Markdown release notes (multi-line; emitted via heredoc). |
| `published_at` | `github_release` only. ISO 8601 publication timestamp. |

The action also writes a compact summary to `$GITHUB_STEP_SUMMARY` when
that variable is set (it always is on GitHub-hosted runners) so the
resolved values show up directly on the workflow run page — no extra
`run:` step needed in the caller.

## 🗂️ Tracker file

When `tracker_path` is set (the default), the action writes a JSON file
whose shape depends on the provider. Example for `github_release`:

```json
{
  "repo": "nginx/nginx",
  "tag": "release-1.27.2",
  "version": "release-1.27.2",
  "commit": "8a2f7..."
}
```

Commit this file to your repo. The action diffs against the on-disk
copy on the next run; if the contents are byte-identical, `changed` is
`false` and the file is not rewritten. This keeps your git history free
of no-op commits.

Set `tracker_path: ''` to disable the file entirely — useful when the
caller does its own dispatch and does not need a persistent marker.
In that mode the action always reports `changed=true`.

## 🔐 Permissions

The action itself only needs **`contents: read`** (the default). Your
workflow needs **`contents: write`** if it commits the tracker file
back to the repository, as in the [quick-start example](#-quick-start).

For private upstream repos, pass a token with `Contents: read` scope
on the upstream:

```yaml
with:
  source: github_release
  upstream_repo: my-org/private-repo
  github_token: ${{ secrets.UPSTREAM_TOKEN }}
```

Fine-grained PATs and GitHub App installation tokens both work.

## 🧪 SemVer ranking details

Tags are sorted with strict SemVer ordering:

- `1.2.3` > `1.2.3-rc.1` > `1.2.3-beta` > `1.2.3-alpha`
- Within pre-release identifiers, numeric segments sort below
  alphanumeric (per the spec): `1.2.3-1` < `1.2.3-a`.
- Build metadata (`+sha.abc`) is ignored for ordering, also per the spec.
- Non-SemVer tags sort below all SemVer tags so they can never win.
- `github_release` does **not** rank — it trusts the upstream's
  `releases/latest` selection. Use `github_tags` when you need explicit
  SemVer ranking instead.

## 🐛 Debugging

The script logs to stdout for every run:

```text
First run for github_release 1.27.2 — wrote .github/upstream/tracked-release.json
```

When `changed=true` and a previous tracker file existed, a unified diff
is printed:

```text
Change detected (github_release):
--- .github/upstream/tracked-release.json
+++ .github/upstream/tracked-release.json.new
@@ -1,5 +1,5 @@
 {
   "repo": "nginx/nginx",
-  "tag": "release-1.27.1",
-  "version": "release-1.27.1",
-  "commit": "abc..."
+  "tag": "release-1.27.2",
+  "version": "release-1.27.2",
+  "commit": "def..."
 }
```

## ❓ Troubleshooting

### "GitHub API 403 — SAML enforcement"

The token is valid but has not been authorised for the organisation's
SAML SSO. Open https://github.com/settings/tokens, find the PAT,
click "Configure SSO", and authorise it for the target org.
Fine-grained PATs and GitHub App installation tokens work without
per-PAT SSO authorisation.

### "GitHub API 401/403 on `repos/.../commits/<tag>`"

The token does not have `Contents: read` on the upstream repo, or it
has expired. For a private upstream in a different org, supply
`github_token: ${{ secrets.UPSTREAM_TOKEN }}` where `UPSTREAM_TOKEN`
is a fine-grained PAT scoped to that repo.

### "Version `X.Y` is not SemVer-shaped"

Short SemVer (`X.Y`, `X`) is padded to `X.Y.Z` automatically. If you
hit this error, it means the upstream file contained something the
regex couldn't extract — pair `github_branch_file` with a
`version_regex` to pull just the version from a structured file.

### "no tags matched pattern"

The default `tag_pattern` requires strict `vX.Y.Z` shape. Set
`tag_pattern` to a more permissive regex if your upstream uses a
different convention (e.g. `release-X.Y.Z`).

### `changed=true` on every run

Either `tracker_path` is set to `''` (file disabled), or the previous
tracker file is being clobbered between runs (check that the file is
committed to the repo and that the workflow has `permissions:
contents: write`).

## ❓ FAQ

### How often should this run?

A `cron` of `*/6 * * *` (every 6 hours) is a reasonable starting point.
The action does at most 2 GitHub API calls per provider invocation,
which is well under any reasonable rate-limit budget.

### Does this dispatch downstream workflows?

No — by design. This action does **one job**: report whether the
upstream version changed. Compose it with `gh workflow run` or
[`peter-evans/repository-dispatch`](https://github.com/peter-evans/repository-dispatch)
in your own workflow to trigger downstream pipelines.

### Can I track multiple upstreams?

Yes. Either run the action multiple times in the same job (use
distinct `tracker_path` values for each) or run multiple jobs in
parallel. Each call is independent.

### Does it support GitHub Enterprise?

The action hits `api.github.com` directly. GHES support would require
parametrising the API base URL — open an issue if you need it.

### Why is `container_image` limited to public images?

Docker Hub's tags API works without auth for public repos. `ghcr.io`
and `quay.io` are supported for **public** images only — private
images on those registries require per-registry credential bootstrap
(`docker login` semantics) that is out of scope for v1.x. The action
returns an explicit error when it hits a `401`/`403` so the cause is
obvious. Other registries (`gcr.io`, `mcr.microsoft.com`, ECR, ACR)
are rejected up-front; PRs adding them are welcome.

### Why is `github_release` missing my latest tag?

GitHub's `releases/latest` endpoint hides pre-releases. If your
upstream ships only `-rc` / `-beta` releases (e.g. `actions/runner`,
betas, nightlies) the default selection will be stale or empty. Set
`include_prereleases: 'true'` to list `repos/{repo}/releases` and pick
the highest SemVer instead. Pair with `tag_pattern` to exclude release
lines you don't want.

## 🤝 Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## 📄 License

Copyright © 2025-2026 Blackout Secure

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).

## 💬 Support

- **Issues**: [GitHub Issues](https://github.com/blackoutsecure/bos-upstream-watcher/issues)
- **Security**: see the organization-wide [Security Policy](https://github.com/blackoutsecure/.github/blob/main/SECURITY.md) and report via [GitHub Security Advisories](https://github.com/blackoutsecure/bos-upstream-watcher/security/advisories/new)
- **Sponsor**: [GitHub Sponsors](https://github.com/sponsors/blackoutsecure)

## 🔗 Related

- [Blackout Secure Sitemap Generator](https://github.com/blackoutsecure/bos-sitemap-generator)
- [bos-automation-hub](https://github.com/blackoutsecure/bos-automation-hub)
  — reusable workflows that compose this action with downstream
  dispatch and tracker-file commit logic.

---

**Made with ❤️ by [Blackout Secure](https://github.com/blackoutsecure)**
