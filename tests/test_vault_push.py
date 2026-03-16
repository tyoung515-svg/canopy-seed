import asyncio
import json

from core.api_server import DashboardAPI
import start


class DummySettings:
    def __init__(self):
        self.ANTHROPIC_API_KEY = ""
        self.GOOGLE_API_KEY = ""
        self.GEMINI_API_KEY = ""
        self.OPENAI_API_KEY = ""


class DummyAIBackend:
    def __init__(self):
        self._claude_client = object()


class DummyRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _build_api(settings=None, ai_backend=None):
    api = DashboardAPI.__new__(DashboardAPI)
    api.settings = settings or DummySettings()
    api.ai_backend = ai_backend or DummyAIBackend()
    return api


def test_vault_push_updates_all_three_keys():
    api = _build_api()

    payload = {
        "anthropic_api_key": "sk-ant-test",
        "google_api_key": "AIza-test",
        "openai_api_key": "sk-proj-test",
    }
    response = asyncio.run(api.handle_vault_push(DummyRequest(payload)))

    body = json.loads(response.text)
    assert response.status == 200
    assert body["success"] is True
    assert body["keys_updated"] == [
        "anthropic_api_key",
        "google_api_key",
        "openai_api_key",
    ]
    assert api.settings.ANTHROPIC_API_KEY == "sk-ant-test"
    assert api.settings.GOOGLE_API_KEY == "AIza-test"
    assert api.settings.GEMINI_API_KEY == "AIza-test"
    assert api.settings.OPENAI_API_KEY == "sk-proj-test"


def test_vault_push_anthropic_only_resets_claude_client():
    ai_backend = DummyAIBackend()
    api = _build_api(ai_backend=ai_backend)

    response = asyncio.run(api.handle_vault_push(DummyRequest({"anthropic_api_key": "sk-ant-new"})))

    body = json.loads(response.text)
    assert response.status == 200
    assert body["success"] is True
    assert body["keys_updated"] == ["anthropic_api_key"]
    assert api.settings.ANTHROPIC_API_KEY == "sk-ant-new"
    assert ai_backend._claude_client is None


def test_vault_push_unknown_key_is_ignored():
    api = _build_api()

    response = asyncio.run(api.handle_vault_push(DummyRequest({"unknown_key": "value"})))

    body = json.loads(response.text)
    assert response.status == 200
    assert body["success"] is True
    assert body["keys_updated"] == []


def test_vault_push_empty_body_returns_success_with_no_updates():
    api = _build_api()

    response = asyncio.run(api.handle_vault_push(DummyRequest({})))

    body = json.loads(response.text)
    assert response.status == 200
    assert body == {"success": True, "keys_updated": []}


def test_vault_mode_true_skips_start_preflight(monkeypatch):
    called = {"check_env": False}
    warnings = []

    def fake_getenv(key, default=""):
        if key == "VAULT_MODE":
            return "true"
        return default

    def fake_check_env():
        called["check_env"] = True

    monkeypatch.setattr(start.os, "getenv", fake_getenv)
    monkeypatch.setattr(start, "check_env", fake_check_env)
    monkeypatch.setattr(start.logger, "warning", warnings.append)

    start.run_preflight_checks()

    assert called["check_env"] is False
    assert any("VAULT_MODE enabled" in message for message in warnings)
