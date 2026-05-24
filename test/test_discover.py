"""Pure-function unit tests for `src/discover.py`. No network calls."""

import json
from pathlib import Path

import pytest

import discover

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
        assert ua.startswith("bos-discover-upstream-release/")

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
    }


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
        for k, v in _env(str(tracker)).items():
            monkeypatch.setenv(k, v)

        rc = discover.main()
        assert rc == 0

        content = out_file.read_text(encoding="utf-8")
        assert "changed=true" in content
        assert "version=9.9.9" in content
        assert "tag=v9.9.9" in content


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
        assert "name: 'Blackout Secure Discover Upstream Release'" in body
        assert "using: composite" in body
        assert 'python3 "${GITHUB_ACTION_PATH}/src/discover.py"' in body
