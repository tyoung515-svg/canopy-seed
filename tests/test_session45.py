"""
Session 45 tests — Benchmark-driven routing, Gemini 3.x Flash, Qwen Coder variants.

ADR-035: Updated SWARM_BACKEND_LITE/STANDARD defaults, GEMINI_FLASH_LITE_MODEL setting,
         qwen-coder-flash and qwen-coder-plus backends, corrected Qwen model IDs.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── path shim ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_settings(**overrides):
    """Minimal settings mock for ADR-035 tests."""
    settings = MagicMock()
    settings.ANTHROPIC_API_KEY = ""
    settings.GOOGLE_API_KEY = "test-google-key"
    settings.OPENAI_API_KEY = ""
    settings.QWEN_API_KEY = "test-qwen-key"
    settings.QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    settings.QWEN_FLASH_MODEL = "qwen3.5-flash-2026-02-23"
    settings.QWEN_PLUS_MODEL = "qwen3.5-plus-2026-02-15"
    settings.QWEN_CODER_MODEL = "qwen3-coder-plus-2025-07-22"
    settings.QWEN_CODER_FLASH_MODEL = "qwen3-coder-flash"
    settings.QWEN_CODER_PLUS_MODEL = "qwen3-coder-plus-2025-07-22"
    settings.QWEN_LOCAL_MODEL = "Qwen/Qwen3.5-35B-A3B"
    settings.GEMINI_FLASH_MODEL = "gemini-3-flash-preview"
    settings.GEMINI_FLASH_LITE_MODEL = "gemini-3.1-flash-lite-preview"
    settings.GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
    settings.LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
    settings.LMSTUDIO_MODEL = "local-model"
    settings.CLAUDE_MODEL = "claude-sonnet-4-6"
    settings.CLAUDE_ESCALATION_ENABLED = False
    settings.DEFAULT_AI_BACKEND = "claude"
    settings.OPENAI_MODEL = "gpt-5-mini"
    settings.OPENAI_CODEX_MODEL = "gpt-5.3-codex"
    settings.CODEX_REASONING_EFFORT = "medium"
    settings.WORKER_BASE_URL = ""
    settings.WORKER_MODEL = ""
    # ADR-035 new defaults
    settings.SWARM_BACKEND_LITE = "gemini-flash-lite"
    settings.SWARM_BACKEND_STANDARD = "gemini-flash"
    settings.SWARM_BACKEND_BRAIN = "claude"
    settings.SWARM_BACKEND_HEAVY = "claude"
    for k, v in overrides.items():
        setattr(settings, k, v)
    return settings


# ════════════════════════════════════════════════════════════════════════════
# ADR-035 — Settings defaults
# ════════════════════════════════════════════════════════════════════════════

def test_settings_swarm_backend_lite_default_is_gemini_flash_lite():
    """SWARM_BACKEND_LITE default is now gemini-flash-lite (ADR-035)."""
    import os
    # Unset env var so we get the class default
    env_backup = os.environ.pop("SWARM_BACKEND_LITE", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.SWARM_BACKEND_LITE == "gemini-flash-lite", (
            f"Expected gemini-flash-lite, got {s.SWARM_BACKEND_LITE}"
        )
    finally:
        if env_backup is not None:
            os.environ["SWARM_BACKEND_LITE"] = env_backup


def test_settings_swarm_backend_standard_default_is_gemini_flash():
    """SWARM_BACKEND_STANDARD default is now gemini-flash (ADR-035)."""
    import os
    env_backup = os.environ.pop("SWARM_BACKEND_STANDARD", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.SWARM_BACKEND_STANDARD == "gemini-flash", (
            f"Expected gemini-flash, got {s.SWARM_BACKEND_STANDARD}"
        )
    finally:
        if env_backup is not None:
            os.environ["SWARM_BACKEND_STANDARD"] = env_backup


def test_settings_gemini_flash_lite_model_default():
    """GEMINI_FLASH_LITE_MODEL default is gemini-3.1-flash-lite-preview."""
    import os
    env_backup = os.environ.pop("GEMINI_FLASH_LITE_MODEL", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.GEMINI_FLASH_LITE_MODEL == "gemini-3.1-flash-lite-preview"
    finally:
        if env_backup is not None:
            os.environ["GEMINI_FLASH_LITE_MODEL"] = env_backup


def test_settings_gemini_flash_model_default():
    """GEMINI_FLASH_MODEL default is gemini-3-flash-preview."""
    import os
    env_backup = os.environ.pop("GEMINI_FLASH_MODEL", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.GEMINI_FLASH_MODEL == "gemini-3-flash-preview"
    finally:
        if env_backup is not None:
            os.environ["GEMINI_FLASH_MODEL"] = env_backup


def test_settings_qwen_flash_model_uses_versioned_id():
    """QWEN_FLASH_MODEL default uses versioned benchmark string."""
    import os
    env_backup = os.environ.pop("QWEN_FLASH_MODEL", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.QWEN_FLASH_MODEL == "qwen3.5-flash-2026-02-23"
    finally:
        if env_backup is not None:
            os.environ["QWEN_FLASH_MODEL"] = env_backup


def test_settings_qwen_plus_model_uses_versioned_id():
    """QWEN_PLUS_MODEL default uses versioned benchmark string."""
    import os
    env_backup = os.environ.pop("QWEN_PLUS_MODEL", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.QWEN_PLUS_MODEL == "qwen3.5-plus-2026-02-15"
    finally:
        if env_backup is not None:
            os.environ["QWEN_PLUS_MODEL"] = env_backup


def test_settings_qwen_coder_plus_model_default():
    """QWEN_CODER_PLUS_MODEL default is qwen3-coder-plus-2025-07-22."""
    import os
    env_backup = os.environ.pop("QWEN_CODER_PLUS_MODEL", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.QWEN_CODER_PLUS_MODEL == "qwen3-coder-plus-2025-07-22"
    finally:
        if env_backup is not None:
            os.environ["QWEN_CODER_PLUS_MODEL"] = env_backup


def test_settings_qwen_coder_flash_model_default():
    """QWEN_CODER_FLASH_MODEL default is qwen3-coder-flash."""
    import os
    env_backup = os.environ.pop("QWEN_CODER_FLASH_MODEL", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.QWEN_CODER_FLASH_MODEL == "qwen3-coder-flash"
    finally:
        if env_backup is not None:
            os.environ["QWEN_CODER_FLASH_MODEL"] = env_backup


def test_settings_qwen_coder_alias_points_to_plus():
    """QWEN_CODER_MODEL (legacy alias) resolves to the plus model ID."""
    import os
    env_backup = os.environ.pop("QWEN_CODER_MODEL", None)
    try:
        from config.settings import Settings
        s = Settings()
        assert s.QWEN_CODER_MODEL == "qwen3-coder-plus-2025-07-22"
    finally:
        if env_backup is not None:
            os.environ["QWEN_CODER_MODEL"] = env_backup


# ════════════════════════════════════════════════════════════════════════════
# ADR-035 — AgentPool tier routing with new defaults
# ════════════════════════════════════════════════════════════════════════════

def test_tier_routing_new_defaults():
    """With ADR-035 defaults, lite→gemini-flash-lite, standard→gemini-flash."""
    from core.agent_pool import AgentPool
    settings = _make_settings()
    pool = AgentPool(settings=settings)
    assert pool._select_backend_for_tier("lite") == "gemini-flash-lite"
    assert pool._select_backend_for_tier("low") == "gemini-flash-lite"
    assert pool._select_backend_for_tier("simple") == "gemini-flash-lite"
    assert pool._select_backend_for_tier("standard") == "gemini-flash"
    assert pool._select_backend_for_tier("elevated") == "gemini-flash"
    assert pool._select_backend_for_tier("brain") == "claude"
    assert pool._select_backend_for_tier("heavy") == "claude"


def test_tier_routing_codex_override_still_works():
    """Users can still override back to openai-codex via settings."""
    from core.agent_pool import AgentPool
    settings = _make_settings(
        SWARM_BACKEND_LITE="openai-codex",
        SWARM_BACKEND_STANDARD="openai-codex",
    )
    pool = AgentPool(settings=settings)
    assert pool._select_backend_for_tier("lite") == "openai-codex"
    assert pool._select_backend_for_tier("standard") == "openai-codex"


# ════════════════════════════════════════════════════════════════════════════
# ADR-035 — AIBackend gemini-flash-lite uses GEMINI_FLASH_LITE_MODEL
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_gemini_flash_lite_uses_flash_lite_model_setting():
    """gemini-flash-lite backend passes GEMINI_FLASH_LITE_MODEL to _gemini_complete."""
    from core.ai_backend import AIBackend
    settings = _make_settings()
    backend = AIBackend(settings=settings)

    captured_model = {}

    async def fake_gemini_complete(system, message, history, **kwargs):
        captured_model["model"] = kwargs.get("model")
        return "ok"

    backend._gemini_complete = fake_gemini_complete
    await backend.complete(
        system="sys", message="hello", history=[],
        backend="gemini-flash-lite",
    )
    assert captured_model["model"] == "gemini-3.1-flash-lite-preview"


@pytest.mark.asyncio
async def test_gemini_flash_uses_flash_model_setting():
    """gemini-flash backend passes GEMINI_FLASH_MODEL to _gemini_complete."""
    from core.ai_backend import AIBackend
    settings = _make_settings()
    backend = AIBackend(settings=settings)

    captured_model = {}

    async def fake_gemini_complete(system, message, history, **kwargs):
        captured_model["model"] = kwargs.get("model")
        return "ok"

    backend._gemini_complete = fake_gemini_complete
    await backend.complete(
        system="sys", message="hello", history=[],
        backend="gemini-flash",
    )
    assert captured_model["model"] == "gemini-3-flash-preview"


@pytest.mark.asyncio
async def test_gemini_flash_lite_override_via_setting():
    """GEMINI_FLASH_LITE_MODEL setting overrides the default model ID."""
    from core.ai_backend import AIBackend
    settings = _make_settings(GEMINI_FLASH_LITE_MODEL="gemini-3.2-flash-lite-preview")
    backend = AIBackend(settings=settings)

    captured_model = {}

    async def fake_gemini_complete(system, message, history, **kwargs):
        captured_model["model"] = kwargs.get("model")
        return "ok"

    backend._gemini_complete = fake_gemini_complete
    await backend.complete(
        system="sys", message="hello", history=[],
        backend="gemini-flash-lite",
    )
    assert captured_model["model"] == "gemini-3.2-flash-lite-preview"


# ════════════════════════════════════════════════════════════════════════════
# ADR-035 — qwen-coder-flash and qwen-coder-plus backends
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_qwen_coder_flash_dispatches_to_coder_flash_model():
    """qwen-coder-flash backend uses QWEN_CODER_FLASH_MODEL."""
    from core.ai_backend import AIBackend
    settings = _make_settings()
    backend = AIBackend(settings=settings)

    captured_model = {}

    async def fake_qwen_complete(system, message, history, **kwargs):
        captured_model["model"] = kwargs.get("model")
        return "ok"

    backend._qwen_complete = fake_qwen_complete
    await backend.complete(
        system="sys", message="hello", history=[],
        backend="qwen-coder-flash",
    )
    assert captured_model["model"] == "qwen3-coder-flash"


@pytest.mark.asyncio
async def test_qwen_coder_plus_dispatches_to_coder_plus_model():
    """qwen-coder-plus backend uses QWEN_CODER_PLUS_MODEL."""
    from core.ai_backend import AIBackend
    settings = _make_settings()
    backend = AIBackend(settings=settings)

    captured_model = {}

    async def fake_qwen_complete(system, message, history, **kwargs):
        captured_model["model"] = kwargs.get("model")
        return "ok"

    backend._qwen_complete = fake_qwen_complete
    await backend.complete(
        system="sys", message="hello", history=[],
        backend="qwen-coder-plus",
    )
    assert captured_model["model"] == "qwen3-coder-plus-2025-07-22"


@pytest.mark.asyncio
async def test_qwen_coder_legacy_alias_uses_plus_model():
    """qwen-coder (legacy alias) dispatches to the plus model."""
    from core.ai_backend import AIBackend
    settings = _make_settings()
    backend = AIBackend(settings=settings)

    captured_model = {}

    async def fake_qwen_complete(system, message, history, **kwargs):
        captured_model["model"] = kwargs.get("model")
        return "ok"

    backend._qwen_complete = fake_qwen_complete
    await backend.complete(
        system="sys", message="hello", history=[],
        backend="qwen-coder",
    )
    assert captured_model["model"] == "qwen3-coder-plus-2025-07-22"


@pytest.mark.asyncio
async def test_qwen_flash_uses_versioned_model_id():
    """qwen-flash backend uses the versioned qwen3.5-flash model ID."""
    from core.ai_backend import AIBackend
    settings = _make_settings()
    backend = AIBackend(settings=settings)

    captured_model = {}

    async def fake_qwen_complete(system, message, history, **kwargs):
        captured_model["model"] = kwargs.get("model")
        return "ok"

    backend._qwen_complete = fake_qwen_complete
    await backend.complete(
        system="sys", message="hello", history=[],
        backend="qwen-flash",
    )
    assert captured_model["model"] == "qwen3.5-flash-2026-02-23"
