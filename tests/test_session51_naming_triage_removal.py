"""
Session 51 tests — Naming triage removal from repair flows.

ADR-016 naming triage (Round 0) was removed from both:
  - _run_giant_brain_repair
  - _run_repair_loop

Rationale: Giant Brain 3-phase audit (Phase 1b root-cause) is a strict superset.
Naming triage ran flash-lite at 1024 tokens and was unable to touch test files,
making it ineffective for package-name mismatches between source modules and
test imports. The flash-lite empty-tool-call pattern also made it unreliable.

Covers:
  _run_giant_brain_repair skips naming triage
    - never calls _naming_triage, goes straight to Giant Brain passes
  _run_repair_loop skips naming triage
    - never calls _naming_triage, goes straight to repair rounds
  _naming_triage still exists but is unreachable from repair flows
"""

import asyncio
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool():
    """Return a minimal AgentPool-like object with mocked dependencies."""
    from core.agent_pool import AgentPool
    pool = object.__new__(AgentPool)
    pool.ai = MagicMock()
    pool._sse_broadcast = None
    pool._project_seed_text = MagicMock(return_value="seed")
    pool._detect_target_language = MagicMock(return_value="python")
    pool._broadcast = AsyncMock()
    pool._rerun_tests = AsyncMock()
    pool._collect_test_output = MagicMock(return_value="test output")
    pool._collect_source_tree = MagicMock(return_value="source tree")
    return pool


def _make_summary(failed: int = 0, passed: int = 5):
    summary = MagicMock()
    summary.failed = failed
    summary.passed = passed
    summary.results = []
    return summary


# ---------------------------------------------------------------------------
# _run_giant_brain_repair — naming triage not called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_giant_brain_repair_does_not_call_naming_triage(tmp_path):
    """_run_giant_brain_repair must not call _naming_triage at any point."""
    pool = _make_pool()

    # All tests passing — Giant Brain loop exits immediately
    clean_summary = _make_summary(failed=0, passed=10)
    pool._rerun_tests.return_value = clean_summary
    pool._giant_brain_audit = AsyncMock(return_value=None)
    pool._naming_triage = AsyncMock(return_value=[])

    project_context = {"description": "test project"}
    await pool._run_giant_brain_repair(
        swarm_summary=clean_summary,
        export_dir=tmp_path,
        project_context=project_context,
        session_id="sess-test",
        memory=None,
    )

    pool._naming_triage.assert_not_called()


@pytest.mark.asyncio
async def test_giant_brain_repair_does_not_call_naming_triage_when_failing(tmp_path):
    """Even with failing tests, _run_giant_brain_repair skips _naming_triage."""
    pool = _make_pool()

    failing_summary = _make_summary(failed=3, passed=7)
    clean_summary = _make_summary(failed=0, passed=10)

    pool._rerun_tests.return_value = clean_summary
    pool._giant_brain_audit = AsyncMock(return_value=None)  # audit returns nothing → loop exits
    pool._naming_triage = AsyncMock(return_value=[])

    project_context = {"description": "test project"}
    await pool._run_giant_brain_repair(
        swarm_summary=failing_summary,
        export_dir=tmp_path,
        project_context=project_context,
        session_id="sess-test",
        memory=None,
    )

    pool._naming_triage.assert_not_called()


# ---------------------------------------------------------------------------
# _run_repair_loop — naming triage not called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repair_loop_does_not_call_naming_triage_when_clean(tmp_path):
    """_run_repair_loop must not call _naming_triage when tests pass."""
    pool = _make_pool()
    clean_summary = _make_summary(failed=0, passed=10)
    pool._naming_triage = AsyncMock(return_value=[])

    with patch("core.orchestrator.ProjectOrchestrator") as MockOrch, \
         patch("core.tester_swarm.TesterSwarm"):
        mock_orch = MockOrch.return_value
        mock_orch.repair_audit = AsyncMock(return_value=[])

        await pool._run_repair_loop(
            swarm_summary=clean_summary,
            export_dir=tmp_path,
            project_context={"description": "proj"},
            session_id="sess-repair",
        )

    pool._naming_triage.assert_not_called()


@pytest.mark.asyncio
async def test_repair_loop_does_not_call_naming_triage_when_failing(tmp_path):
    """Even with failing tests, _run_repair_loop skips _naming_triage."""
    pool = _make_pool()
    failing_summary = _make_summary(failed=4, passed=6)
    pool._naming_triage = AsyncMock(return_value=[MagicMock()])

    with patch("core.orchestrator.ProjectOrchestrator") as MockOrch, \
         patch("core.tester_swarm.TesterSwarm"):
        mock_orch = MockOrch.return_value
        # audit returns [] → loop exits after round 1
        mock_orch.repair_audit = AsyncMock(return_value=[])

        await pool._run_repair_loop(
            swarm_summary=failing_summary,
            export_dir=tmp_path,
            project_context={"description": "proj"},
            session_id="sess-repair",
        )

    pool._naming_triage.assert_not_called()


# ---------------------------------------------------------------------------
# _naming_triage still exists (not deleted — preserved for potential future use)
# ---------------------------------------------------------------------------

def test_naming_triage_method_still_exists():
    """_naming_triage must still exist on AgentPool (not deleted)."""
    from core.agent_pool import AgentPool
    assert hasattr(AgentPool, "_naming_triage"), (
        "_naming_triage should still exist even though it's not called from repair flows"
    )
    assert callable(AgentPool._naming_triage)


def test_naming_triage_returns_empty_for_no_naming_signals(tmp_path):
    """_naming_triage returns [] when failure output has no naming signals."""
    pool = _make_pool()

    result_obj = MagicMock()
    result_obj.passed = False
    result_obj.failure_output = "AssertionError: expected 1 got 2"
    result_obj.failure_summary = ""
    result_obj.output = ""
    result_obj.stdout = ""
    result_obj.test_file = ""
    result_obj.source_file = ""
    result_obj.file = ""

    summary = MagicMock()
    summary.results = [result_obj]

    subtasks = asyncio.run(
        pool._naming_triage(
            swarm_summary=summary,
            export_dir=tmp_path,
            project_context={},
        )
    )
    assert subtasks == []


def test_naming_triage_returns_empty_for_no_results():
    """_naming_triage returns [] when swarm_summary has no results attr."""
    pool = _make_pool()
    summary = MagicMock(spec=[])  # no .results attribute

    subtasks = asyncio.run(
        pool._naming_triage(
            swarm_summary=summary,
            export_dir=Path("/tmp"),
            project_context={},
        )
    )
    assert subtasks == []
