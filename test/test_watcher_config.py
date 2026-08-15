"""Config cascade tests: bundled defaults, global tier, repo tier, inputs."""

import json

import pytest

import watcher_config


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _inputs(**overrides):
    base = dict.fromkeys(watcher_config.FIELD_TO_ENV.values(), "")
    base.update(
        {
            "USE_GLOBAL_CONFIG": "auto",
            "GLOBAL_CONFIG_PATH": "",
            "GLOBAL_CONFIG_JSON": "",
            "CONFIG_PATH": "",
            "CONFIG_JSON": "",
            "ENABLE_AI": "",
            "AI_PROVIDER": "",
            "ENABLE_JOB_SUMMARY": "",
        }
    )
    base.update(overrides)
    return base


class TestBundledDefaults:
    def test_marketplace_config_supplies_defaults(self, tmp_path):
        resolved = watcher_config.resolve(_inputs(SOURCE="npm", PACKAGE_NAME="left-pad"), root=tmp_path)
        assert resolved.env["TRACKER_PATH"] == ".github/upstream/tracked-release.json"
        assert resolved.env["STRIP_V_PREFIX"] == "true"
        assert resolved.env["INCLUDE_PRERELEASES"] == "false"
        assert resolved.env["VERSION_FILE_PATH"] == "version"
        assert "bundled marketplace defaults" in resolved.sources

    def test_ai_and_reporting_on_by_default(self, tmp_path):
        resolved = watcher_config.resolve(_inputs(SOURCE="pypi", PACKAGE_NAME="requests"), root=tmp_path)
        assert resolved.ai.enable_ai_release_summary is True
        assert resolved.ai.enable_ai_error_remediation is True
        assert resolved.reporting.enable_job_summary is True
        assert resolved.reporting.fail_on == "fail"


class TestRepoConfig:
    def test_universal_config_section_is_used(self, tmp_path):
        _write(
            tmp_path / ".github/bos-universal-config.json",
            {
                "upstream_watcher": {
                    "source": "github_tags",
                    "upstream_repo": "kubernetes/kubernetes",
                    "tracker_path": ".github/upstream/k8s.json",
                },
                "organization": {"reporting": {"fail_on": "warn"}},
            },
        )
        resolved = watcher_config.resolve(_inputs(), root=tmp_path)
        assert resolved.env["SOURCE"] == "github_tags"
        assert resolved.env["UPSTREAM_REPO"] == "kubernetes/kubernetes"
        assert resolved.env["TRACKER_PATH"] == ".github/upstream/k8s.json"
        assert resolved.reporting.fail_on == "warn"
        assert resolved.repository_config == ".github/bos-universal-config.json"

    def test_standalone_config_needs_no_section(self, tmp_path):
        _write(tmp_path / "upstream-watcher.json", {"source": "npm", "package_name": "@actions/core"})
        resolved = watcher_config.resolve(_inputs(), root=tmp_path)
        assert resolved.env["SOURCE"] == "npm"
        assert resolved.env["PACKAGE_NAME"] == "@actions/core"

    def test_inputs_win_over_config(self, tmp_path):
        _write(
            tmp_path / "bos-universal-config.json",
            {"upstream_watcher": {"source": "npm", "package_name": "left-pad"}},
        )
        resolved = watcher_config.resolve(
            _inputs(SOURCE="pypi", PACKAGE_NAME="requests"), root=tmp_path
        )
        assert resolved.env["SOURCE"] == "pypi"
        assert resolved.env["PACKAGE_NAME"] == "requests"
        assert "action inputs" in resolved.sources

    def test_explicit_config_path_must_exist(self, tmp_path):
        with pytest.raises(watcher_config.ConfigError, match="config file not found"):
            watcher_config.resolve(_inputs(CONFIG_PATH="nope.json"), root=tmp_path)

    def test_invalid_json_is_reported_with_path(self, tmp_path):
        path = tmp_path / "upstream-watcher.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(watcher_config.ConfigError, match="invalid JSON"):
            watcher_config.resolve(_inputs(), root=tmp_path)


class TestGlobalConfig:
    def test_global_tier_merges_below_repo(self, tmp_path):
        _write(
            tmp_path / ".github/blackout-secure-upstream-watcher-global-config.json",
            {
                "upstream_watcher": {"source": "npm", "package_name": "left-pad"},
                "organization": {"reporting": {"title_prefix": "Acme"}},
            },
        )
        _write(
            tmp_path / ".github/bos-universal-config.json",
            {"upstream_watcher": {"package_name": "@actions/core"}},
        )
        resolved = watcher_config.resolve(_inputs(), root=tmp_path)
        assert resolved.env["SOURCE"] == "npm"
        assert resolved.env["PACKAGE_NAME"] == "@actions/core"
        assert resolved.reporting.title_prefix == "Acme"
        assert resolved.global_config.endswith("upstream-watcher-global-config.json")

    def test_required_global_config_must_exist(self, tmp_path):
        with pytest.raises(watcher_config.ConfigError, match="config file not found"):
            watcher_config.resolve(_inputs(SOURCE="npm", USE_GLOBAL_CONFIG="true"), root=tmp_path)

    def test_disabled_global_config_is_skipped(self, tmp_path):
        _write(
            tmp_path / ".github/blackout-secure-upstream-watcher-global-config.json",
            {"upstream_watcher": {"source": "npm"}},
        )
        with pytest.raises(watcher_config.ConfigError, match="'source' is required"):
            watcher_config.resolve(_inputs(USE_GLOBAL_CONFIG="false"), root=tmp_path)

    def test_inline_json_merges_last(self, tmp_path):
        resolved = watcher_config.resolve(
            _inputs(CONFIG_JSON='{"upstream_watcher": {"source": "pypi", "package_name": "flask"}}'),
            root=tmp_path,
        )
        assert resolved.env["SOURCE"] == "pypi"
        assert resolved.env["PACKAGE_NAME"] == "flask"
        assert "inline repository config" in resolved.sources

    def test_use_marketplace_config_false_drops_bundled_defaults(self, tmp_path):
        resolved = watcher_config.resolve(
            _inputs(
                CONFIG_JSON=json.dumps(
                    {
                        "upstream_watcher": {
                            "use_marketplace_config": False,
                            "source": "npm",
                            "package_name": "left-pad",
                            "strip_v_prefix": True,
                            "include_prereleases": False,
                        }
                    }
                )
            ),
            root=tmp_path,
        )
        assert resolved.env["TRACKER_PATH"] == ""


class TestValidation:
    def test_missing_source_is_actionable(self, tmp_path):
        with pytest.raises(watcher_config.ConfigError, match="'source' is required"):
            watcher_config.resolve(_inputs(), root=tmp_path)

    def test_unknown_source_lists_valid_values(self, tmp_path):
        with pytest.raises(watcher_config.ConfigError, match="unknown source"):
            watcher_config.resolve(_inputs(SOURCE="ftp"), root=tmp_path)

    def test_config_tracker_path_must_be_relative(self, tmp_path):
        _write(
            tmp_path / "upstream-watcher.json",
            {"source": "npm", "package_name": "x", "tracker_path": "/etc/passwd"},
        )
        with pytest.raises(watcher_config.ConfigError, match="repo-relative"):
            watcher_config.resolve(_inputs(), root=tmp_path)

    def test_tracker_disable_token_clears_path(self, tmp_path):
        resolved = watcher_config.resolve(
            _inputs(SOURCE="npm", PACKAGE_NAME="x", TRACKER_PATH="none"), root=tmp_path
        )
        assert resolved.env["TRACKER_PATH"] == ""

    def test_bool_input_must_be_true_or_false(self, tmp_path):
        with pytest.raises(watcher_config.ConfigError, match="strip_v_prefix"):
            watcher_config.resolve(_inputs(SOURCE="npm", STRIP_V_PREFIX="yes"), root=tmp_path)

    def test_bool_config_field_must_be_json_bool(self, tmp_path):
        _write(tmp_path / "upstream-watcher.json", {"source": "npm", "strip_v_prefix": "true"})
        with pytest.raises(watcher_config.ConfigError, match="must be true or false"):
            watcher_config.resolve(_inputs(), root=tmp_path)


class TestPackageIdentity:
    def test_reserved_identity_keys_cannot_be_overridden(self, tmp_path):
        _write(
            tmp_path / "upstream-watcher.json",
            {"source": "npm", "package_name": "x", "version": "9.9.9", "author": "someone else"},
        )
        resolved = watcher_config.resolve(_inputs(), root=tmp_path)
        assert "version" in resolved.ignored_metadata_keys
        assert "author" in resolved.ignored_metadata_keys
        assert "version" not in resolved.section


class TestInputOverrides:
    def test_enable_ai_false_disables_every_feature(self, tmp_path):
        resolved = watcher_config.resolve(
            _inputs(SOURCE="npm", PACKAGE_NAME="x", ENABLE_AI="false"), root=tmp_path
        )
        assert resolved.ai.enable_ai_release_summary is False
        assert resolved.ai.enable_ai_error_remediation is False

    def test_provider_input_overrides_config(self, tmp_path):
        resolved = watcher_config.resolve(
            _inputs(SOURCE="npm", PACKAGE_NAME="x", AI_PROVIDER="none"), root=tmp_path
        )
        assert resolved.ai.ai_release_summary_provider == "none"

    def test_job_summary_input_overrides_config(self, tmp_path):
        resolved = watcher_config.resolve(
            _inputs(SOURCE="npm", PACKAGE_NAME="x", ENABLE_JOB_SUMMARY="false"), root=tmp_path
        )
        assert resolved.reporting.enable_job_summary is False
