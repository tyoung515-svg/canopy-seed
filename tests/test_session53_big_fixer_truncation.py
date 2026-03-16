"""
Session 53 tests — Big Fixer hardening (updated for Session 56 rewrite).

Session 53 originally tested a token-retry + JSON-blob architecture that was
fully replaced in Session 56 (ADR-030 native tool calling rewrite). These tests
have been updated to verify the new behaviour:

  - Big Fixer uses gemini-customtools as primary backend (not gemini-flash)
  - Big Fixer passes tools=TOOL_DEFINITIONS and tool_dispatcher to complete()
  - Big Fixer uses max_tokens=65536 (Gemini 3.x ceiling) — not 16384
  - No monolithic JSON blob parsing; no token retry loop
  - gemini backend is the fallback (not claude-haiku)
  - _run_big_fixer always returns (SwarmSummary, list) 2-tuple

Legacy constants _BF_BASE_TOKENS and the two-level retry pattern were removed
in Session 56. Tests that checked for those constants are replaced here.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool(tmp_path: Path):
    from core.agent_pool import AgentPool
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value="BIG_FIXER_DONE")
    # Session 58: _run_big_fixer now calls get_role_chain("big_fixer") instead of
    # hardcoded ("gemini-customtools", "gemini"). Configure the mock so the loop runs.
    mock_ai.get_role_chain = MagicMock(
        return_value=["gemini-customtools", "gemini-flash-high", "claude"]
    )
    pool = AgentPool(ai_backend=mock_ai)
    pool._sse_broadcast = None
    pool._broadcast = AsyncMock()
    return pool, mock_ai


def _make_failing(failed: int = 1, total: int = 10):
    m = MagicMock()
    m.failed = failed
    m.passed = total - failed
    m.total = total
    m.results = []
    m.install_hard_fail = False
    return m


def _make_clean(total: int = 10):
    m = MagicMock()
    m.failed = 0
    m.passed = total
    m.total = total
    m.results = []
    m.install_hard_fail = False
    return m


# ---------------------------------------------------------------------------
# Test 1 — primary backend is gemini-customtools (replaced: gemini-flash check)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_big_fixer_primary_backend_is_gemini_customtools(tmp_path):
    """Session 56: Big Fixer must use gemini-customtools as primary backend,
    not gemini-flash (Session 52) or openai-codex (Session 51)."""
    pool, mock_ai = _make_pool(tmp_path)

    calls = []

    async def _side_effect(**kwargs):
        calls.append(kwargs.get("backend"))
        return "BIG_FIXER_DONE"

    mock_ai.complete = AsyncMock(side_effect=_side_effect)

    with patch.object(pool, "_rerun_tests", AsyncMock(return_value=_make_clean())), \
         patch.object(pool, "_collect_source_tree", MagicMock(return_value={})), \
         patch.object(pool, "_collect_test_output", MagicMock(return_value="FAILED")):
        await pool._run_big_fixer(
            swarm_summary=_make_failing(),
            export_dir=tmp_path,
            project_context={"description": "test"},
            session_id="sess-primary-backend",
            repair_history=[],
        )

    assert "gemini-customtools" in calls, (
        f"gemini-customtools must be tried as primary backend; got: {calls}"
    )
    assert "gemini-flash" not in calls, (
        f"gemini-flash must NOT be used as Big Fixer primary backend (Session 56); got: {calls}"
    )


# ---------------------------------------------------------------------------
# Test 2 — max_tokens is 65536 (replaced: 16384 base budget check)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_big_fixer_uses_65536_max_tokens(tmp_path):
    """Session 56: Big Fixer must request 65536 max_tokens (Gemini 3.x ceiling).
    The old 16384/_BF_BASE_TOKENS pattern is replaced by the tool-calling loop."""
    pool, mock_ai = _make_pool(tmp_path)

    captured = {}

    async def _side_effect(**kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        captured["backend"] = kwargs.get("backend")
        return "BIG_FIXER_DONE"

    mock_ai.complete = AsyncMock(side_effect=_side_effect)

    with patch.object(pool, "_rerun_tests", AsyncMock(return_value=_make_clean())), \
         patch.object(pool, "_collect_source_tree", MagicMock(return_value={})), \
         patch.object(pool, "_collect_test_output", MagicMock(return_value="FAILED")):
        await pool._run_big_fixer(
            swarm_summary=_make_failing(),
            export_dir=tmp_path,
            project_context={"description": "test"},
            session_id="sess-65536",
            repair_history=[],
        )

    assert captured.get("max_tokens") == 65536, (
        f"Big Fixer must use max_tokens=65536 (Gemini 3.x ceiling); got: {captured.get('max_tokens')}"
    )


# ---------------------------------------------------------------------------
# Test 3 — gemini is fallback (replaced: claude-haiku fallback check)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_big_fixer_falls_back_to_flash_high_not_gemini(tmp_path):
    """Session 58: When gemini-customtools fails, fallback must be gemini-flash-high.
    The hardcoded ('gemini-customtools', 'gemini') tuple is replaced by get_role_chain()."""
    pool, mock_ai = _make_pool(tmp_path)

    # Wire get_role_chain to return the Session 58 Big Fixer chain
    mock_ai.get_role_chain = MagicMock(
        return_value=["gemini-customtools", "gemini-flash-high", "claude"]
    )

    calls = []

    async def _side_effect(**kwargs):
        backend = kwargs.get("backend")
        calls.append(backend)
        if backend == "gemini-customtools":
            raise RuntimeError("customtools unavailable")
        return "BIG_FIXER_DONE"

    mock_ai.complete = AsyncMock(side_effect=_side_effect)

    with patch.object(pool, "_rerun_tests", AsyncMock(return_value=_make_clean())), \
         patch.object(pool, "_collect_source_tree", MagicMock(return_value={})), \
         patch.object(pool, "_collect_test_output", MagicMock(return_value="FAILED")):
        await pool._run_big_fixer(
            swarm_summary=_make_failing(),
            export_dir=tmp_path,
            project_context={"description": "test"},
            session_id="sess-fallback",
            repair_history=[],
        )

    assert "gemini-flash-high" in calls, (
        f"gemini-flash-high must be tried as fallback after gemini-customtools fails; got: {calls}"
    )
    assert "claude-haiku" not in calls, (
        f"claude-haiku must NOT appear in the fallback chain; got: {calls}"
    )
    assert "gemini" not in calls, (
        f"plain 'gemini' must NOT appear — replaced by gemini-flash-high (Session 58); got: {calls}"
    )


# ---------------------------------------------------------------------------
# Test 4 — no JSON blob (replaced: _BF_BASE_TOKENS * 2 source check)
# ---------------------------------------------------------------------------

def test_big_fixer_source_has_no_json_blob_pattern():
    """Session 56: _run_big_fixer must NOT contain old JSON blob or token retry
    constants. These were replaced by the native tool-calling loop."""
    import inspect
    from core.agent_pool import AgentPool

    src = inspect.getsource(AgentPool._run_big_fixer)

    # Old session-53 constants must be gone
    assert "_BF_BASE_TOKENS" not in src, (
        "_BF_BASE_TOKENS must be removed (Session 56 rewrote the budget pattern)"
    )
    # Old JSON blob parsing must be gone
    assert '"edits"' not in src, (
        'JSON blob key "edits" must be gone — Big Fixer no longer emits a blob'
    )
    # New tool-calling pattern must be present
    assert "tool_dispatcher" in src, (
        "tool_dispatcher kwarg must appear in _run_big_fixer (Session 56)"
    )
    assert "TOOL_DEFINITIONS" in src, (
        "TOOL_DEFINITIONS must be passed to complete() in _run_big_fixer (Session 56)"
    )


# ---------------------------------------------------------------------------
# Test 5 — gemini-customtools in source (replaced: gemini-flash source check)
# ---------------------------------------------------------------------------

def test_big_fixer_source_references_gemini_customtools():
    """Session 58: _run_big_fixer must use get_role_chain("big_fixer") for its backend
    chain rather than a hardcoded ("gemini-customtools", "gemini") tuple.
    gemini-customtools remains the primary backend via the default chain, but the
    source no longer contains the literal string — it uses the role-chain system."""
    import inspect
    from core.agent_pool import AgentPool

    src = inspect.getsource(AgentPool._run_big_fixer)

    # Session 58: dynamic role-chain lookup replaces hardcoded tuple
    assert 'get_role_chain' in src, (
        "_run_big_fixer must use get_role_chain() — hardcoded tuple removed (Session 58)"
    )
    assert '"big_fixer"' in src, (
        '_run_big_fixer must pass the "big_fixer" role key to get_role_chain'
    )
    assert '"gemini-flash"' not in src, (
        "gemini-flash must NOT appear in _run_big_fixer source (Session 56 replaced it)"
    )
