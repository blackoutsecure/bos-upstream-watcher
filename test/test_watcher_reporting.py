"""Report rendering, error classification, and fail policy tests."""

import pytest

import watcher_reporting as reporting


def _finding(severity="pass", rule_id="UW-RES-001"):
    return reporting.Finding(
        rule_id=rule_id,
        category="Upstream version resolution",
        severity=severity,
        location="nginx/nginx",
        evidence="Resolved nginx/nginx to 1.2.3.",
        remediation="No action required.",
    )


class TestReportingSettings:
    def test_defaults(self):
        settings = reporting.reporting_settings({})
        assert settings.enable_job_summary is True
        assert settings.enable_annotations is True
        assert settings.fail_on == "fail"
        assert settings.title_prefix == "Blackout Secure"

    def test_organization_section_is_read(self):
        settings = reporting.reporting_settings(
            {"organization": {"reporting": {"fail_on": "warn", "title_prefix": "Acme"}}}
        )
        assert settings.fail_on == "warn"
        assert settings.title_prefix == "Acme"

    def test_invalid_values_are_rejected(self):
        with pytest.raises(reporting.ReportError):
            reporting.reporting_settings({"organization": {"reporting": {"fail_on": "always"}}})
        with pytest.raises(reporting.ReportError):
            reporting.reporting_settings({"organization": {"reporting": {"title_prefix": ""}}})
        with pytest.raises(reporting.ReportError):
            reporting.reporting_settings({"organization": "yes"})


class TestFailPolicy:
    def test_fail_on_fail_ignores_warnings(self):
        report = reporting.RunReport(findings=[_finding("warn")])
        assert reporting.should_fail(report, reporting.ReportingSettings()) is False

    def test_fail_on_warn_escalates(self):
        report = reporting.RunReport(findings=[_finding("warn")])
        settings = reporting.ReportingSettings(fail_on="warn")
        assert reporting.should_fail(report, settings) is True

    def test_never_never_fails(self):
        report = reporting.RunReport(findings=[_finding("fail")])
        settings = reporting.ReportingSettings(fail_on="never")
        assert reporting.should_fail(report, settings) is False


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("message", "rule_id"),
        [
            ("invalid JSON in cfg.json: line 2", "UW-CFG-001"),
            ("config file not found: /x/y.json", "UW-CFG-002"),
            ("unknown source 'ftp'", "UW-CFG-003"),
            ("GitHub API 403 for https://api.github.com/x: rate limit exceeded", "UW-AUTH-001"),
            ("GET https://example.test returned HTTP 500", "UW-NET-001"),
            ("regex '^v' did not match body of https://example.test", "UW-MATCH-001"),
            ("unsupported registry 'gcr.io'", "UW-CFG-004"),
            ("[Errno 13] Permission denied: 'tracker.json'", "UW-FS-001"),
            ("something entirely unexpected", "UW-RUN-000"),
        ],
    )
    def test_rules_are_stable(self, message, rule_id):
        assert reporting.assess_error(RuntimeError(message)).rule_id == rule_id

    def test_every_finding_is_fail_severity(self):
        assert reporting.assess_error(RuntimeError("boom")).severity == "fail"

    def test_location_prefers_a_url(self):
        finding = reporting.assess_error(
            RuntimeError("GET https://example.test/v returned HTTP 500")
        )
        assert finding.location == "https://example.test/v"

    def test_ai_payload_is_allowlisted(self):
        payload = reporting.assess_error(RuntimeError("boom")).ai_payload()
        assert set(payload) == {
            "category",
            "error_text",
            "location",
            "deterministic_remediation",
        }


class TestRendering:
    def test_summary_has_every_required_section(self):
        report = reporting.RunReport(
            findings=[_finding("pass"), _finding("warn", "UW-CHG-001")],
            context=reporting.RunContext(source="npm", label="left-pad", package_version="1.2.0"),
        )
        text = reporting.render_summary(report, reporting.ReportingSettings())
        for heading in (
            "# Blackout Secure Upstream Watcher Report",
            "## Executive summary",
            "## Configuration used",
            "## Recommended Actions",
            "## Detailed Findings",
            "### Scope and methodology",
        ):
            assert heading in text
        assert "| 1 | 1 | 0 | 0 | 2 |" in text
        assert "Warning — upstream discovered with advisories" in text

    def test_pipes_and_newlines_cannot_break_the_table(self):
        finding = reporting.Finding(
            rule_id="UW-RES-001",
            category="c",
            severity="pass",
            location="a|b",
            evidence="line1\nline2",
            remediation="r",
        )
        text = reporting.render_summary(
            reporting.RunReport(findings=[finding]), reporting.ReportingSettings()
        )
        assert "a\\|b" in text
        assert "line1<br>line2" in text

    def test_ai_section_can_be_disabled(self):
        report = reporting.RunReport(findings=[_finding()])
        report.ai.summary = "- something"
        settings = reporting.ReportingSettings(enable_ai_section=False)
        assert "AI-assisted analysis" not in reporting.render_summary(report, settings)

    def test_annotations_map_severity_to_commands(self):
        report = reporting.RunReport(
            findings=[_finding("fail", "UW-RUN-000"), _finding("warn"), _finding("skip")]
        )
        lines = reporting.emit_annotations(report, reporting.ReportingSettings())
        assert lines[0].startswith("::error title=UW-RUN-000::")
        assert lines[1].startswith("::warning title=UW-RES-001::")
        assert len(lines) == 2

    def test_annotations_can_be_disabled(self):
        report = reporting.RunReport(findings=[_finding("fail")])
        settings = reporting.ReportingSettings(enable_annotations=False)
        assert reporting.emit_annotations(report, settings) == []

    def test_append_summary_reports_io_failure(self, tmp_path):
        assert reporting.append_summary(str(tmp_path / "ok.md"), "body") is True
        assert reporting.append_summary(str(tmp_path / "no" / "dir" / "x.md"), "body") is False
