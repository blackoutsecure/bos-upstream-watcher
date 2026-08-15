"""Pure-function unit tests for `src/discover.py`. No network calls."""

import json
from pathlib import Path

import pytest

import discover
import watcher_config

# ---------------------------------------------------------------------------
# semver_key + pick_highest
# ---------------------------------------------------------------------------


class TestSemverKey:
    def test_release_outranks_prerelease(self):
        assert discover.semver_key("1.2.3") > discover.semver_key("1.2.3-rc.1")

    def test_patch_ordering(self):
        assert discover.semver_key("1.2.10") > discover.semver_key("1.2.9")

    def test_minor_ordering(self):
        assert discover.semver_key("1.10.0") > discover.semver_key("1.9.99")

    def test_v_prefix_tolerated(self):
        assert discover.semver_key("v1.2.3") == discover.semver_key("1.2.3")

    def test_prerelease_numeric_below_alpha(self):
        assert discover.semver_key("1.0.0-1") < discover.semver_key("1.0.0-a")

    def test_prerelease_segments(self):
        assert discover.semver_key("1.0.0-alpha.1") < discover.semver_key("1.0.0-alpha.2")
        assert discover.semver_key("1.0.0-alpha") < discover.semver_key("1.0.0-beta")

    def test_non_semver_sinks(self):
        non = discover.semver_key("garbage-tag")
        valid = discover.semver_key("0.0.1")
        assert non < valid

    def test_build_metadata_ignored(self):
        # SemVer spec: build metadata is ignored for ordering.
        assert discover.semver_key("1.2.3+sha.abc") == discover.semver_key("1.2.3+sha.def")


class TestPickHighest:
    def test_basic(self):
        tags = ["v1.2.3", "v1.2.10", "v2.0.0-rc.1", "v2.0.0", "garbage"]
        assert discover.pick_highest(tags, discover.DEFAULT_TAG_PATTERN) == "v2.0.0"

    def test_filter_excludes_pre_releases(self):
        tags = ["v1.0.0", "v1.0.1-rc.1", "v1.0.2-beta"]
        chosen = discover.pick_highest(tags, r"^v\d+\.\d+\.\d+$")
        assert chosen == "v1.0.0"

    def test_no_match_dies(self):
        with pytest.raises(SystemExit):
            discover.pick_highest(["abc", "def"], discover.DEFAULT_TAG_PATTERN)


# ---------------------------------------------------------------------------
# _normalize_semver + _strip_v
# ---------------------------------------------------------------------------


class TestNormalizeSemver:
    def test_pads_x_to_xyz(self):
        assert discover._normalize_semver("8") == "8.0.0"

    def test_pads_xy_to_xyz(self):
        assert discover._normalize_semver("8.1") == "8.1.0"

    def test_preserves_full(self):
        assert discover._normalize_semver("8.1.2") == "8.1.2"

    def test_preserves_suffix(self):
        assert discover._normalize_semver("8.1-rc.1") == "8.1.0-rc.1"
        assert discover._normalize_semver("8-beta") == "8.0.0-beta"

    def test_passthrough_for_non_semver(self):
        assert discover._normalize_semver("not-a-version") == "not-a-version"


class TestStripV:
    def test_strip_true_strips(self):
        assert discover._strip_v("v1.2.3", True) == "1.2.3"

    def test_strip_false_keeps(self):
        assert discover._strip_v("v1.2.3", False) == "v1.2.3"

    def test_no_v_no_change(self):
        assert discover._strip_v("1.2.3", True) == "1.2.3"


# ---------------------------------------------------------------------------
# Input validators
# ---------------------------------------------------------------------------


class TestValidateOwnerRepo:
    def test_accepts_simple(self):
        discover._validate_owner_repo("owner/repo")

    def test_accepts_dots_dashes_underscores(self):
        discover._validate_owner_repo("owner.name/repo-name_2")

    @pytest.mark.parametrize(
        "repo",
        [
            "no-slash",
            "/leading",
            "trailing/",
            "owner/repo/sub",
            ".bad/repo",
            "owner/repo$with$dollar",
        ],
    )
    def test_rejects_malformed(self, repo, monkeypatch):
        monkeypatch.setenv("SOURCE", "github_release")
        with pytest.raises(SystemExit):
            discover._validate_owner_repo(repo)


class TestRequire:
    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("SOURCE", "github_release")
        assert discover._require("X", "value") == "value"

    def test_dies_when_empty(self, monkeypatch):
        monkeypatch.setenv("SOURCE", "github_release")
        with pytest.raises(SystemExit):
            discover._require("X", "")


# ---------------------------------------------------------------------------
# write_outputs
# ---------------------------------------------------------------------------


class TestWriteOutputs:
    def test_writes_kv_lines(self, tmp_path, monkeypatch):
        out = tmp_path / "gh-out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        discover.write_outputs({"changed": "true", "version": "1.2.3"})
        lines = out.read_text(encoding="utf-8").splitlines()
        assert "changed=true" in lines
        assert "version=1.2.3" in lines

    def test_rejects_newline_values(self, tmp_path, monkeypatch):
        out = tmp_path / "gh-out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        with pytest.raises(SystemExit):
            discover.write_outputs({"v": "line1\nline2"})

    def test_dies_without_github_output(self, monkeypatch):
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        with pytest.raises(SystemExit):
            discover.write_outputs({"k": "v"})


# ---------------------------------------------------------------------------
# user_agent override
# ---------------------------------------------------------------------------


class TestUserAgent:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("USER_AGENT_OVERRIDE", raising=False)
        ua = discover._user_agent()
        assert ua.startswith("bos-upstream-watcher/")

    def test_override(self, monkeypatch):
        monkeypatch.setenv("USER_AGENT_OVERRIDE", "my-bot/0.1")
        assert discover._user_agent() == "my-bot/0.1"


# ---------------------------------------------------------------------------
# Tracker file behaviour (run() with a stubbed provider)
# ---------------------------------------------------------------------------


def _install_stub_provider(monkeypatch, version: str = "1.2.3"):
    """Replace PROVIDERS['github_release'] with a deterministic stub so we can
    exercise the tracker-file branch of run() without touching the network."""

    def stub(_env):
        tracker = {
            "repo": "stub/repo",
            "tag": f"v{version}",
            "version": version,
            "commit": "deadbeef" * 5,
        }
        return {
            "tag": f"v{version}",
            "version": version,
            "commit": tracker["commit"],
            "source_url": f"https://example.test/{version}",
            "label": "stub/repo",
            "release_url": f"https://example.test/{version}",
            "release_name": f"v{version}",
            "release_body": "## stub release notes\n\n- did a thing\n- did another",
            "published_at": "2024-09-12T18:23:00Z",
            "tracker": tracker,
        }

    monkeypatch.setitem(discover.PROVIDERS, "github_release", stub)


def _env(tracker_path: str) -> dict[str, str]:
    return {
        "SOURCE": "github_release",
        "UPSTREAM_REPO": "stub/repo",
        "UPSTREAM_BRANCH": "",
        "VERSION_FILE_PATH": "",
        "IMAGE_REF": "",
        "PACKAGE_NAME": "",
        "VERSION_URL": "",
        "VERSION_REGEX": "",
        "TAG_PATTERN": "",
        "STRIP_V_PREFIX": "true",
        "TRACKER_PATH": tracker_path,
        "INCLUDE_PRERELEASES": "false",
    }


def _main_env(tracker_path: str) -> dict[str, str]:
    """Inputs for `main()`: AI off so no test ever reaches a provider."""
    return {**_env(tracker_path), "ENABLE_AI": "false"}


class TestRunTrackerFile:
    def test_first_run_writes_file_and_reports_changed(self, tmp_path, monkeypatch):
        _install_stub_provider(monkeypatch, "1.2.3")
        tracker = tmp_path / ".github/upstream/tracked.json"
        out = discover.run(_env(str(tracker)))
        assert out["changed"] == "true"
        assert tracker.exists()
        # Byte-stable formatting: 2-space indent + trailing newline.
        body = tracker.read_text(encoding="utf-8")
        assert body.endswith("\n")
        data = json.loads(body)
        assert data["version"] == "1.2.3"

    def test_unchanged_run_reports_changed_false(self, tmp_path, monkeypatch):
        _install_stub_provider(monkeypatch, "1.2.3")
        tracker = tmp_path / "tracked.json"

        out1 = discover.run(_env(str(tracker)))
        assert out1["changed"] == "true"

        out2 = discover.run(_env(str(tracker)))
        assert out2["changed"] == "false"

    def test_version_change_rewrites_file(self, tmp_path, monkeypatch):
        _install_stub_provider(monkeypatch, "1.2.3")
        tracker = tmp_path / "tracked.json"
        discover.run(_env(str(tracker)))

        _install_stub_provider(monkeypatch, "1.2.4")
        out = discover.run(_env(str(tracker)))
        assert out["changed"] == "true"
        assert "1.2.4" in tracker.read_text(encoding="utf-8")

    def test_empty_tracker_path_always_changed(self, tmp_path, monkeypatch):
        _install_stub_provider(monkeypatch, "1.2.3")
        out = discover.run(_env(""))
        assert out["changed"] == "true"
        assert out["tracker_path"] == ""


# ---------------------------------------------------------------------------
# main() smoke test (writes to GITHUB_OUTPUT)
# ---------------------------------------------------------------------------


class TestMain:
    def test_writes_outputs(self, tmp_path, monkeypatch):
        _install_stub_provider(monkeypatch, "9.9.9")
        out_file = tmp_path / "gh-out"
        tracker = tmp_path / "tracker.json"

        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        for k, v in _main_env(str(tracker)).items():
            monkeypatch.setenv(k, v)

        rc = discover.main()
        assert rc == 0

        content = out_file.read_text(encoding="utf-8")
        assert "changed=true" in content
        assert "version=9.9.9" in content
        assert "tag=v9.9.9" in content
        assert "update_type=unknown" in content
        assert "ai_status=" in content
        assert '"name":"bos-upstream-watcher"' in content


# ---------------------------------------------------------------------------
# Smoke: action.yml structural sanity (no external YAML parser required)
# ---------------------------------------------------------------------------


class TestActionYaml:
    def test_action_yaml_present(self):
        root = Path(__file__).resolve().parent.parent
        action = root / "action.yml"
        assert action.exists()
        body = action.read_text(encoding="utf-8")
        assert "branding:" in body
        assert "name: 'Blackout Secure Upstream Watcher'" in body
        assert "using: composite" in body
        assert 'python3 "${GITHUB_ACTION_PATH}/src/discover.py"' in body

    def test_v1_1_new_inputs_and_outputs_wired(self):
        """v1.1.0 surface contract: include_prereleases input + 5 new outputs
        must be declared in action.yml so the marketplace UI shows them and
        downstream callers can reference them by name."""
        root = Path(__file__).resolve().parent.parent
        body = (root / "action.yml").read_text(encoding="utf-8")
        # New input
        assert "include_prereleases:" in body
        assert "INCLUDE_PRERELEASES: ${{ inputs.include_prereleases }}" in body
        # New outputs
        for out in ("label", "release_url", "release_name", "release_body", "published_at"):
            assert f"{out}:" in body, f"missing output '{out}' in action.yml"
            assert f"steps.discover.outputs.{out}" in body, f"output '{out}' not wired"

    def test_universal_marketplace_enforces_branch_contract(self):
        root = Path(__file__).resolve().parent.parent
        workflow_dir = root / ".github/workflows"
        marketplace_body = (workflow_dir / "bos-universal-marketplace-kicker.yml").read_text(
            encoding="utf-8"
        )
        assert "branches: [main, dev]" in marketplace_body
        assert "pull_request_target:" in marketplace_body
        assert "branches: [main]" in marketplace_body
        assert "options: [validate, name-check, release, metadata]" in marketplace_body
        # Kicker dispatches to the hub ref matching the branch under test.
        assert "bos-universal-marketplace.yml@main" in marketplace_body
        assert "bos-universal-marketplace.yml@dev" in marketplace_body
        # Guard / promote / metadata always run from the stable hub ref.
        assert "marketplace-repo-guard.yml@main" in marketplace_body
        assert "release-promote.yml@main" in marketplace_body
        assert "repo-metadata-sync.yml@main" in marketplace_body
        assert "shared/universal-config@main" in marketplace_body
        assert "shared/universal-config@dev" not in marketplace_body
        assert "release-promote.yml@dev" not in marketplace_body
        assert "github.event.repository.default_branch" in marketplace_body
        assert "cfg: ${{ steps.config.outputs.cfg }}" in marketplace_body
        assert "marketplace.source_branch || github.event.repository.default_branch" in marketplace_body
        assert "marketplace.target_branch || 'main'" in marketplace_body

        config = json.loads(
            (root / ".github/bos-universal-config.json").read_text(encoding="utf-8")
        )
        marketplace = config["marketplace"]
        assert marketplace["enabled"] is True
        assert marketplace["source_branch"] == "dev"
        assert marketplace["target_branch"] == "main"
        assert marketplace["allowlist_paths"] == [
            "action.yml",
            "src",
            "README.md",
            "LICENSE",
            "NOTICE",
        ]
        # Everything that must never reach the curated Marketplace branch.
        for blocked in (
            ".github/workflows/",
            ".github/bos-universal-config.json",
            "pyproject.toml",
            "requirements-dev.txt",
            "test/",
        ):
            assert blocked in marketplace["blocked_paths"]
        assert marketplace["required_paths"] == [
            ".github/dependabot.yml",
            "action.yml",
            "src",
            "LICENSE",
            "NOTICE",
            "README.md",
        ]
        assert marketplace["include_dependabot_config"] is True
        assert marketplace["include_github_metadata"] is False
        # Metadata sync needs an admin PAT; ship the settings disabled.
        assert marketplace["repo_metadata"]["enable"] is False

        assert not (workflow_dir / "lint.yml").exists()
        for retired_name in ("marketplace-ci.yml", "marketplace-guard.yml", "release.yml"):
            assert not (workflow_dir / retired_name).exists()

    def test_universal_kickers_use_promoted_runtime_and_config(self):
        root = Path(__file__).resolve().parent.parent
        workflow_dir = root / ".github/workflows"
        security_body = (workflow_dir / "bos-universal-security-kicker.yml").read_text(
            encoding="utf-8"
        )
        assert "bos-universal-security.yml@main" in security_body
        assert "bos-universal-security.yml@dev" in security_body
        assert "config_authoritative: true" in security_body
        assert "secrets: inherit" in security_body

        sync_body = (workflow_dir / "bos-universal-sync-kicker.yml").read_text(
            encoding="utf-8"
        )
        assert "bos-universal-sync.yml@main" in sync_body
        assert "bos-universal-sync.yml@dev" in sync_body
        assert "'.github/bos-universal-config.json'" in sync_body
        assert "github.event.repository.default_branch" in sync_body
        assert "mode: ${{ inputs.mode || '' }}" in sync_body

        action_test_body = (workflow_dir / "bos-universal-action-test-kicker.yml").read_text(
            encoding="utf-8"
        )
        assert "bos-universal-action-test.yml@main" in action_test_body
        assert "bos-universal-action-test.yml@dev" in action_test_body
        # The hub action-test surface replaces the hand-rolled matrix workflow.
        assert not (workflow_dir / "test.yml").exists()

        marketplace_body = (workflow_dir / "bos-universal-marketplace-kicker.yml").read_text(
            encoding="utf-8"
        )
        for body in (security_body, sync_body, marketplace_body, action_test_body):
            assert body.startswith("# Managed by Blackout Secure Managed File Sync")

        assert not (root / "bos-launchpad-config.json").exists()
        assert not (root / "bos-universal-config.json").exists()
        config = json.loads(
            (root / ".github/bos-universal-config.json").read_text(encoding="utf-8")
        )
        assert config["security"]["enable_python_lint"] is True
        assert config["security"]["python_version"] == "3.12"
        assert config["security"]["enable_shell_lint"] is False
        assert config["security"]["readme_header_profile"] == "marketplace"
        assert config["organization"]["reporting"]["enable_job_summary"] is True
        assert config["organization"]["reporting"]["fail_on"] == "fail"
        assert config["action_test"]["python_versions"] == ["3.10", "3.11", "3.12"]
        assert config["action_test"]["os_matrix"] == [
            "ubuntu-latest",
            "macos-latest",
            "windows-latest",
        ]
        assert config["action_test"]["enable_smoke_test"] is True
        assert config["action_test"]["smoke_test_config"]["source"] == "npm"
        # `''` now inherits the configured tracker path, so the smoke test has
        # to disable the tracker explicitly.
        assert config["action_test"]["smoke_test_config"]["tracker_path"] == "none"

    def test_managed_file_sync_uses_known_service_names(self):
        """Every selected service must exist in the published sync catalog.

        The engine hard-fails with a config error when a name has no service
        definition, so an unknown name here breaks the whole sync run.
        """
        root = Path(__file__).resolve().parent.parent
        config = json.loads(
            (root / ".github/bos-universal-config.json").read_text(encoding="utf-8")
        )
        sync = config["managed_file_sync"]
        known = {
            "baseline",
            "quality_baseline",
            "common",
            "editorconfig",
            "lf_line_endings",
            "markdownlint",
            "dependabot_actions",
            "dependabot_pip",
            "prettier",
            "shellcheck",
            "bos_universal_security_kicker",
            "bos_universal_sync_kicker",
            "bos_universal_marketplace_kicker",
            "bos_universal_action_test_kicker",
        }
        assert set(sync["services"]) <= known
        assert set(sync["exclude_services"]) <= known
        assert set(sync["service_definitions"]) <= known
        assert sync["mode"] == "commit"
        # The dev/main split needs Dependabot PRs to land on dev.
        actions_lines = sync["service_definitions"]["dependabot_actions"]["files"][0][
            "content_lines"
        ]
        assert "    target-branch: dev" in actions_lines


# ---------------------------------------------------------------------------
# v1.1.0 — multi-line output support (release_body uses heredoc)
# ---------------------------------------------------------------------------


class TestWriteOutputsMultiline:
    def test_multiline_value_emits_heredoc(self, tmp_path, monkeypatch):
        out = tmp_path / "gh-out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        body = "line1\nline2\nline3"
        discover.write_outputs(
            {"release_body": body}, multiline_keys=frozenset({"release_body"})
        )
        content = out.read_text(encoding="utf-8")
        # Heredoc format: key<<DELIM\nvalue\nDELIM\n
        assert content.startswith("release_body<<BOS_UPSTREAM_EOF\n")
        assert "\nline1\nline2\nline3\n" in content
        assert content.rstrip().endswith("BOS_UPSTREAM_EOF")

    def test_multiline_picks_unique_delimiter(self, tmp_path, monkeypatch):
        """If the value itself contains the default delimiter, write_outputs
        must pick a longer one to avoid heredoc termination collision."""
        out = tmp_path / "gh-out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        body = "innocuous text\nBOS_UPSTREAM_EOF\nmore text"
        discover.write_outputs(
            {"release_body": body}, multiline_keys=frozenset({"release_body"})
        )
        content = out.read_text(encoding="utf-8")
        # The picked delimiter must NOT be the bare default (else heredoc breaks).
        first_line = content.split("\n", 1)[0]
        assert first_line.startswith("release_body<<")
        delim = first_line[len("release_body<<") :]
        assert delim != "BOS_UPSTREAM_EOF"
        assert delim.startswith("BOS_UPSTREAM_EOF_X")
        # Round-trip: the delimiter must not appear in the body.
        assert delim not in body

    def test_single_line_unchanged_when_key_not_multiline(self, tmp_path, monkeypatch):
        out = tmp_path / "gh-out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        discover.write_outputs(
            {"version": "1.2.3", "release_body": "single line ok too"},
            multiline_keys=frozenset({"release_body"}),
        )
        lines = out.read_text(encoding="utf-8").splitlines()
        assert "version=1.2.3" in lines
        # `release_body` value has no newlines, but it IS in multiline_keys,
        # so heredoc is used anyway. Verify both encodings coexist.
        assert any(line.startswith("release_body<<") for line in lines)


# ---------------------------------------------------------------------------
# v1.1.0 — RFC 5988 Link header parser (OCI registry pagination)
# ---------------------------------------------------------------------------


class TestParseLinkNext:
    def test_returns_none_for_empty(self):
        assert discover._parse_link_next("") is None
        assert discover._parse_link_next("") is None

    def test_extracts_next_url(self):
        h = '</v2/owner/img/tags/list?last=v1.0.0&n=100>; rel="next"'
        assert discover._parse_link_next(h) == "/v2/owner/img/tags/list?last=v1.0.0&n=100"

    def test_handles_multiple_rels(self):
        h = (
            '</v2/owner/img/tags/list?last=foo>; rel="next", '
            '</v2/owner/img/tags/list>; rel="first"'
        )
        assert discover._parse_link_next(h) == "/v2/owner/img/tags/list?last=foo"

    def test_handles_rel_without_quotes(self):
        h = "</foo>; rel=next"
        assert discover._parse_link_next(h) == "/foo"

    def test_returns_none_when_no_next(self):
        h = '</v2/foo>; rel="last"'
        assert discover._parse_link_next(h) is None


# ---------------------------------------------------------------------------
# v1.1.0 — container_image registry routing
# ---------------------------------------------------------------------------


class TestParseImageRef:
    def test_bare_name_is_dockerhub(self):
        assert discover._parse_image_ref("nginx") == ("docker.io", "nginx")

    def test_ns_slash_name_is_dockerhub(self):
        assert discover._parse_image_ref("library/nginx") == ("docker.io", "library/nginx")

    def test_explicit_dockerhub(self):
        assert discover._parse_image_ref("docker.io/library/nginx") == (
            "docker.io",
            "library/nginx",
        )

    def test_ghcr_prefix(self):
        assert discover._parse_image_ref("ghcr.io/blackoutsecure/runner") == (
            "ghcr.io",
            "blackoutsecure/runner",
        )

    def test_quay_prefix(self):
        assert discover._parse_image_ref("quay.io/prometheus/node-exporter") == (
            "quay.io",
            "prometheus/node-exporter",
        )

    @pytest.mark.parametrize(
        "bad_ref",
        [
            "gcr.io/foo/bar",
            "mcr.microsoft.com/foo/bar",
            "registry.example.com/foo/bar",
            "myregistry:5000/foo/bar",
        ],
    )
    def test_unsupported_registry_rejected(self, bad_ref):
        with pytest.raises(SystemExit):
            discover._parse_image_ref(bad_ref)


class TestSplitTwoSegments:
    def test_valid(self):
        assert discover._split_two_segments("owner/img", "ghcr.io", "ghcr.io/owner/img") == (
            "owner",
            "img",
        )

    @pytest.mark.parametrize(
        "bad_path",
        [
            "no-slash",
            "too/many/segments",
            "/leading",
            "trailing/",
        ],
    )
    def test_invalid(self, bad_path):
        with pytest.raises(SystemExit):
            discover._split_two_segments(bad_path, "ghcr.io", f"ghcr.io/{bad_path}")


# ---------------------------------------------------------------------------
# v1.1.0 — GHCR + Quay tag listing (mocked HTTP)
# ---------------------------------------------------------------------------


def _container_env(image_ref: str, tag_pattern: str = "") -> dict[str, str]:
    return {
        "SOURCE": "container_image",
        "UPSTREAM_REPO": "",
        "UPSTREAM_BRANCH": "",
        "VERSION_FILE_PATH": "",
        "IMAGE_REF": image_ref,
        "PACKAGE_NAME": "",
        "VERSION_URL": "",
        "VERSION_REGEX": "",
        "TAG_PATTERN": tag_pattern,
        "STRIP_V_PREFIX": "true",
        "TRACKER_PATH": "",
        "INCLUDE_PRERELEASES": "false",
    }


class TestGhcrProvider:
    def test_picks_highest_semver(self, monkeypatch):
        calls: list[str] = []

        def fake_http_request(url, *, headers=None, accept_json=False):
            calls.append(url)
            if "ghcr.io/token" in url:
                return 200, b'{"token": "anon"}', {}
            assert headers and headers.get("Authorization") == "Bearer anon"
            body = b'{"name": "owner/img", "tags": ["v1.0.0", "v1.2.3", "v1.2.10", "garbage"]}'
            return 200, body, {}

        monkeypatch.setattr(discover, "http_request", fake_http_request)

        result = discover.provider_container_image(
            _container_env("ghcr.io/owner/img")
        )
        assert result["tag"] == "v1.2.10"
        assert result["version"] == "1.2.10"
        assert result["label"] == "ghcr.io/owner/img"
        assert result["tracker"]["image"] == "ghcr.io/owner/img"
        assert "github.com/owner/img/pkgs/container/img" in result["source_url"]

    def test_pagination_follows_link_header(self, monkeypatch):
        page = {"count": 0}

        def fake_http_request(url, *, headers=None, accept_json=False):
            if "ghcr.io/token" in url:
                return 200, b'{"token": "anon"}', {}
            page["count"] += 1
            if page["count"] == 1:
                return (
                    200,
                    b'{"tags": ["v1.0.0", "v1.1.0"]}',
                    {"link": '</v2/owner/img/tags/list?last=v1.1.0&n=100>; rel="next"'},
                )
            return 200, b'{"tags": ["v2.0.0"]}', {}

        monkeypatch.setattr(discover, "http_request", fake_http_request)

        result = discover.provider_container_image(
            _container_env("ghcr.io/owner/img")
        )
        # Highest across BOTH pages wins.
        assert result["tag"] == "v2.0.0"
        assert page["count"] == 2

    def test_private_image_401_dies_with_hint(self, monkeypatch):
        def fake_http_request(url, *, headers=None, accept_json=False):
            if "ghcr.io/token" in url:
                return 200, b'{"token": "anon"}', {}
            return 401, b'{"errors":[{"code":"UNAUTHORIZED"}]}', {}

        monkeypatch.setattr(discover, "http_request", fake_http_request)

        with pytest.raises(SystemExit):
            discover.provider_container_image(_container_env("ghcr.io/owner/private"))


class TestQuayProvider:
    def test_picks_highest_semver(self, monkeypatch):
        def fake_http_request(url, *, headers=None, accept_json=False):
            assert "quay.io/api/v1/repository/prometheus/node-exporter/tag/" in url
            body = (
                b'{"tags": ['
                b'{"name": "v1.5.0"}, {"name": "v1.6.0"}, {"name": "v1.6.1"}, '
                b'{"name": "latest"}'
                b'], "has_additional": false}'
            )
            return 200, body, {}

        monkeypatch.setattr(discover, "http_request", fake_http_request)

        result = discover.provider_container_image(
            _container_env("quay.io/prometheus/node-exporter")
        )
        assert result["tag"] == "v1.6.1"
        assert result["version"] == "1.6.1"
        assert result["label"] == "quay.io/prometheus/node-exporter"
        assert "quay.io/repository/prometheus/node-exporter" in result["source_url"]

    def test_pagination_follows_has_additional(self, monkeypatch):
        page = {"count": 0}

        def fake_http_request(url, *, headers=None, accept_json=False):
            page["count"] += 1
            if page["count"] == 1:
                return (
                    200,
                    b'{"tags": [{"name": "v1.0.0"}], "has_additional": true}',
                    {},
                )
            return 200, b'{"tags": [{"name": "v2.0.0"}], "has_additional": false}', {}

        monkeypatch.setattr(discover, "http_request", fake_http_request)
        result = discover.provider_container_image(_container_env("quay.io/ns/img"))
        assert result["tag"] == "v2.0.0"
        assert page["count"] == 2

    def test_404_dies(self, monkeypatch):
        def fake_http_request(url, *, headers=None, accept_json=False):
            return 404, b'{"error":"not found"}', {}

        monkeypatch.setattr(discover, "http_request", fake_http_request)
        with pytest.raises(SystemExit):
            discover.provider_container_image(_container_env("quay.io/ns/missing"))


class TestDockerHubStillWorks:
    """Docker Hub path (the only registry supported in v1.0.0) still works
    after the v1.1.0 multi-registry refactor."""

    def test_canonical_dockerhub(self, monkeypatch):
        def fake_http_request(url, *, headers=None, accept_json=False):
            assert "hub.docker.com/v2/repositories/library/nginx" in url
            return 200, b'{"results": [{"name": "1.27.1"}, {"name": "1.27.2"}], "next": null}', {}

        monkeypatch.setattr(discover, "http_request", fake_http_request)
        result = discover.provider_container_image(_container_env("docker.io/library/nginx"))
        assert result["tag"] == "1.27.2"
        assert result["label"] == "docker.io/library/nginx"

    def test_bare_image_assumed_dockerhub_library(self, monkeypatch):
        def fake_http_request(url, *, headers=None, accept_json=False):
            assert "hub.docker.com/v2/repositories/library/redis" in url
            return 200, b'{"results": [{"name": "7.4.0"}], "next": null}', {}

        monkeypatch.setattr(discover, "http_request", fake_http_request)
        result = discover.provider_container_image(_container_env("redis"))
        assert result["tag"] == "7.4.0"
        assert result["label"] == "docker.io/library/redis"


# ---------------------------------------------------------------------------
# v1.1.0 — github_release with include_prereleases
# ---------------------------------------------------------------------------


def _release_env(include_prereleases: bool, tag_pattern: str = "") -> dict[str, str]:
    return {
        "SOURCE": "github_release",
        "UPSTREAM_REPO": "owner/repo",
        "UPSTREAM_BRANCH": "",
        "VERSION_FILE_PATH": "",
        "IMAGE_REF": "",
        "PACKAGE_NAME": "",
        "VERSION_URL": "",
        "VERSION_REGEX": "",
        "TAG_PATTERN": tag_pattern,
        "STRIP_V_PREFIX": "true",
        "TRACKER_PATH": "",
        "INCLUDE_PRERELEASES": "true" if include_prereleases else "false",
    }


class TestGithubReleaseExtraOutputs:
    def test_release_url_name_body_published_at(self, monkeypatch):
        def fake_gh_api(path):
            if path.startswith("repos/owner/repo/releases/latest"):
                return {
                    "tag_name": "v1.2.3",
                    "name": "v1.2.3 — Stable release",
                    "body": "## What's new\n\n- Big change\n- Another change",
                    "published_at": "2024-09-12T18:23:00Z",
                    "html_url": "https://github.com/owner/repo/releases/tag/v1.2.3",
                }
            if path.startswith("repos/owner/repo/commits/"):
                return {"sha": "a" * 40}
            raise AssertionError(f"unexpected gh_api call: {path}")

        monkeypatch.setattr(discover, "gh_api", fake_gh_api)
        result = discover.provider_github_release(_release_env(include_prereleases=False))
        assert result["tag"] == "v1.2.3"
        assert result["version"] == "1.2.3"
        assert result["label"] == "owner/repo"
        assert result["release_url"] == "https://github.com/owner/repo/releases/tag/v1.2.3"
        assert result["release_name"] == "v1.2.3 — Stable release"
        assert "Big change" in result["release_body"]
        assert "\n" in result["release_body"]  # multi-line preserved
        assert result["published_at"] == "2024-09-12T18:23:00Z"


class TestIncludePrereleases:
    def test_picks_highest_including_prereleases(self, monkeypatch):
        def fake_gh_api(path):
            if path.startswith("repos/owner/repo/releases?"):
                return [
                    {"tag_name": "v1.0.0", "draft": False},
                    {"tag_name": "v2.0.0-beta.1", "draft": False},
                    {"tag_name": "v2.0.0-rc.1", "draft": False},
                    {"tag_name": "garbage-tag", "draft": False},
                ]
            if path.startswith("repos/owner/repo/commits/"):
                return {"sha": "b" * 40}
            raise AssertionError(f"unexpected gh_api call: {path}")

        monkeypatch.setattr(discover, "gh_api", fake_gh_api)
        result = discover.provider_github_release(_release_env(include_prereleases=True))
        # SemVer: v2.0.0-rc.1 > v2.0.0-beta.1 > v1.0.0
        assert result["tag"] == "v2.0.0-rc.1"
        assert result["version"] == "2.0.0-rc.1"

    def test_drafts_skipped(self, monkeypatch):
        def fake_gh_api(path):
            if path.startswith("repos/owner/repo/releases?"):
                return [
                    {"tag_name": "v1.0.0", "draft": False},
                    {"tag_name": "v9.9.9", "draft": True},  # would win if included
                ]
            if path.startswith("repos/owner/repo/commits/"):
                return {"sha": "c" * 40}
            raise AssertionError(f"unexpected gh_api call: {path}")

        monkeypatch.setattr(discover, "gh_api", fake_gh_api)
        result = discover.provider_github_release(_release_env(include_prereleases=True))
        assert result["tag"] == "v1.0.0"

    def test_no_matches_dies(self, monkeypatch):
        def fake_gh_api(path):
            if path.startswith("repos/owner/repo/releases?"):
                return [
                    {"tag_name": "weird-tag-format", "draft": False},
                ]
            raise AssertionError(f"unexpected gh_api call: {path}")

        monkeypatch.setattr(discover, "gh_api", fake_gh_api)
        with pytest.raises(SystemExit):
            discover.provider_github_release(_release_env(include_prereleases=True))

    def test_empty_release_list_dies(self, monkeypatch):
        def fake_gh_api(path):
            if path.startswith("repos/owner/repo/releases?"):
                return []
            raise AssertionError(f"unexpected gh_api call: {path}")

        monkeypatch.setattr(discover, "gh_api", fake_gh_api)
        with pytest.raises(SystemExit):
            discover.provider_github_release(_release_env(include_prereleases=True))

    def test_tag_pattern_filters(self, monkeypatch):
        """With tag_pattern restricting to non-pre-release, the highest
        full release wins even when later pre-releases exist."""

        def fake_gh_api(path):
            if path.startswith("repos/owner/repo/releases?"):
                return [
                    {"tag_name": "v1.0.0", "draft": False},
                    {"tag_name": "v2.0.0-rc.1", "draft": False},
                    {"tag_name": "v1.5.0", "draft": False},
                ]
            if path.startswith("repos/owner/repo/commits/"):
                return {"sha": "d" * 40}
            raise AssertionError(f"unexpected gh_api call: {path}")

        monkeypatch.setattr(discover, "gh_api", fake_gh_api)
        result = discover.provider_github_release(
            _release_env(include_prereleases=True, tag_pattern=r"^v\d+\.\d+\.\d+$")
        )
        assert result["tag"] == "v1.5.0"


# ---------------------------------------------------------------------------
# v1.1.0 — Step summary
# ---------------------------------------------------------------------------


class TestStepSummary:
    def _report(self, monkeypatch, tracker_path=""):
        env = _env(tracker_path)
        outputs = discover.run(env)
        config = watcher_config.resolve(
            {**env, "ENABLE_AI": "false"}, root=Path(__file__).resolve().parent.parent
        )
        report = discover.build_report(env, outputs, config)
        discover.apply_ai_digest(report, outputs, config)
        return report, outputs, config

    def test_writes_when_env_set(self, tmp_path, monkeypatch):
        _install_stub_provider(monkeypatch, "1.2.3")
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        report, _, config = self._report(monkeypatch)
        discover.emit_report(report, config.reporting)
        body = summary.read_text(encoding="utf-8")
        assert "# Blackout Secure Upstream Watcher Report" in body
        assert "## Executive summary" in body
        assert "## Configuration used" in body
        assert "## Detailed Findings" in body
        assert "UW-RES-001" in body
        assert "UW-CHG-001" in body
        assert "UW-TRK-001" in body
        assert "github_release" in body
        assert "stub/repo" in body
        assert "1.2.3" in body

    def test_skipped_when_env_unset(self, tmp_path, monkeypatch):
        _install_stub_provider(monkeypatch, "1.2.3")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        report, _, config = self._report(monkeypatch)
        # Should not raise and should not create any summary file.
        discover.emit_report(report, config.reporting)

    def test_io_error_does_not_fail_run(self, tmp_path, monkeypatch):
        """If GITHUB_STEP_SUMMARY points to an unwritable path, the run must
        still succeed — the report is best-effort, not load-bearing."""
        _install_stub_provider(monkeypatch, "1.2.3")
        unwritable = tmp_path / "no" / "such" / "dir" / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(unwritable))
        report, outputs, config = self._report(monkeypatch)
        # Must not raise.
        discover.emit_report(report, config.reporting)
        assert outputs["version"] == "1.2.3"

    def test_disabled_by_input(self, tmp_path, monkeypatch):
        _install_stub_provider(monkeypatch, "1.2.3")
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        env = _env("")
        outputs = discover.run(env)
        config = watcher_config.resolve(
            {**env, "ENABLE_AI": "false", "ENABLE_JOB_SUMMARY": "false"},
            root=Path(__file__).resolve().parent.parent,
        )
        report = discover.build_report(env, outputs, config)
        discover.emit_report(report, config.reporting)
        assert not summary.exists()


# ---------------------------------------------------------------------------
# v1.1.0 — label + extra outputs flow through run()
# ---------------------------------------------------------------------------


class TestRunExtraOutputs:
    def test_extra_fields_present_in_outputs(self, tmp_path, monkeypatch):
        _install_stub_provider(monkeypatch, "1.2.3")
        outputs = discover.run(_env(""))
        assert outputs["label"] == "stub/repo"
        assert outputs["release_url"] == "https://example.test/1.2.3"
        assert outputs["release_name"] == "v1.2.3"
        assert "stub release notes" in outputs["release_body"]
        assert "\n" in outputs["release_body"]
        assert outputs["published_at"] == "2024-09-12T18:23:00Z"

    def test_run_writes_multiline_release_body_to_github_output(
        self, tmp_path, monkeypatch
    ):
        _install_stub_provider(monkeypatch, "1.2.3")
        out_file = tmp_path / "gh-out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        env = _main_env("none")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        rc = discover.main()
        assert rc == 0
        content = out_file.read_text(encoding="utf-8")
        # Heredoc for release_body
        assert "release_body<<BOS_UPSTREAM_EOF" in content
        assert "stub release notes" in content
        # Single-line for everything else
        assert "label=stub/repo" in content
        assert "release_name=v1.2.3" in content
        assert "published_at=2024-09-12T18:23:00Z" in content
