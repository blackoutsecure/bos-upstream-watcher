# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## What this is

`bos-upstream-watcher` is a GitHub Marketplace composite action that answers one question:
has the upstream project I depend on shipped a new version since the last run? It resolves
the current version from one of seven providers, compares it against a committed tracker
JSON file, and emits `changed`, `version`, `tag`, `commit`, `update_type` and related
outputs. It is the poll half of the "poll upstream, rebuild downstream" pattern.

Consumers are other Blackout Secure repositories and any public Marketplace user. The main
in-org consumer is `bos-automation-hub`, whose reusable workflow
`.github/workflows/monitor-upstream-release.yml` pins this action at
`blackoutsecure/bos-upstream-watcher@1a336966520af363c9b76cdd34c0f1443b723179 # v1.1.3` and
wraps it with tracker-file commit and downstream `workflow_dispatch` behaviour. The hub also
distributes org-tier defaults at
`bos-automation-hub/sync-files/config/upstream-watcher-global-config.json`
(`tracker_path`, `strip_v_prefix`, `include_prereleases`, `user_agent`, `ai`). This repo
watches itself: `.github/tracked-release.json` is a live tracker file.

`action.yml` is `runs.using: composite` with one `bash` step that validates a few inputs and
execs `python3 "${GITHUB_ACTION_PATH}/src/discover.py"`. The Python is stdlib-only
(`urllib`, `json`, `re`, `difflib`), so it runs on any GitHub-hosted runner with no
`pip install`. `src/discover.py` declares `__version__ = "1.2.0"`. Ruff targets py310 at 100
columns; CI runs pytest on 3.10, 3.11, and 3.12. Dev deps are only `pytest>=9.1.1` and
`ruff>=0.16.4`. There is no build backend, lockfile, or installable package —
`test/conftest.py` prepends `src/` to `sys.path` so the modules import as top-level names.

## Commands

```bash
python3 -m pip install -r requirements-dev.txt

# Run against a real public upstream (network). GITHUB_OUTPUT must exist.
cd /Volumes/devbox/repos/blackoutsecure/bos-upstream-watcher
GITHUB_OUTPUT=/tmp/uw-out.txt GITHUB_STEP_SUMMARY=/tmp/uw-summary.md \
GITHUB_WORKSPACE="$PWD" SOURCE=pypi PACKAGE_NAME=requests \
TRACKER_PATH=/tmp/uw-tracker.json USE_MARKETPLACE_CONFIG=true ENABLE_AI=false \
python3 src/discover.py && cat /tmp/uw-out.txt

python3 -m pytest -q                                    # full suite (offline)
python3 -m pytest -q test/test_discover.py::TestSemverKey
python3 -m pytest -q test/test_watcher_config.py -k tracker
python3 -m ruff check .
```

## Validating changes

CI is a single required check. Pushes and pull requests run the hub's reusable
`bos-universal-security.yml` via `.github/workflows/bos-universal-gatekeeper-kicker.yml`,
the only workflow file here. `.github/bos-universal-config.json` enables
`enable_python_lint` (ruff + pytest, `python_version: "3.12"`), `enable_dependency_review`,
`enable_code_scan`, `enable_pr_title_check`, and `enable_readme_header_check` with the
`marketplace` profile; Node and shell linting are off. `action_test` adds a push-to-`dev`
smoke test invoking the action with `source: npm`, `package_name: "@actions/core"`,
`tracker_path: none` — the only CI step that hits the network.

Narrowest first locally: `pytest -k <name>` on the function you touched, then the full
suite, then `ruff check .`.

Every test in `test/` is offline. Provider tests `monkeypatch.setattr` over
`discover.http_request` / `discover.gh_api`, tracker tests install a stub into
`discover.PROVIDERS`, and `TestActionYaml` does string assertions against `action.yml` and
`.github/bos-universal-config.json`. Only the manual command above and the CI smoke test
reach the network.

Known pre-existing failures: `TestActionYaml::test_universal_marketplace_enforces_branch_contract`
and `::test_universal_kickers_use_promoted_runtime_and_config` raise `FileNotFoundError` for
`bos-universal-marketplace-kicker.yml` and `bos-universal-security-kicker.yml`, consolidated
into the single gatekeeper kicker without updating the tests. Baseline is 162 passed, 2
failed; do not blame your change for these. The system `python3` here is 3.9.6, below the
3.10 floor the code targets.

## Architecture

```text
action.yml                          Composite action: inputs, outputs, one bash step -> src/discover.py
src/discover.py                     Providers, SemVer ranking, tracker diff, outputs, report assembly, main()
src/watcher_config.py               Four-tier config cascade, validation, ResolvedConfig
src/watcher_ai.py                   Optional AI digest/remediation plus deterministic heuristics
src/watcher_metadata.py             Action identity; reserved keys stripped from every config tier
src/watcher_reporting.py            Findings, severities, job summary, annotations, should_fail
src/upstream-watcher-marketplace-config.json  Bundled tier-1 defaults shipped with the action
test/conftest.py                    Puts src/ on sys.path
test/test_discover.py               Provider, SemVer, output, tracker, action.yml contract tests
test/test_watcher_config.py         Cascade precedence, validation, tracker-disable tokens
test/test_watcher_ai.py             Provider detection, payload allowlist, heuristics
test/test_watcher_metadata.py       Reserved-key stripping
test/test_watcher_reporting.py      Report rendering and fail_on policy
pyproject.toml                      ruff + pytest config only
requirements-dev.txt                pytest, ruff
.github/bos-universal-config.json   Repo-owned gate/test/marketplace/watcher overrides
.github/tracked-release.json        This repo's own tracker file
```

Check flow:

1. **Config resolution** — `main()` reads `ENV_KEYS` + `CONTROL_ENV_KEYS` from the
   environment into `watcher_config.resolve()`. Four tiers deep-merge, later winning:
   bundled `src/upstream-watcher-marketplace-config.json`; the global config at
   `.github/blackout-secure-upstream-watcher-global-config.json` plus `global_config_json`;
   the repo config auto-discovered from `DEFAULT_CONFIG_PATHS`
   (`.github/bos-universal-config.json` first) plus `config_json`; then any non-empty input.
   Watcher keys live under an `upstream_watcher` section; a document with neither that key
   nor a companion section (`organization`, `security`, `marketplace`, `general`) is treated
   as the section itself. Every tier passes through `strip_package_metadata()`, so config
   can never rebrand the action. `_validate()` requires a known `source`, boolean
   `strip_v_prefix` / `include_prereleases`, and a repo-relative `tracker_path`.
2. **Dispatch** — `run()` looks `env["SOURCE"]` up in the `PROVIDERS` dict.
3. **Version discovery** — HTTP via `http_request()` (3 attempts, `2 * attempt` backoff,
   retry on 5xx and connection errors only; 4xx is terminal) or `gh_api()` (adds
   `Authorization: Bearer $GH_TOKEN` and SAML/PAT hints on 401/403). Candidates are filtered
   by `tag_pattern` (default `^v?\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$`) and ranked by
   `semver_key()`: non-SemVer sorts to `(-1,-1,-1,())`, a release outranks any pre-release
   of the same base, pre-release identifiers compare numeric before alphanumeric, build
   metadata is ignored. `_normalize_semver()` pads `X` and `X.Y` to `X.Y.Z`.
4. **Comparison** — the provider's `tracker` dict is serialised as
   `json.dumps(..., indent=2) + "\n"`. With a `TRACKER_PATH` the existing file is read,
   `previous_version` extracted, and text compared: equal is `changed=false`, different
   rewrites and prints a `difflib.unified_diff`. No tracker path means always
   `changed=true`. `update_type()` returns `major`, `minor`, `patch`, `prerelease`, `none`,
   or `unknown`.
5. **Outputs** — `build_report()` emits findings `UW-RES-001`, `UW-CHG-001`, `UW-TRK-001`,
   `UW-CFG-000`; `apply_ai_digest()` attaches advisory summary/impact with a deterministic
   fallback; `emit_report()` writes `$GITHUB_STEP_SUMMARY` and annotations;
   `write_outputs()` appends to `$GITHUB_OUTPUT`. Exit code comes from `should_fail()`
   against `organization.reporting.fail_on`.

Supported source types:

| `source`             | Required                                                | Optional                             | Endpoint                                                                                              | `commit`                              |
| -------------------- | ------------------------------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------- | ------------------------------------- |
| `github_release`     | `upstream_repo`                                         | `include_prereleases`, `tag_pattern` | `repos/{repo}/releases/latest`, or `repos/{repo}/releases` paginated 5x100 when `include_prereleases` | yes, via `repos/{repo}/commits/{tag}` |
| `github_branch_file` | `upstream_repo`, `upstream_branch`, `version_file_path` | `version_regex`                      | `raw.githubusercontent.com/{repo}/{branch}/{path}` plus `repos/{repo}/commits/{branch}`               | yes                                   |
| `github_tags`        | `upstream_repo`                                         | `tag_pattern`                        | `repos/{repo}/tags`, 5x100 pages                                                                      | yes                                   |
| `container_image`    | `image_ref`                                             | `tag_pattern`                        | Docker Hub v2, GHCR OCI Distribution (anonymous bearer token), or Quay v1                             | no                                    |
| `npm`                | `package_name`                                          | —                                    | `registry.npmjs.org/{pkg}/latest`                                                                     | no                                    |
| `pypi`               | `package_name`                                          | —                                    | `pypi.org/pypi/{pkg}/json`                                                                            | no                                    |
| `generic_url`        | `version_url`, `version_regex`                          | —                                    | the URL as given                                                                                      | no                                    |

`github_release` alone populates `release_url`, `release_name`, `release_body`,
`published_at`. `container_image` supports only `docker.io`, `ghcr.io`, `quay.io`; other
registry hosts and private GHCR/Quay repos die with an explicit message, not a bare 401.

Action contract: `runs.using: composite`, one `shell: bash` step with `set -euo pipefail`.
Every input reaches Python through the step's `env:` block — nothing is interpolated into
the `run:` body. The step rejects only explicitly supplied bad values (unknown `source`,
non-boolean `strip_v_prefix` / `include_prereleases`, absolute or `..` `tracker_path`),
because empty means "inherit from the cascade" and the resolver validates the merged result.
`github_token` defaults to `inputs.github_token || github.token` and is input-only, never
read from config. The 23 inputs and 17 outputs are declared in `action.yml`; the outputs are
`changed`, `version`, `tag`, `commit`, `source_url`, `tracker_path`, `label`, `release_url`,
`release_name`, `release_body`, `published_at`, `previous_version`, `update_type`,
`ai_summary`, `ai_impact`, `ai_status`, `metadata`. Only `release_body` and `ai_summary` are
in `_MULTILINE_OUTPUTS` and use heredoc syntax; every other output is rejected if it
contains a newline, and `write_outputs()` is the single source of truth for that check. An
empty `tracker_path` inherits; disabling requires a `TRACKER_DISABLE_TOKENS` value (`none`,
`off`, `false`, `disabled`).

Adding a source type: write `provider_<name>(env) -> dict` in `src/discover.py` returning at
least `tag`, `version`, `source_url`, `label`, `tracker`, reading inputs through
`_require()`; register it in `PROVIDERS` and `watcher_config.VALID_SOURCES`; for a new
setting add the key to `FIELD_TO_ENV` (and `BOOL_FIELDS` if boolean), add the input to
`action.yml` with `default: ''`, and wire the env var in the step's `env:` block and
`discover.ENV_KEYS`; for a new output add it to `_OUTPUT_KEYS_FROM_RESULT` and declare it in
`action.yml`; extend the `source` allowlist `case` in `action.yml` and the provider table in
`README.md`; add offline tests. A new container registry needs only one lister function and
one `_RegistryConfig` entry in `_REGISTRIES`.

## Conventions

Module docstrings state the contract, not the mechanics. Type hints everywhere with
`from __future__ import annotations`; config and policy objects are frozen dataclasses.
Fatal paths call `die()` with a lowercase message naming the offending input so the failure
report can quote it. Dispatch tables (`PROVIDERS`, `_REGISTRIES`, `FIELD_TO_ENV`,
`_OUTPUT_KEYS_FROM_RESULT`) are preferred over branching. Ruff enforces `E,F,W,I,B,UP,S,SIM`;
`test/**` is exempt from `S` and `B`; `urlopen` calls carry `# noqa: S310`. Comments justify
a non-obvious choice rather than narrating the code:

```python
    # `commits/<tag>` resolves both lightweight and annotated tags to the
    # underlying commit SHA in a single call. The Git Refs API would return
    # the tag-OBJECT SHA for annotated tags, which is NOT the commit.
    commit_info = gh_api(f"repos/{repo}/commits/{tag}")
```

## Blackout Secure conventions

These apply to every repository in the `blackoutsecure` organization.

### Branch model

- `dev` is the default branch and where all work lands.
- `main` is the promoted stable runtime that consumers reference through `@main`.
- Version tags (`vX.Y.Z` and a floating `vX`) point at promoted runtime commits.
- Promotion is driven from `bos-automation-hub` (`release-promote.yml`). Do not push
  directly to `main` and do not move tags by hand.

### Centrally managed files - do not hand-edit here

`blackoutsecure/bos-automation-hub` distributes these through
`bos-managed-file-sync-action`. Change the source under the hub's `sync-files/`, never the
copy in this repository:

- `LICENSE`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `SECURITY.md`, `SUPPORT.md`
- `.github/FUNDING.yml`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/ISSUE_TEMPLATE/`
- `.github/workflows/bos-universal-gatekeeper-kicker.yml`
- the `# >>> managed-file-sync:<service> >>> ... # <<< managed-file-sync:<service> <<<`
  delimited blocks inside `.editorconfig`, `.markdownlint.yaml`, `.shellcheckrc`,
  `.yamllint.yml`, `.gitignore`, and `README.md`

`.github/bos-universal-config.json` is repo-owned. It holds this repository's overrides on
top of the hub's global config and is the right place to change gate behaviour.

### CI gate

Pushes and pull requests run the hub's reusable `bos-universal-security.yml`, reported as a
single required check. It runs markdownlint, yamllint, shellcheck, and actionlint; ESLint,
Prettier, Ruff, pytest, and Bats where the repository has them; `bos-code-scanning-kit`
(secret scan, SAST, GHAS posture) and CodeQL; dependency review; and compliance checks for
the canonical README header and a conventional-commit PR title
(`feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert: subject`).

Every `uses:` reference in a workflow must be a commit SHA with a trailing version comment,
for example `actions/checkout@<sha> # v4.2.2`.

## Boundaries

### Always

- Keep `src/` stdlib-only; a third-party import breaks the no-`pip install` guarantee.
- Route new inputs through the composite step's `env:` block, never into the `run:` body.
- Add a new output to both `_OUTPUT_KEYS_FROM_RESULT` and `action.yml`, and decide explicitly
  whether it belongs in `_MULTILINE_OUTPUTS`.
- Keep tracker JSON byte-stable (2-space indent, insertion order, trailing newline); a
  formatting change fires a false `changed=true` for every consumer.
- Keep `README.md`, `action.yml` descriptions, and the hub's `monitor-upstream-release.yml`
  inputs aligned when the surface changes.
- Mock `discover.http_request` / `discover.gh_api` in tests; the suite stays offline.
- Keep AI advisory: it may never change an output, finding, severity, or exit code.

### Ask first

- Changing the four-tier precedence order, `DEFAULT_CONFIG_PATHS`, or
  `RESERVED_METADATA_KEYS` — the hub's global config depends on them.
- Changing the default `tracker_path`, `DEFAULT_TAG_PATTERN`, or `TRACKER_DISABLE_TOKENS`.
- Deleting or rewriting the `TestActionYaml` branch-contract tests that currently fail.
- Bumping `discover.__version__` or otherwise changing the published Marketplace surface.
- Broadening `container_image` to a new registry or to private GHCR/Quay images.
- Editing `marketplace.allowlist_paths` / `blocked_paths` / `required_paths` in
  `.github/bos-universal-config.json`.

### Never

- Hand-edit `.github/workflows/bos-universal-gatekeeper-kicker.yml`, push to `main`, or move
  a version tag.
- Read `github_token` from the config cascade, or let config override `watcher_metadata.py`.
- Send config documents, tracker contents, or credentials to an AI provider, or accept a
  plain-HTTP AI endpoint.
- Retry a 4xx in `http_request()`, or remove the newline validation in `write_outputs()`.
- Commit an unrelated `.github/tracked-release.json` delta; the pipeline writes it.
- Add a build backend, a lockfile, or a runtime dependency to `pyproject.toml`.
