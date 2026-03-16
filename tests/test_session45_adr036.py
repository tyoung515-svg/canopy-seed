"""
Session 45 / ADR-036 tests — Role-based backend chains, qwen-122b backend.

ADR-036: Corrected benchmark analysis, 11 named role chains replacing the
         coarse SWARM_BACKEND_LITE/STANDARD/BRAIN/HEAVY system.
         qwen-122b backend for qwen3.5-122b-a10b (MoE, 9.675 avg on std tier).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_settings(**overrides):
    s = MagicMock()
    s.ANTHROPIC_API_KEY = ""
    s.GOOGLE_API_KEY = "test-google-key"
    s.QWEN_API_KEY = "test-qwen-key"
    s.QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    s.QWEN_122B_MODEL = "qwen3.5-122b-a10b"
    s.QWEN_FLASH_MODEL = "qwen3.5-flash-2026-02-23"
    s.QWEN_PLUS_MODEL = "qwen3.5-plus-2026-02-15"
    s.QWEN_CODER_FLASH_MODEL = "qwen3-coder-flash"
    s.QWEN_CODER_PLUS_MODEL = "qwen3-coder-plus-2025-07-22"
    s.QWEN_CODER_MODEL = "qwen3-coder-plus-2025-07-22"
    s.QWEN_LOCAL_MODEL = "Qwen/Qwen3.5-35B-A3B"
    s.GEMINI_FLASH_MODEL = "gemini-3-flash-preview"
    s.GEMINI_FLASH_LITE_MODEL = "gemini-3.1-flash-lite-preview"
    s.GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
    s.CLAUDE_MODEL = "claude-sonnet-4-6"
    s.CLAUDE_ESCALATION_ENABLED = False
    s.DEFAULT_AI_BACKEND = "claude"
    s.LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
    s.LMSTUDIO_MODEL = "local-model"
    s.OPENAI_API_KEY = ""
    s.OPENAI_CODEX_MODEL = "gpt-5.3-codex"
    s.CODEX_REASONING_EFFORT = "medium"
    s.WORKER_BASE_URL = ""
    s.WORKER_MODEL = ""
    # ADR-036 role chains
    s.ROLE_INTERVIEW_CHAIN    = "claude,gemini,qwen-122b"
    s.ROLE_PLANNER_CHAIN      = "gemini,claude,qwen-plus"
    s.ROLE_ORCHESTRATOR_CHAIN = "gemini,claude,qwen-plus"
    s.ROLE_CHUNKER_CHAIN      = "gemini-flash,qwen-122b,claude-haiku"
    s.ROLE_AUDITOR_CHAIN      = "gemini,qwen-122b,claude"
    s.ROLE_DEV_SWARM_T1_CHAIN = "gemini-flash-lite,qwen-coder-flash,claude-haiku"
    s.ROLE_DEV_SWARM_T2_CHAIN = "gemini-flash,qwen-122b,claude-haiku"
    s.ROLE_DEV_SWARM_T3_CHAIN = "claude,gemini,qwen-plus"
    s.ROLE_TEST_SWARM_T1_CHAIN = "gemini-flash,qwen-122b,claude-haiku"
    s.ROLE_TEST_SWARM_T2_CHAIN = "gemini,claude,qwen-plus"
    s.ROLE_BIG_FIXER_CHAIN    = "gemini,claude,qwen-plus"
    # Legacy flat routing (kept for backward compat)
    s.SWARM_BACKEND_LITE     = "gemini-flash-lite"
    s.SWARM_BACKEND_STANDARD = "gemini-flash"
    s.SWARM_BACKEND_BRAIN    = "claude"
    s.SWARM_BACKEND_HEAVY    = "claude"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ════════════════════════════════════════════════════════════════════════════
# Settings — QWEN_122B_MODEL default
# ════════════════════════════════════════════════════════════════════════════

def test_settings_qwen_122b_model_default():
    """QWEN_122B_MODEL default is qwen3.5-122b-a10b."""
    import os
    env_backup = os.environ.pop("QWEN_122B_MODEL", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.QWEN_122B_MODEL == "qwen3.5-122b-a10b"
    finally:
        if env_backup is not None:
            os.environ["QWEN_122B_MODEL"] = env_backup


def test_settings_role_interview_chain_default():
    """ROLE_INTERVIEW_CHAIN default starts with claude (highest quality)."""
    import os
    env_backup = os.environ.pop("ROLE_INTERVIEW_CHAIN", None)
    try:
        from config.settings import Settings
        s = Settings()
        chain = s.ROLE_INTERVIEW_CHAIN.split(",")
        assert chain[0].strip() == "claude"
        assert len(chain) == 3
    finally:
        if env_backup is not None:
            os.environ["ROLE_INTERVIEW_CHAIN"] = env_backup


def test_settings_role_dev_swarm_t1_chain_default():
    """ROLE_DEV_SWARM_T1_CHAIN default starts with gemini-flash-minimal (Session 58: swarm = MINIMAL thinking)."""
    import os
    env_backup = os.environ.pop("ROLE_DEV_SWARM_T1_CHAIN", None)
    try:
        from config.settings import Settings
        s = Settings()
        chain = s.ROLE_DEV_SWARM_T1_CHAIN.split(",")
        assert chain[0].strip() == "gemini-flash-minimal"
    finally:
        if env_backup is not None:
            os.environ["ROLE_DEV_SWARM_T1_CHAIN"] = env_backup


def test_settings_role_dev_swarm_t2_chain_default():
    """ROLE_DEV_SWARM_T2_CHAIN default starts with gemini-flash-minimal (Session 58: swarm = MINIMAL thinking)."""
    import os
    env_backup = os.environ.pop("ROLE_DEV_SWARM_T2_CHAIN", None)
    try:
        from config.settings import Settings
        s = Settings()
        chain = s.ROLE_DEV_SWARM_T2_CHAIN.split(",")
        assert chain[0].strip() == "gemini-flash-minimal"
    finally:
        if env_backup is not None:
            os.environ["ROLE_DEV_SWARM_T2_CHAIN"] = env_backup


def test_settings_role_dev_swarm_t3_chain_default():
    """ROLE_DEV_SWARM_T3_CHAIN default starts with claude (complex tasks)."""
    import os
    env_backup = os.environ.pop("ROLE_DEV_SWARM_T3_CHAIN", None)
    try:
        from config.settings import Settings
        s = Settings()
        chain = s.ROLE_DEV_SWARM_T3_CHAIN.split(",")
        assert chain[0].strip() == "claude"
    finally:
        if env_backup is not None:
            os.environ["ROLE_DEV_SWARM_T3_CHAIN"] = env_backup


# ════════════════════════════════════════════════════════════════════════════
# AIBackend.get_role_chain() and get_primary_backend_for_role()
# ════════════════════════════════════════════════════════════════════════════

def test_get_role_chain_returns_ordered_list():
    """get_role_chain returns a 3-element list in correct order."""
    from core.ai_backend import AIBackend
    backend = AIBackend(settings=_make_settings())
    chain = backend.get_role_chain("interview")
    assert chain == ["claude", "gemini", "qwen-122b"]


def test_get_role_chain_all_11_roles():
    """All 11 roles return a non-empty chain."""
    from core.ai_backend import AIBackend
    backend = AIBackend(settings=_make_settings())
    roles = [
        "interview", "planner", "orchestrator", "chunker", "auditor",
        "dev_swarm_t1", "dev_swarm_t2", "dev_swarm_t3",
        "test_swarm_t1", "test_swarm_t2", "big_fixer",
    ]
    for role in roles:
        chain = backend.get_role_chain(role)
        assert len(chain) >= 1, f"Role {role} returned empty chain"
        assert all(isinstance(b, str) and b for b in chain), f"Role {role} has empty backend in chain"


def test_get_role_chain_hyphen_alias():
    """Roles with hyphens (dev-swarm-t1) resolve the same as underscores."""
    from core.ai_backend import AIBackend
    backend = AIBackend(settings=_make_settings())
    assert backend.get_role_chain("dev-swarm-t1") == backend.get_role_chain("dev_swarm_t1")


def test_get_role_chain_unknown_role_returns_claude():
    """Unknown role name falls back to ['claude']."""
    from core.ai_backend import AIBackend
    backend = AIBackend(settings=_make_settings())
    chain = backend.get_role_chain("nonexistent_role")
    assert chain == ["claude"]


def test_get_primary_backend_for_role():
    """get_primary_backend_for_role returns the first element."""
    from core.ai_backend import AIBackend
    backend = AIBackend(settings=_make_settings())
    assert backend.get_primary_backend_for_role("interview") == "claude"
    assert backend.get_primary_backend_for_role("dev_swarm_t1") == "gemini-flash-lite"
    assert backend.get_primary_backend_for_role("dev_swarm_t2") == "gemini-flash"
    assert backend.get_primary_backend_for_role("dev_swarm_t3") == "claude"
    assert backend.get_primary_backend_for_role("auditor") == "gemini"
    assert backend.get_primary_backend_for_role("big_fixer") == "gemini"


def test_get_role_chain_respects_env_override():
    """Role chain uses ROLE_*_CHAIN setting when overridden."""
    from core.ai_backend import AIBackend
    s = _make_settings(ROLE_INTERVIEW_CHAIN="qwen-122b,claude,gemini")
    backend = AIBackend(settings=s)
    chain = backend.get_role_chain("interview")
    assert chain == ["qwen-122b", "claude", "gemini"]


# ════════════════════════════════════════════════════════════════════════════
# AgentPool._select_backend_for_tier with ADR-036 role chains
# ════════════════════════════════════════════════════════════════════════════

def test_tier_routing_t1_uses_dev_swarm_t1_primary():
    """T1/lite tier routes to primary of ROLE_DEV_SWARM_T1_CHAIN."""
    from core.agent_pool import AgentPool
    pool = AgentPool(settings=_make_settings())
    assert pool._select_backend_for_tier("t1") == "gemini-flash-lite"
    assert pool._select_backend_for_tier("lite") == "gemini-flash-lite"
    assert pool._select_backend_for_tier("simple") == "gemini-flash-lite"


def test_tier_routing_t2_uses_dev_swarm_t2_primary():
    """T2/standard tier routes to primary of ROLE_DEV_SWARM_T2_CHAIN."""
    from core.agent_pool import AgentPool
    pool = AgentPool(settings=_make_settings())
    assert pool._select_backend_for_tier("t2") == "gemini-flash"
    assert pool._select_backend_for_tier("standard") == "gemini-flash"
    assert pool._select_backend_for_tier("elevated") == "gemini-flash"


def test_tier_routing_t3_uses_dev_swarm_t3_primary():
    """T3/brain tier routes to primary of ROLE_DEV_SWARM_T3_CHAIN."""
    from core.agent_pool import AgentPool
    pool = AgentPool(settings=_make_settings())
    assert pool._select_backend_for_tier("t3") == "claude"
    assert pool._select_backend_for_tier("brain") == "claude"
    assert pool._select_backend_for_tier("heavy") == "claude"


def test_tier_routing_no_settings_fallback():
    """Without settings, legacy openai-codex / claude fallbacks apply."""
    from core.agent_pool import AgentPool
    pool = AgentPool(settings=None)
    assert pool._select_backend_for_tier("lite") == "openai-codex"
    assert pool._select_backend_for_tier("standard") == "openai-codex"
    assert pool._select_backend_for_tier("brain") == "claude"
    assert pool._select_backend_for_tier("escalate") == "claude"


# ════════════════════════════════════════════════════════════════════════════
# AIBackend — qwen-122b backend dispatch
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_qwen_122b_dispatches_to_qwen_122b_model():
    """qwen-122b backend uses QWEN_122B_MODEL setting."""
    from core.ai_backend import AIBackend
    settings = _make_settings()
    backend = AIBackend(settings=settings)

    captured = {}

    async def fake_qwen_complete(system, message, history, **kwargs):
        captured["model"] = kwargs.get("model")
        return "ok"

    backend._qwen_complete = fake_qwen_complete
    await backend.complete(system="sys", message="hello", history=[], backend="qwen-122b")
    assert captured["model"] == "qwen3.5-122b-a10b"


@pytest.mark.asyncio
async def test_qwen_122b_model_override():
    """QWEN_122B_MODEL setting overrides the default model ID."""
    from core.ai_backend import AIBackend
    settings = _make_settings(QWEN_122B_MODEL="qwen3.5-200b-a20b")
    backend = AIBackend(settings=settings)

    captured = {}

    async def fake_qwen_complete(system, message, history, **kwargs):
        captured["model"] = kwargs.get("model")
        return "ok"

    backend._qwen_complete = fake_qwen_complete
    await backend.complete(system="sys", message="hello", history=[], backend="qwen-122b")
    assert captured["model"] == "qwen3.5-200b-a20b"
