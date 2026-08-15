"""AI helper tests. No network calls — providers are stubbed or absent."""

import json

import pytest

import watcher_ai


class TestSettings:
    def test_defaults_enable_ai(self):
        settings = watcher_ai.settings_from_section({})
        assert settings.enable_ai_release_summary is True
        assert settings.enable_ai_error_remediation is True
        assert settings.local_heuristic_fallback is True

    def test_section_toggles_are_read(self):
        settings = watcher_ai.settings_from_section(
            {
                "ai": {
                    "enable_ai_release_summary": False,
                    "ai_release_summary_provider": "none",
                    "timeout_seconds": 5,
                }
            }
        )
        assert settings.enable_ai_release_summary is False
        assert settings.ai_release_summary_provider == "none"
        # Unset provider inherits the summary provider.
        assert settings.ai_error_remediation_provider == "none"
        assert settings.timeout_seconds == 5

    def test_invalid_types_are_rejected(self):
        with pytest.raises(watcher_ai.AIError):
            watcher_ai.settings_from_section({"ai": {"enable_ai_release_summary": "yes"}})
        with pytest.raises(watcher_ai.AIError):
            watcher_ai.settings_from_section({"ai": {"timeout_seconds": 0}})
        with pytest.raises(watcher_ai.AIError):
            watcher_ai.settings_from_section({"ai": "on"})


class TestProviderDetection:
    def test_no_credential_means_no_provider(self):
        assert watcher_ai.detect_provider("auto", environ={}) is None

    def test_github_token_enables_github_models(self):
        provider = watcher_ai.detect_provider("auto", environ={"GITHUB_TOKEN": "t"})
        assert provider is not None
        assert provider.name == "github-models"
        assert provider.endpoint == watcher_ai.GITHUB_MODELS_ENDPOINT

    def test_disabled_names_return_none(self):
        for name in ("none", "off", "disabled", "false"):
            assert watcher_ai.detect_provider(name, environ={"GITHUB_TOKEN": "t"}) is None

    def test_non_https_endpoint_is_refused(self):
        provider = watcher_ai.detect_provider(
            "auto",
            environ={"GITHUB_TOKEN": "t", "GITHUB_MODELS_ENDPOINT": "http://evil.test"},
        )
        assert provider is None

    def test_external_provider_needs_key_and_endpoint(self):
        assert watcher_ai.detect_provider("acme", environ={"ACME_API_KEY": "k"}) is None
        provider = watcher_ai.detect_provider(
            "acme", environ={"ACME_API_KEY": "k", "ACME_API_ENDPOINT": "https://acme.test/v1"}
        )
        assert provider is not None
        assert provider.name == "acme"


class TestHeuristics:
    def test_breaking_keyword_beats_semver_delta(self):
        assert watcher_ai.heuristic_impact("patch", "Note: BREAKING CHANGE in the CLI") == "high"

    def test_semver_delta_drives_impact(self):
        assert watcher_ai.heuristic_impact("major", "") == "high"
        assert watcher_ai.heuristic_impact("minor", "") == "medium"
        assert watcher_ai.heuristic_impact("patch", "") == "low"
        assert watcher_ai.heuristic_impact("unknown", "") == "unknown"

    def test_summary_mentions_both_versions(self):
        text = watcher_ai.heuristic_summary(
            {"label": "nginx/nginx", "previous_version": "1.0.0", "version": "2.0.0", "update_type": "major"}
        )
        assert "nginx/nginx" in text
        assert "1.0.0 → 2.0.0" in text
        assert "major" in text


class _StubProvider(watcher_ai.Provider):
    pass


def _provider():
    return watcher_ai.Provider(
        name="github-models", endpoint="https://models.test", model="m", token="t"
    )


class TestReleaseDigest:
    def test_valid_response_is_returned(self, monkeypatch):
        captured = {}

        def fake_request(payload, provider, *, timeout):
            captured["payload"] = payload
            return json.dumps({"summary": "- did a thing", "impact": "medium"})

        monkeypatch.setattr(watcher_ai, "_request_content", fake_request)
        digest = watcher_ai.release_digest({"version": "1.2.3"}, _provider())
        assert digest is not None
        assert digest.impact == "medium"
        assert digest.summary == "- did a thing"

    def test_only_allowlisted_fields_are_sent(self, monkeypatch):
        captured = {}

        def fake_request(payload, provider, *, timeout):
            captured["sent"] = json.loads(payload["messages"][1]["content"])
            return json.dumps({"summary": "s", "impact": "low"})

        monkeypatch.setattr(watcher_ai, "_request_content", fake_request)
        watcher_ai.release_digest(
            {"version": "1.2.3", "tracker_path": "secret.json", "token": "nope"}, _provider()
        )
        assert set(captured["sent"]) == {
            "label",
            "source",
            "previous_version",
            "version",
            "update_type",
            "release_name",
            "release_body",
        }

    def test_release_body_is_truncated(self, monkeypatch):
        captured = {}

        def fake_request(payload, provider, *, timeout):
            captured["sent"] = json.loads(payload["messages"][1]["content"])
            return None

        monkeypatch.setattr(watcher_ai, "_request_content", fake_request)
        watcher_ai.release_digest({"release_body": "x" * 99999}, _provider())
        assert len(captured["sent"]["release_body"]) == watcher_ai.MAX_RELEASE_BODY_CHARS

    def test_unusable_response_returns_none(self, monkeypatch):
        for body in (None, "not json", json.dumps({"summary": "s", "impact": "critical"})):
            monkeypatch.setattr(
                watcher_ai, "_request_content", lambda *a, b=body, **k: b
            )
            assert watcher_ai.release_digest({}, _provider()) is None


class TestErrorRemediation:
    def test_valid_response_is_returned(self, monkeypatch):
        monkeypatch.setattr(
            watcher_ai,
            "_request_content",
            lambda *a, **k: json.dumps(
                {"recommendation": "do x", "rationale": "because", "confidence": "high"}
            ),
        )
        advice = watcher_ai.recommend_error({"category": "c"}, _provider())
        assert advice is not None
        assert advice.confidence == "High"

    def test_invalid_confidence_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            watcher_ai,
            "_request_content",
            lambda *a, **k: json.dumps(
                {"recommendation": "r", "rationale": "r", "confidence": "certain"}
            ),
        )
        assert watcher_ai.recommend_error({}, _provider()) is None
