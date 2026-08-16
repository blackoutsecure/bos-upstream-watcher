"""Layered configuration for the upstream watcher.

Four tiers merge, in precedence order:

1. the bundled marketplace defaults shipped with the action,
2. an optional organization/hub global config file (and inline JSON),
3. the per-repo config file (and inline JSON), and
4. the action inputs, which always win when they are set.

Per-repo policy lives under an ``upstream_watcher`` section of
``.github/bos-universal-config.json`` (preferred), so one universal config
document can carry security, Marketplace, sync, and watcher policy together.
Every key is optional and unknown keys are ignored, so a newer action can
extend the schema without breaking older callers.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watcher_ai import AISettings, settings_from_section
from watcher_metadata import strip_package_metadata
from watcher_reporting import ReportingSettings, reporting_settings

CONFIG_SECTION = "upstream_watcher"
MARKETPLACE_CONFIG_FILE = "upstream-watcher-marketplace-config.json"
COMPANION_CONFIG_SECTIONS = ("organization", "security", "marketplace", "general")
DEFAULT_CONFIG_PATHS = (
    ".github/bos-universal-config.json",
    "bos-universal-config.json",
    "upstream-watcher.json",
    ".upstream-watcher.json",
)
DEFAULT_GLOBAL_CONFIG_PATH = ".github/blackout-secure-upstream-watcher-global-config.json"

# Config key -> action env var. The env dict is the contract `discover.run()`
# already consumes, so config and inputs converge on one shape.
FIELD_TO_ENV = {
    "source": "SOURCE",
    "upstream_repo": "UPSTREAM_REPO",
    "upstream_branch": "UPSTREAM_BRANCH",
    "version_file_path": "VERSION_FILE_PATH",
    "image_ref": "IMAGE_REF",
    "package_name": "PACKAGE_NAME",
    "version_url": "VERSION_URL",
    "version_regex": "VERSION_REGEX",
    "tag_pattern": "TAG_PATTERN",
    "strip_v_prefix": "STRIP_V_PREFIX",
    "tracker_path": "TRACKER_PATH",
    "include_prereleases": "INCLUDE_PRERELEASES",
    "user_agent": "USER_AGENT_OVERRIDE",
}
BOOL_FIELDS = frozenset({"strip_v_prefix", "include_prereleases"})
# Explicit "no tracker file" tokens. An empty value means "inherit from the
# next config tier", so disabling needs a word rather than an empty string.
TRACKER_DISABLE_TOKENS = frozenset({"none", "off", "false", "disabled"})
VALID_SOURCES = frozenset(
    {
        "github_release",
        "github_branch_file",
        "github_tags",
        "container_image",
        "npm",
        "pypi",
        "generic_url",
    }
)


class ConfigError(Exception):
    """Raised for unreadable or invalid configuration."""


@dataclass(frozen=True)
class ResolvedConfig:
    """The merged, validated view of every configuration tier."""

    env: dict[str, str]
    section: dict[str, Any]
    ai: AISettings
    reporting: ReportingSettings
    sources: tuple[str, ...] = ()
    repository_config: str = ""
    global_config: str = ""
    ignored_metadata_keys: tuple[str, ...] = field(default=())


def load_json_file(path: Path) -> Any:
    """Read a JSON document, reporting parse failures as :class:`ConfigError`."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file must be UTF-8 text: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"failed to read config file {path}: {exc}") from exc


def find_config(root: Path, config_path: str = "") -> Path | None:
    """Locate the repo config file, or return ``None`` when there is none."""
    if config_path:
        candidate = Path(config_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate}")
        return candidate
    for relative in DEFAULT_CONFIG_PATHS:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``; scalars from override win."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _as_document(data: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a JSON object: {source}")
    return data


def parse_inline(raw: str | Mapping[str, Any] | None, *, source: str) -> dict[str, Any]:
    """Parse an inline JSON object supplied through an action input."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"inline config must decode to a JSON object: {source}")
    return data


def extract_section(document: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Return the watcher policy plus the universal companion sections.

    A document with neither an ``upstream_watcher`` key nor any universal
    companion section is treated as the section itself, which keeps standalone
    ``upstream-watcher.json`` files simple.
    """
    if CONFIG_SECTION in document:
        section = document[CONFIG_SECTION]
    elif any(name in document for name in COMPANION_CONFIG_SECTIONS):
        section = {}
    else:
        section = document
    if not isinstance(section, dict):
        raise ConfigError(f"'{CONFIG_SECTION}' must be a JSON object: {source}")

    merged = dict(section)
    for name in COMPANION_CONFIG_SECTIONS:
        companion = document.get(name)
        if companion is None:
            continue
        if not isinstance(companion, dict):
            raise ConfigError(f"'{name}' must be a JSON object: {source}")
        nested = merged.get(name)
        if nested is not None and not isinstance(nested, dict):
            raise ConfigError(f"'{CONFIG_SECTION}.{name}' must be a JSON object: {source}")
        merged[name] = _deep_merge(nested or {}, companion)
    return merged


def bundled_config(root: Path) -> dict[str, Any]:
    """Load the marketplace defaults shipped alongside this module."""
    path = Path(__file__).resolve().parent / MARKETPLACE_CONFIG_FILE
    if not path.is_file():  # tolerate a trimmed checkout rather than fail the run
        return {}
    document = _as_document(load_json_file(path), source="marketplace config")
    return extract_section(document, source="marketplace config")


def _bool_env(value: str, key: str) -> bool:
    if value in {"true", "false"}:
        return value == "true"
    raise ConfigError(f"input '{key}' must be 'true' or 'false' (got '{value}')")


def _field_to_env_value(name: str, value: Any) -> str:
    if name in BOOL_FIELDS:
        if not isinstance(value, bool):
            raise ConfigError(f"'{CONFIG_SECTION}.{name}' must be true or false")
        return "true" if value else "false"
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"'{CONFIG_SECTION}.{name}' must be a string")
    return str(value)


def resolve(inputs: Mapping[str, str], *, root: Path | None = None) -> ResolvedConfig:
    """Merge every config tier with the action inputs and validate the result."""
    root = root or Path(os.environ.get("GITHUB_WORKSPACE") or ".").resolve()

    use_global = (inputs.get("USE_GLOBAL_CONFIG") or "auto").strip().lower()
    if use_global not in {"auto", "true", "false"}:
        raise ConfigError("input 'use_global_config' must be 'auto', 'true', or 'false'")

    use_marketplace = (inputs.get("USE_MARKETPLACE_CONFIG") or "true").strip().lower()
    if use_marketplace not in {"true", "false"}:
        raise ConfigError("input 'use_marketplace_config' must be 'true' or 'false'")

    sources: list[str] = []
    merged: dict[str, Any] = bundled_config(root) if use_marketplace == "true" else {}
    ignored: list[str] = []
    if merged:
        sources.append("bundled marketplace defaults")
        merged, dropped = strip_package_metadata(merged)
        ignored.extend(dropped)

    global_path_input = (inputs.get("GLOBAL_CONFIG_PATH") or "").strip()
    global_display = ""
    if use_global != "false":
        candidate = Path(global_path_input or DEFAULT_GLOBAL_CONFIG_PATH)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            document = _as_document(load_json_file(candidate), source=str(candidate))
            merged, dropped = _merge_tier(merged, document, source=str(candidate))
            ignored.extend(dropped)
            sources.append(f"global config ({_relative(candidate, root)})")
            global_display = _relative(candidate, root)
        elif use_global == "true":
            raise ConfigError(f"config file not found: {candidate}")

    global_inline = parse_inline(inputs.get("GLOBAL_CONFIG_JSON"), source="inline global config")
    if global_inline:
        merged, dropped = _merge_tier(merged, global_inline, source="inline global config")
        ignored.extend(dropped)
        sources.append("inline global config")

    repo_config = find_config(root, (inputs.get("CONFIG_PATH") or "").strip())
    repo_display = ""
    if repo_config is not None:
        document = _as_document(load_json_file(repo_config), source=str(repo_config))
        merged, dropped = _merge_tier(merged, document, source=str(repo_config))
        ignored.extend(dropped)
        repo_display = _relative(repo_config, root)
        sources.append(f"repository config ({repo_display})")

    repo_inline = parse_inline(inputs.get("CONFIG_JSON"), source="inline repository config")
    if repo_inline:
        merged, dropped = _merge_tier(merged, repo_inline, source="inline repository config")
        ignored.extend(dropped)
        sources.append("inline repository config")

    env = _env_from_section(merged)
    overridden = False
    tracker_from_input = False
    for name, key in FIELD_TO_ENV.items():
        supplied = (inputs.get(key) or "").strip()
        if not supplied:
            continue
        if name in BOOL_FIELDS:
            supplied = "true" if _bool_env(supplied, key.lower()) else "false"
        env[key] = supplied
        overridden = True
        tracker_from_input = tracker_from_input or name == "tracker_path"
    if overridden:
        sources.append("action inputs")

    if env["TRACKER_PATH"].strip().lower() in TRACKER_DISABLE_TOKENS:
        env["TRACKER_PATH"] = ""
        tracker_from_input = True

    _validate(env, validate_tracker_path=not tracker_from_input)

    ai = settings_from_section(merged)
    ai = _apply_ai_input_overrides(ai, inputs)
    reporting = reporting_settings(merged)
    reporting = _apply_reporting_input_overrides(reporting, inputs)

    return ResolvedConfig(
        env=env,
        section=merged,
        ai=ai,
        reporting=reporting,
        sources=tuple(sources),
        repository_config=repo_display,
        global_config=global_display,
        ignored_metadata_keys=tuple(dict.fromkeys(ignored)),
    )


def _merge_tier(
    base: dict[str, Any],
    document: dict[str, Any],
    *,
    source: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    section = extract_section(document, source=source)
    section, dropped = strip_package_metadata(section)
    if section.get("use_marketplace_config") is False:
        base = {}
    return _deep_merge(base, section), dropped


def _env_from_section(section: Mapping[str, Any]) -> dict[str, str]:
    env = dict.fromkeys(FIELD_TO_ENV.values(), "")
    for name, key in FIELD_TO_ENV.items():
        if name in section and section[name] is not None:
            env[key] = _field_to_env_value(name, section[name])
    return env


def _validate(env: Mapping[str, str], *, validate_tracker_path: bool = True) -> None:
    source = env.get("SOURCE", "")
    if not source:
        raise ConfigError(
            "'source' is required — set the `source` input or "
            f"'{CONFIG_SECTION}.source' in the repository config"
        )
    if source not in VALID_SOURCES:
        raise ConfigError(
            f"unknown source '{source}' — must be one of {', '.join(sorted(VALID_SOURCES))}"
        )
    for key in ("STRIP_V_PREFIX", "INCLUDE_PRERELEASES"):
        if env.get(key) not in {"true", "false"}:
            raise ConfigError(f"'{key.lower()}' must be true or false (got '{env.get(key)}')")
    tracker = env.get("TRACKER_PATH", "")
    if (
        validate_tracker_path
        and tracker
        and (tracker.startswith("/") or ".." in Path(tracker).parts)
    ):
        raise ConfigError(
            f"'{CONFIG_SECTION}.tracker_path' must be a repo-relative path: '{tracker}'"
        )


def _apply_ai_input_overrides(settings: AISettings, inputs: Mapping[str, str]) -> AISettings:
    """Action inputs win over config for the AI kill switch and provider."""
    enable = (inputs.get("ENABLE_AI") or "").strip().lower()
    provider = (inputs.get("AI_PROVIDER") or "").strip()
    if enable not in {"", "true", "false"}:
        raise ConfigError("input 'enable_ai' must be 'true' or 'false'")

    summary_enabled = settings.enable_ai_release_summary
    error_enabled = settings.enable_ai_error_remediation
    if enable:
        summary_enabled = error_enabled = enable == "true"

    return AISettings(
        enable_ai_release_summary=summary_enabled,
        ai_release_summary_provider=provider or settings.ai_release_summary_provider,
        enable_ai_error_remediation=error_enabled,
        ai_error_remediation_provider=provider or settings.ai_error_remediation_provider,
        local_heuristic_fallback=settings.local_heuristic_fallback,
        timeout_seconds=settings.timeout_seconds,
    )


def _apply_reporting_input_overrides(
    settings: ReportingSettings,
    inputs: Mapping[str, str],
) -> ReportingSettings:
    enable = (inputs.get("ENABLE_JOB_SUMMARY") or "").strip().lower()
    if enable not in {"", "true", "false"}:
        raise ConfigError("input 'enable_job_summary' must be 'true' or 'false'")
    if not enable:
        return settings
    return ReportingSettings(
        enable_job_summary=enable == "true",
        enable_annotations=settings.enable_annotations,
        enable_ai_section=settings.enable_ai_section,
        title_prefix=settings.title_prefix,
        fail_on=settings.fail_on,
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)
