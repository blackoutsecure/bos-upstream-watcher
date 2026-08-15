"""Structured run findings and GitHub Actions reports.

Every run produces the same audit layout: an executive summary, the effective
configuration, recommended actions, and a per-rule findings table. Rule IDs are
stable so downstream automation can key off them.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPORT_LABELS = {
    "pass": "Pass",
    "warn": "Warning",
    "fail": "High",
    "skip": "Not Assessed",
}
REPORT_MEANINGS = {
    "pass": "Control satisfied.",
    "warn": "Advisory finding; review recommended.",
    "fail": "Required control failed and must be corrected.",
    "skip": "Not evaluated on this run; coverage cannot be inferred.",
}
_SEVERITY_ORDER = ("fail", "warn", "pass", "skip")
_ANNOTATION_COMMAND = {"fail": "error", "warn": "warning", "pass": "notice"}


class ReportError(Exception):
    """Raised for invalid `organization.reporting` policy."""


@dataclass(frozen=True)
class ReportingSettings:
    """Normalized organization-wide report policy."""

    enable_job_summary: bool = True
    enable_annotations: bool = True
    enable_ai_section: bool = True
    title_prefix: str = "Blackout Secure"
    fail_on: str = "fail"


def reporting_settings(section: dict[str, Any]) -> ReportingSettings:
    """Read ``organization.reporting`` using the automation hub defaults."""
    organization = section.get("organization") or {}
    if not isinstance(organization, dict):
        raise ReportError("'organization' must be a JSON object")
    reporting = organization.get("reporting") or {}
    if not isinstance(reporting, dict):
        raise ReportError("'organization.reporting' must be a JSON object")

    fail_on = _text(reporting, "fail_on", "fail").lower()
    if fail_on not in {"fail", "warn", "never"}:
        raise ReportError("'organization.reporting.fail_on' must be 'fail', 'warn', or 'never'")

    return ReportingSettings(
        enable_job_summary=_flag(reporting, "enable_job_summary", True),
        enable_annotations=_flag(reporting, "enable_annotations", True),
        enable_ai_section=_flag(reporting, "enable_ai_section", True),
        title_prefix=_text(reporting, "title_prefix", "Blackout Secure"),
        fail_on=fail_on,
    )


def _flag(reporting: dict[str, Any], key: str, default: bool) -> bool:
    value = reporting.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ReportError(f"'organization.reporting.{key}' must be true or false")


def _text(reporting: dict[str, Any], key: str, default: str) -> str:
    value = reporting.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"'organization.reporting.{key}' must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class Finding:
    """A deterministic assessment of one control."""

    rule_id: str
    category: str
    severity: str
    location: str
    evidence: str
    remediation: str
    confidence: str = "High (deterministic)"
    source: str = "Blackout Secure deterministic rules"

    def ai_payload(self) -> dict[str, str]:
        """Return the allowlisted fields that may be sent to an AI provider."""
        return {
            "category": self.category,
            "error_text": self.evidence,
            "location": self.location,
            "deterministic_remediation": self.remediation,
        }


@dataclass(frozen=True)
class RunContext:
    """Non-secret invocation metadata available even when config loading fails."""

    command: str = "discover"
    source: str = ""
    label: str = ""
    tracker_path: str = ""
    repository_config: str = ""
    global_config: str = ""
    config_sources: tuple[str, ...] = ()
    ignored_metadata_keys: tuple[str, ...] = ()
    package_version: str = ""


@dataclass
class AISection:
    """Rendered AI block state. Advisory only; never changes a finding."""

    status: str = "Disabled by configuration"
    summary: str = ""
    impact: str = ""
    source: str = ""
    recommendation: str = ""
    rationale: str = ""
    confidence: str = ""


@dataclass
class RunReport:
    """Everything the summary renderer needs for one run."""

    findings: list[Finding] = field(default_factory=list)
    context: RunContext = field(default_factory=RunContext)
    ai: AISection = field(default_factory=AISection)
    verdict: str = ""

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(_SEVERITY_ORDER, 0)
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts


def should_fail(report: RunReport, settings: ReportingSettings) -> bool:
    """Decide the process exit status from report severity and org policy."""
    counts = report.counts()
    if settings.fail_on == "never":
        return False
    if settings.fail_on == "warn":
        return bool(counts["fail"] or counts["warn"])
    return bool(counts["fail"])


# ---------------------------------------------------------------------------
# Deterministic error classification
# ---------------------------------------------------------------------------

_RULES: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (
        ("invalid json", "must decode to an object", "must be a json object"),
        "UW-CFG-001",
        "Invalid configuration JSON",
        "Correct the JSON syntax or type at the reported location, then re-run. Validate the "
        "document with a JSON parser before retrying.",
    ),
    (
        ("config file not found", "file not found:"),
        "UW-CFG-002",
        "Configuration file not found",
        "Create the requested config file, correct `config_path` / `global_config_path`, or set "
        "`use_global_config: false` when the tier is intentionally absent.",
    ),
    (
        ("unknown source", "is required for", "must be one of", "must be true or false"),
        "UW-CFG-003",
        "Invalid upstream-watcher settings",
        "Set the reported field either as an action input or under `upstream_watcher` in the repo "
        "config. Inputs win over config, and config wins over the bundled defaults.",
    ),
    (
        ("rate limit", "http 401", "http 403", "saml", "sso", "token"),
        "UW-AUTH-001",
        "Upstream authentication or rate limit failure",
        "Supply a `github_token` with `Contents: read` on the upstream repo, authorize it for SAML "
        "SSO when the org requires it, or reduce polling frequency. Anonymous GitHub API calls are "
        "capped at 60 requests/hour.",
    ),
    (
        ("returned http", "connection", "timed out", "temporary failure", "urlopen"),
        "UW-NET-001",
        "Upstream request failed",
        "Confirm the upstream endpoint is reachable and correct. Transient 5xx and connection "
        "errors are retried three times before the run fails; re-run once upstream recovers.",
    ),
    (
        ("did not match", "no tags matched", "capture group", "empty", "no releases"),
        "UW-MATCH-001",
        "No upstream version matched the configured filter",
        "Relax or correct `tag_pattern` / `version_regex`, or set `include_prereleases: true` when "
        "the upstream ships only pre-releases.",
    ),
    (
        ("unsupported registry", "image_ref", "owner/name"),
        "UW-CFG-004",
        "Unsupported upstream reference",
        "Use a supported reference format for the selected source. Container images support "
        "docker.io, ghcr.io, and quay.io; GitHub sources require `owner/name`.",
    ),
    (
        ("permission denied", "is a directory", "no such file or directory", "read-only"),
        "UW-FS-001",
        "Tracker file I/O error",
        "Point `tracker_path` at a writable, repo-relative path and make sure the workflow checks "
        "out the repository before this step.",
    ),
)


def assess_error(error: Exception) -> Finding:
    """Classify an exception into a stable rule and deterministic remediation."""
    message = str(error).strip() or type(error).__name__
    lower = message.lower()
    location = _error_location(message)
    for needles, rule_id, category, remediation in _RULES:
        if any(needle in lower for needle in needles):
            return Finding(
                rule_id=rule_id,
                category=category,
                severity="fail",
                location=location,
                evidence=message,
                remediation=remediation,
            )
    return Finding(
        rule_id="UW-RUN-000",
        category="Upstream watcher runtime error",
        severity="fail",
        location=location,
        evidence=message,
        remediation=(
            "Review the error evidence and runner state, correct the underlying condition, and "
            "re-run. Escalate with the report rule and action version if it persists."
        ),
        confidence="Medium (deterministic)",
    )


def _error_location(message: str) -> str:
    for pattern in (
        r"(https?://\S+)",
        r"'(upstream_watcher(?:\.[A-Za-z0-9_.-]+)?)'",
        r"config file not found:\s*(.+)$",
        r"file not found:\s*(.+)$",
        r"input '([A-Za-z0-9_]+)'",
        r"\b([A-Za-z0-9_]+) is required\b",
    ):
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).strip().rstrip(".,")
    return "Configuration or runner context"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_summary(report: RunReport, settings: ReportingSettings) -> str:
    """Render the standard audit layout for one run."""
    counts = report.counts()
    total = sum(counts.values())
    context = report.context
    sources = ", ".join(context.config_sources) or "Action inputs only"
    verdict = report.verdict or _default_verdict(counts)

    lines = [
        f"# {_cell(settings.title_prefix)} Upstream Watcher Report",
        "",
        "**Provided by [Blackout Secure](https://blackoutsecure.app)**",
        "",
        "> This open-source report provides operational guidance and does not replace "
        "professional security, compliance, or legal advice.",
        "",
        "## Executive summary",
        "",
        "| Pass | Warning | High | Not Assessed | Total |",
        "| ---: | ---: | ---: | ---: | ---: |",
        f"| {counts['pass']} | {counts['warn']} | {counts['fail']} | {counts['skip']} | {total} |",
        "",
        f"**Verdict:** {verdict}",
        "",
        "## Configuration used",
        "",
        "| Setting | Value | What it means |",
        "| --- | --- | --- |",
        f"| Command | {_cell(context.command)} | Operation requested by the action. |",
        f"| Source | {_cell(context.source)} | Upstream provider that was queried. |",
        f"| Upstream | {_cell(context.label)} | Canonical identifier of the watched upstream. |",
        f"| Tracker path | {_cell(context.tracker_path or 'disabled')} | File compared to detect change. |",
        f"| Repository config | {_cell(context.repository_config or 'none')} | Per-repo policy source. |",
        f"| Global config | {_cell(context.global_config or 'none')} | Organization policy source. |",
        f"| Config cascade | {_cell(sources)} | Tiers applied in precedence order. |",
        f"| Action version | {_cell(context.package_version)} | Release producing this report. |",
        f"| AI assistance | {_cell(report.ai.status)} | Advisory only; deterministic findings remain authoritative. |",
    ]
    if context.ignored_metadata_keys:
        lines.append(
            f"| Ignored config keys | {_cell(', '.join(context.ignored_metadata_keys))} | "
            "Reserved package identity keys cannot be overridden by config. |"
        )

    lines.extend(["", "## Recommended Actions", ""])
    actionable = [f for f in report.findings if f.severity in {"fail", "warn"}]
    if actionable:
        lines.append("### Blackout Secure Recommended Remediation")
        lines.append("")
        for finding in actionable:
            lines.append(f"- `{finding.rule_id}` — {finding.remediation}")
        lines.append("")
    else:
        lines.extend(
            ["### Blackout Secure Recommended Remediation", "", "No action required.", ""]
        )

    if settings.enable_ai_section:
        lines.extend(_ai_lines(report.ai))

    lines.extend(
        [
            "## Detailed Findings",
            "",
            "| Rule | Status | Severity | Category | Location | Evidence | Recommended remediation | Confidence | Source |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for finding in report.findings:
        lines.append(
            f"| `{_cell(finding.rule_id)}` | {_cell(finding.severity)} | "
            f"{_cell(REPORT_LABELS[finding.severity])} | {_cell(finding.category)} | "
            f"{_cell(finding.location)} | {_cell(finding.evidence)} | "
            f"{_cell(finding.remediation)} | {_cell(finding.confidence)} | {_cell(finding.source)} |"
        )

    lines.extend(
        [
            "",
            "### Scope and methodology",
            "",
            "- Findings are produced by deterministic rules from the provider response, tracker "
            "state, and config cascade.",
            "- AI is optional and cannot change a finding, its severity, or the exit code.",
            "- When AI is used, only release metadata (labels, versions, truncated release notes) "
            "or error metadata is sent. Credentials, config documents, and tracker contents are "
            "not sent.",
            f"- Report label meanings: {'; '.join(f'{REPORT_LABELS[k]} — {v}' for k, v in REPORT_MEANINGS.items())}",
            f"- Report provenance: `bos-upstream-watcher` {_cell(context.package_version)}.",
            "",
        ]
    )
    return "\n".join(lines)


def _ai_lines(ai: AISection) -> list[str]:
    lines = ["### AI-assisted analysis", ""]
    if not ai.summary and not ai.recommendation:
        lines.extend([f"_Not used: {ai.status}._", ""])
        return lines
    if ai.summary:
        lines.extend([ai.summary, ""])
        if ai.impact:
            lines.extend([f"**Assessed upgrade impact:** {_cell(ai.impact)}", ""])
    if ai.recommendation:
        lines.extend([ai.recommendation, ""])
        if ai.rationale:
            lines.extend([f"**Rationale:** {ai.rationale}", ""])
    if ai.confidence:
        lines.extend([f"**Confidence:** {_cell(ai.confidence)}  ", ""])
    lines.extend([f"**Source:** {_cell(ai.source or ai.status)}", ""])
    return lines


def _default_verdict(counts: dict[str, int]) -> str:
    if counts["fail"]:
        return "High — upstream discovery failed"
    if counts["warn"]:
        return "Warning — upstream discovered with advisories"
    return "Pass — upstream discovery completed"


def emit_annotations(report: RunReport, settings: ReportingSettings) -> list[str]:
    """Return workflow-command annotation lines for the findings."""
    if not settings.enable_annotations:
        return []
    lines = []
    for finding in report.findings:
        command = _ANNOTATION_COMMAND.get(finding.severity)
        if not command:
            continue
        message = f"{finding.rule_id} {finding.category}: {_one_line(finding.evidence)}"
        lines.append(f"::{command} title={finding.rule_id}::{message}")
    return lines


def append_summary(path: str, report_text: str) -> bool:
    """Append a report without allowing summary I/O to mask the run result."""
    try:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(report_text if report_text.endswith("\n") else report_text + "\n")
    except OSError:
        return False
    return True


def _one_line(value: str) -> str:
    return " ".join(str(value).split())


def _cell(value: object) -> str:
    escaped = html.escape(str(value), quote=True)
    return escaped.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")
