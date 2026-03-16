"""
Session 47 tests — Orchestrator role chain wiring.

Covers:
  - decompose() tries each backend in ROLE_ORCHESTRATOR_CHAIN in order
  - decompose() falls back to next backend when primary returns empty tasks
  - decompose() falls back to next backend when primary raises an exception
  - decompose() returns fallback subtask when all backends exhausted
  - repair_audit() tries each backend in ROLE_AUDITOR_CHAIN in order
  - repair_audit() falls back on exception
  - repair_audit() returns [] when all backends exhausted
  - ROLE_ORCHESTRATOR_CHAIN default is gemini-flash,gemini,claude-haiku (Session 56: qwen-122b retired)
  - get_role_chain("orchestrator") reflects updated default
"""

import os
import sys
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ─── helpers ──────────────────────────────────────────────────────────────────

MINIMAL_SUBTASK = {
    "subtasks": [
        {
            "title": "Implement CLI",
            "description": "Write the main CLI entry point",
            "target_files": ["myapp/cli.py"],
            "domain_layer": "backend",
            "ambiguity_score": 1,
            "dependency_score": 1,
            "tier": 2,
        }
    ]
}

MINIMAL_REPAIR_SUBTASK = {
    "subtasks": [
        {
            "title": "Fix import error",
            "description": "Resolve missing module",
            "target_files": ["myapp/utils.py"],
            "domain_layer": "backend",
            "ambiguity_score": 1,
            "dependency_score": 1,
            "tier": 1,
            "fix_description": "Add missing import",
            "complexity": "mechanical",
        }
    ]
}

PROJECT_CONTEXT = {
    "project_name": "Test App",
    "description": "A simple test application",
    "goals": ["goal 1"],
    "constraints": [],
    "tech_preferences": {},
}


def _make_ai(chain_map=None):
    """Return a mock AIBackend with configurable role chains."""
    ai = MagicMock()
    ai.settings = MagicMock()

    def _get_role_chain(role):
        # Session 56: qwen-122b retired — gemini replaces it in orchestrator/auditor chains
        defaults = {
            "orchestrator": ["gemini-flash", "gemini", "claude-haiku"],
            "auditor":      ["gemini", "gemini-flash", "claude"],
        }
        return (chain_map or {}).get(role, defaults.get(role, ["claude"]))

    ai.get_role_chain = _get_role_chain
    ai.complete = AsyncMock(return_value=json.dumps(MINIMAL_SUBTASK))
    return ai


# ─── decompose() tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_decompose_uses_primary_backend():
    """decompose() calls the first backend in the orchestrator chain."""
    from core.orchestrator import ProjectOrchestrator
    ai = _make_ai()
    orch = ProjectOrchestrator(ai)
    result = await orch.decompose(PROJECT_CONTEXT)
    assert len(result) == 1
    first_call_backend = ai.complete.call_args_list[0].kwargs.get("backend") or \
                         ai.complete.call_args_list[0].args[0] if ai.complete.call_args_list[0].args else \
                         ai.complete.call_args_list[0].kwargs["backend"]
    # backend kwarg
    assert ai.complete.call_args_list[0].kwargs["backend"] == "gemini-flash"


@pytest.mark.asyncio
async def test_decompose_falls_back_on_exception():
    """decompose() skips to the next backend when the current one raises."""
    from core.orchestrator import ProjectOrchestrator
    ai = _make_ai()
    # First call raises, second succeeds
    ai.complete = AsyncMock(side_effect=[
        RuntimeError("primary down"),
        json.dumps(MINIMAL_SUBTASK),
    ])
    orch = ProjectOrchestrator(ai)
    result = await orch.decompose(PROJECT_CONTEXT)
    assert len(result) == 1
    assert ai.complete.call_count == 2
    # Session 56: qwen-122b retired; gemini is now the second entry in orchestrator chain
    assert ai.complete.call_args_list[1].kwargs["backend"] == "gemini"


@pytest.mark.asyncio
async def test_decompose_falls_back_on_empty_tasks():
    """decompose() tries next backend when current backend returns 0 tasks."""
    from core.orchestrator import ProjectOrchestrator
    ai = _make_ai()
    empty_response = json.dumps({"subtasks": []})
    ai.complete = AsyncMock(side_effect=[
        empty_response,                   # primary: 0 tasks
        json.dumps(MINIMAL_SUBTASK),      # fallback: 1 task
    ])
    orch = ProjectOrchestrator(ai)
    result = await orch.decompose(PROJECT_CONTEXT)
    assert len(result) == 1
    assert ai.complete.call_count == 2


@pytest.mark.asyncio
async def test_decompose_returns_fallback_task_when_all_exhausted():
    """decompose() returns a single fallback subtask when all backends fail."""
    from core.orchestrator import ProjectOrchestrator
    ai = _make_ai()
    ai.complete = AsyncMock(side_effect=RuntimeError("all down"))
    orch = ProjectOrchestrator(ai)
    result = await orch.decompose(PROJECT_CONTEXT)
    assert len(result) == 1
    assert result[0].title  # fallback subtask always has a title


# ─── repair_audit() tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repair_audit_uses_auditor_chain():
    """repair_audit() calls the first backend in the auditor chain."""
    from core.orchestrator import ProjectOrchestrator
    ai = _make_ai()
    ai.complete = AsyncMock(return_value=json.dumps(MINIMAL_REPAIR_SUBTASK))
    orch = ProjectOrchestrator(ai)
    result = await orch.repair_audit({"myapp/cli.py": "# code"}, "FAILED", PROJECT_CONTEXT)
    assert len(result) == 1
    assert ai.complete.call_args_list[0].kwargs["backend"] == "gemini"


@pytest.mark.asyncio
async def test_repair_audit_falls_back_on_exception():
    """repair_audit() skips to next backend when current raises."""
    from core.orchestrator import ProjectOrchestrator
    ai = _make_ai()
    ai.complete = AsyncMock(side_effect=[
        RuntimeError("gemini down"),
        json.dumps(MINIMAL_REPAIR_SUBTASK),
    ])
    orch = ProjectOrchestrator(ai)
    result = await orch.repair_audit({}, "FAILED", PROJECT_CONTEXT)
    assert len(result) == 1
    # Session 56: qwen-122b retired; gemini-flash is now the second entry in auditor chain
    assert ai.complete.call_args_list[1].kwargs["backend"] == "gemini-flash"


@pytest.mark.asyncio
async def test_repair_audit_returns_empty_when_all_exhausted():
    """repair_audit() returns [] when all backends fail (pipeline continues)."""
    from core.orchestrator import ProjectOrchestrator
    ai = _make_ai()
    ai.complete = AsyncMock(side_effect=RuntimeError("all down"))
    orch = ProjectOrchestrator(ai)
    result = await orch.repair_audit({}, "FAILED", PROJECT_CONTEXT)
    assert result == []


# ─── Settings / role chain defaults ──────────────────────────────────────────

def test_role_orchestrator_chain_default():
    """ROLE_ORCHESTRATOR_CHAIN default must be gemini-customtools primary.
    Session 58 run log: Flash 3.0 HIGH+responseSchema → HTTP 400 for orchestrator decompose.
    customtools (Pro 3.1) is reliable for schema-constrained structured JSON output."""
    with patch.dict("os.environ", {}, clear=False):
        # Remove any override so the hardcoded default is used
        os.environ.pop("ROLE_ORCHESTRATOR_CHAIN", None)
        from config.settings import Settings
        s = Settings()
        assert s.ROLE_ORCHESTRATOR_CHAIN == "gemini-customtools,gemini-flash-high,claude-haiku"


def test_get_role_chain_orchestrator_default():
    """get_role_chain('orchestrator') returns gemini-flash as primary (Session 56: qwen-122b retired)."""
    from core.ai_backend import AIBackend
    ai = MagicMock(spec=AIBackend)
    ai.settings = MagicMock()
    ai.settings.ROLE_ORCHESTRATOR_CHAIN = "gemini-flash,gemini,claude-haiku"
    chain = AIBackend.get_role_chain(ai, "orchestrator")
    assert chain[0] == "gemini-flash"
    assert "gemini" in chain
    assert len(chain) == 3


def test_role_orchestrator_chain_env_override():
    """ROLE_ORCHESTRATOR_CHAIN can be overridden via env var."""
    with patch.dict("os.environ", {"ROLE_ORCHESTRATOR_CHAIN": "qwen-122b,gemini-flash,claude"}):
        from config.settings import Settings
        s = Settings()
        assert s.ROLE_ORCHESTRATOR_CHAIN == "qwen-122b,gemini-flash,claude"
