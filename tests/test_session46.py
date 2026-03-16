"""
Session 46 tests — Forge vault bridge (ADR-037 part 1) + qwen-flash mechanical roles.

Covers:
  - _load_forge_vault_keys() returns {} when proxy is unreachable
  - _key() resolution: vault value wins over env; falls back to env when vault empty
  - Settings.ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY / QWEN_API_KEY
    all resolve from vault values when provided
  - qwen-flash role chains: mcp and memory defaults
  - get_role_chain("mcp") and get_role_chain("memory") return correct chains
  - ROLE_MCP_CHAIN and ROLE_MEMORY_CHAIN settings defaults
  - get_primary_backend_for_role("mcp") == "qwen-flash"
  - get_primary_backend_for_role("memory") == "qwen-flash"
  - env override of ROLE_MCP_CHAIN respected
  - 13 role chains total (11 ADR-036 + 2 ADR-037) all present in settings defaults
"""

import sys
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_settings(**overrides):
    s = MagicMock()
    # API keys
    s.ANTHROPIC_API_KEY = ""
    s.GOOGLE_API_KEY = "test-google-key"
    s.QWEN_API_KEY = "test-qwen-key"
    s.OPENAI_API_KEY = ""
    # Qwen models
    s.QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    s.QWEN_FLASH_MODEL = "qwen3.5-flash-2026-02-23"
    s.QWEN_PLUS_MODEL = "qwen3.5-plus-2026-02-15"
    s.QWEN_CODER_FLASH_MODEL = "qwen3-coder-flash"
    s.QWEN_CODER_PLUS_MODEL = "qwen3-coder-plus-2025-07-22"
    s.QWEN_122B_MODEL = "qwen3.5-122b-a10b"
    # Gemini models
    s.GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
    s.GEMINI_FLASH_MODEL = "gemini-3-flash-preview"
    s.GEMINI_FLASH_LITE_MODEL = "gemini-3.1-flash-lite-preview"
    # Haiku
    s.CLAUDE_MODEL = "claude-sonnet-4-6"
    # Role chains — ADR-036
    s.ROLE_INTERVIEW_CHAIN = "claude,gemini,qwen-122b"
    s.ROLE_PLANNER_CHAIN = "gemini,claude,qwen-plus"
    s.ROLE_ORCHESTRATOR_CHAIN = "gemini,claude,qwen-plus"
    s.ROLE_CHUNKER_CHAIN = "gemini-flash,qwen-122b,claude-haiku"
    s.ROLE_AUDITOR_CHAIN = "gemini,qwen-122b,claude"
    s.ROLE_DEV_SWARM_T1_CHAIN = "gemini-flash-lite,qwen-coder-flash,claude-haiku"
    s.ROLE_DEV_SWARM_T2_CHAIN = "gemini-flash,qwen-122b,claude-haiku"
    s.ROLE_DEV_SWARM_T3_CHAIN = "claude,gemini,qwen-plus"
    s.ROLE_TEST_SWARM_T1_CHAIN = "gemini-flash,qwen-122b,claude-haiku"
    s.ROLE_TEST_SWARM_T2_CHAIN = "gemini,claude,qwen-plus"
    s.ROLE_BIG_FIXER_CHAIN = "gemini,claude,qwen-plus"
    # Role chains — ADR-037 mechanical
    s.ROLE_MCP_CHAIN = "qwen-flash,gemini-flash-lite,claude-haiku"
    s.ROLE_MEMORY_CHAIN = "qwen-flash,gemini-flash-lite,claude-haiku"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ─── Forge vault bridge tests ──────────────────────────────────────────────────

def test_load_forge_vault_keys_returns_empty_when_proxy_unreachable():
    """_load_forge_vault_keys() must return {} when the proxy is not running."""
    from config.settings import _load_forge_vault_keys
    # Use a port that's almost certainly closed
    result = _load_forge_vault_keys("http://localhost:19999")
    assert result == {}


def test_load_forge_vault_keys_returns_empty_on_bad_json():
    """Malformed proxy response → {} (defensive)."""
    from config.settings import _load_forge_vault_keys
    import urllib.request
    import urllib.error
    from unittest.mock import patch, MagicMock

    mock_resp = MagicMock()
    mock_resp.read.return_value = b"not json {"
    mock_resp.__enter__ = lambda s: mock_resp
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = _load_forge_vault_keys()
    assert result == {}


def test_load_forge_vault_keys_returns_keys_from_proxy():
    """Happy path: proxy returns valid keys → they are returned."""
    from config.settings import _load_forge_vault_keys
    from unittest.mock import patch, MagicMock

    payload = {"keys": {"anthropic_api_key": "sk-ant-test", "google_api_key": "AIza-test"}}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__ = lambda s: mock_resp
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = _load_forge_vault_keys()
    assert result["anthropic_api_key"] == "sk-ant-test"
    assert result["google_api_key"] == "AIza-test"


def test_key_resolver_vault_wins_over_env():
    """_key() should return the vault value when it's non-empty."""
    import config.settings as settings_mod
    original = settings_mod._forge_keys
    try:
        settings_mod._forge_keys = {"anthropic_api_key": "vault-key"}
        import os
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}):
            result = settings_mod._key("anthropic_api_key", "ANTHROPIC_API_KEY")
        assert result == "vault-key"
    finally:
        settings_mod._forge_keys = original


def test_key_resolver_falls_back_to_env_when_vault_empty():
    """_key() should return the env value when vault has empty string for the field."""
    import config.settings as settings_mod
    import os
    original = settings_mod._forge_keys
    try:
        settings_mod._forge_keys = {"anthropic_api_key": ""}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}):
            result = settings_mod._key("anthropic_api_key", "ANTHROPIC_API_KEY")
        assert result == "env-key"
    finally:
        settings_mod._forge_keys = original


def test_key_resolver_falls_back_to_env_when_vault_field_absent():
    """_key() should return the env value when the field is not in the vault dict."""
    import config.settings as settings_mod
    import os
    original = settings_mod._forge_keys
    try:
        settings_mod._forge_keys = {}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "only-env-key"}):
            result = settings_mod._key("anthropic_api_key", "ANTHROPIC_API_KEY")
        assert result == "only-env-key"
    finally:
        settings_mod._forge_keys = original


def test_key_resolver_uses_fallback_when_both_absent():
    """_key() should return the fallback default when neither vault nor env has the key."""
    import config.settings as settings_mod
    import os
    original = settings_mod._forge_keys
    try:
        settings_mod._forge_keys = {}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = settings_mod._key("anthropic_api_key", "ANTHROPIC_API_KEY", "hardcoded-default")
        assert result == "hardcoded-default"
    finally:
        settings_mod._forge_keys = original


# ─── Settings.ROLE_MCP_CHAIN / ROLE_MEMORY_CHAIN defaults ────────────────────

def test_settings_role_mcp_chain_default():
    """ROLE_MCP_CHAIN default should route qwen-flash first."""
    from config.settings import Settings
    import os
    os.environ.pop("ROLE_MCP_CHAIN", None)
    s = Settings()
    assert s.ROLE_MCP_CHAIN == "qwen-flash,gemini-flash-lite,claude-haiku"


def test_settings_role_memory_chain_default():
    """ROLE_MEMORY_CHAIN default should route qwen-flash first."""
    from config.settings import Settings
    import os
    os.environ.pop("ROLE_MEMORY_CHAIN", None)
    s = Settings()
    assert s.ROLE_MEMORY_CHAIN == "qwen-flash,gemini-flash-lite,claude-haiku"


def test_settings_role_mcp_chain_env_override():
    """ROLE_MCP_CHAIN should be overridable via env var."""
    from config.settings import Settings
    import os
    os.environ["ROLE_MCP_CHAIN"] = "claude-haiku,qwen-flash"
    try:
        s = Settings()
        assert s.ROLE_MCP_CHAIN == "claude-haiku,qwen-flash"
    finally:
        os.environ.pop("ROLE_MCP_CHAIN", None)


def test_settings_all_13_role_chains_present():
    """All 13 ROLE_*_CHAIN settings must be present on Settings."""
    from config.settings import Settings
    import os
    # Clear env overrides
    for attr in ["ROLE_MCP_CHAIN", "ROLE_MEMORY_CHAIN"]:
        os.environ.pop(attr, None)
    s = Settings()
    expected_attrs = [
        "ROLE_INTERVIEW_CHAIN", "ROLE_PLANNER_CHAIN", "ROLE_ORCHESTRATOR_CHAIN",
        "ROLE_CHUNKER_CHAIN", "ROLE_AUDITOR_CHAIN",
        "ROLE_DEV_SWARM_T1_CHAIN", "ROLE_DEV_SWARM_T2_CHAIN", "ROLE_DEV_SWARM_T3_CHAIN",
        "ROLE_TEST_SWARM_T1_CHAIN", "ROLE_TEST_SWARM_T2_CHAIN",
        "ROLE_BIG_FIXER_CHAIN",
        "ROLE_MCP_CHAIN", "ROLE_MEMORY_CHAIN",  # ADR-037
    ]
    for attr in expected_attrs:
        val = getattr(s, attr, None)
        assert val is not None and isinstance(val, str) and val.strip(), \
            f"{attr} is missing or empty on Settings"


# ─── ai_backend get_role_chain — mcp and memory ───────────────────────────────

def test_get_role_chain_mcp_default():
    """get_role_chain('mcp') should return qwen-flash first by default."""
    from core.ai_backend import AIBackend
    s = _make_settings()
    backend = AIBackend(settings=s)
    chain = backend.get_role_chain("mcp")
    assert chain == ["qwen-flash", "gemini-flash-lite", "claude-haiku"]


def test_get_role_chain_memory_default():
    """get_role_chain('memory') should return qwen-flash first by default."""
    from core.ai_backend import AIBackend
    s = _make_settings()
    backend = AIBackend(settings=s)
    chain = backend.get_role_chain("memory")
    assert chain == ["qwen-flash", "gemini-flash-lite", "claude-haiku"]


def test_get_primary_backend_for_role_mcp():
    """get_primary_backend_for_role('mcp') convenience method returns qwen-flash."""
    from core.ai_backend import AIBackend
    s = _make_settings()
    backend = AIBackend(settings=s)
    assert backend.get_primary_backend_for_role("mcp") == "qwen-flash"


def test_get_primary_backend_for_role_memory():
    """get_primary_backend_for_role('memory') convenience method returns qwen-flash."""
    from core.ai_backend import AIBackend
    s = _make_settings()
    backend = AIBackend(settings=s)
    assert backend.get_primary_backend_for_role("memory") == "qwen-flash"


def test_get_role_chain_mcp_env_override():
    """ROLE_MCP_CHAIN env override is reflected in get_role_chain."""
    from core.ai_backend import AIBackend
    s = _make_settings(ROLE_MCP_CHAIN="claude-haiku,qwen-flash")
    backend = AIBackend(settings=s)
    chain = backend.get_role_chain("mcp")
    assert chain[0] == "claude-haiku"


def test_get_role_chain_hyphen_alias_mcp():
    """'mcp' with hyphen variant 'mcp-server' should still resolve as unknown → claude fallback."""
    from core.ai_backend import AIBackend
    s = _make_settings()
    backend = AIBackend(settings=s)
    # 'mcp-server' is not a registered role — should fall back to ["claude"]
    chain = backend.get_role_chain("mcp-server")
    assert chain == ["claude"]


def test_get_role_chain_all_13_roles_resolve():
    """All 13 registered roles must resolve to non-empty chains."""
    from core.ai_backend import AIBackend
    s = _make_settings()
    backend = AIBackend(settings=s)
    roles = [
        "interview", "planner", "orchestrator", "chunker", "auditor",
        "dev_swarm_t1", "dev_swarm_t2", "dev_swarm_t3",
        "test_swarm_t1", "test_swarm_t2", "big_fixer",
        "mcp", "memory",
    ]
    for role in roles:
        chain = backend.get_role_chain(role)
        assert len(chain) >= 1, f"Role '{role}' returned empty chain"
        assert all(isinstance(b, str) and b.strip() for b in chain), \
            f"Role '{role}' chain has blank entries: {chain}"
