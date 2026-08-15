"""Package identity is owned by the action, never by configuration."""

import json
from pathlib import Path

import discover
import watcher_metadata


class TestPackageMetadata:
    def test_identity_reports_the_running_version(self):
        meta = watcher_metadata.package_metadata(discover.__version__)
        assert meta["name"] == "bos-upstream-watcher"
        assert meta["version"] == discover.__version__
        assert meta["license"] == "Apache-2.0"
        assert meta["repository"].endswith("/bos-upstream-watcher")

    def test_identity_is_json_serialisable_on_one_line(self):
        line = json.dumps(
            watcher_metadata.package_metadata(discover.__version__), separators=(",", ":")
        )
        assert "\n" not in line

    def test_version_matches_action_documentation(self):
        root = Path(__file__).resolve().parent.parent
        assert discover.__version__ == "1.2.0"
        assert "config_path:" in (root / "action.yml").read_text(encoding="utf-8")


class TestStripPackageMetadata:
    def test_reserved_keys_are_removed_and_reported(self):
        section, ignored = watcher_metadata.strip_package_metadata(
            {"source": "npm", "version": "9.9.9", "license": "MIT"}
        )
        assert section == {"source": "npm"}
        assert set(ignored) == {"version", "license"}

    def test_package_name_is_a_watcher_setting_not_identity(self):
        section, ignored = watcher_metadata.strip_package_metadata({"package_name": "left-pad"})
        assert section == {"package_name": "left-pad"}
        assert ignored == ()

    def test_clean_sections_are_returned_unchanged(self):
        section = {"source": "npm"}
        assert watcher_metadata.strip_package_metadata(section) == (section, ())
