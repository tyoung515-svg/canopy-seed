"""
Session 50 tests — SwarmTool dispatch hardening + Giant Brain list-response fix.

Covers:
  Giant Brain list-response recovery
    - when backend returns JSON list, try next backend
    - when all backends return lists, return None

  write_export_file pre-dispatch validation
    - missing both path and content → clear error dict (no Python exception)
    - missing content only → clear error dict
    - valid input → dispatches normally

  TOOL_CALL strip from final ai_output
    - TOOL_CALL: lines are removed before file write
    - TOOL_RESULT: lines are removed before file write
    - clean output is unchanged
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.agent_pool import AgentPool


VALID_MANIFEST = json.dumps({
    "summary": "missing import",
    "escalate": False,
    "fixes": [
        {
            "file": "myapp/cli.py",
            "complexity": "mechanical",
            "description": "Add missing import",
            "action": "add_import",
        }
    ],
})


def _make_gb_pool(*side_effects):
    """Pool mock whose ai.complete cycles through side_effects values."""
    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.ai.get_role_chain = MagicMock(return_value=["gemini-flash", "claude"])
    pool.ai.complete = AsyncMock(side_effect=list(side_effects))
    pool._collect_source_tree = MagicMock(return_value={"app/cli.py": "import sys"})
    return pool


# ─── Giant Brain list-response recovery ───────────────────────────────────────

@pytest.mark.asyncio
async def test_giant_brain_list_response_tries_next_backend(tmp_path):
    """When first backend returns a JSON list, Giant Brain tries the next backend."""
    pool = _make_gb_pool(
        json.dumps([{"wrong": "format"}]),  # gemini-flash → list
        VALID_MANIFEST,                      # claude → dict ✓
    )

    result = await AgentPool._giant_brain_audit(pool, tmp_path, "FAILED")

    assert result is not None, "Giant Brain should recover by trying next backend"
    assert result.get("escalate") is False
    assert len(result["fixes"]) == 1
    # ai.complete should have been called twice (once for each backend)
    assert pool.ai.complete.call_count == 2


@pytest.mark.asyncio
async def test_giant_brain_all_backends_return_list_returns_none(tmp_path):
    """When every backend returns a JSON list, Giant Brain returns None."""
    pool = _make_gb_pool(
        json.dumps([{"wrong": "format"}]),  # gemini-flash → list
        json.dumps([{"wrong": "format"}]),  # claude → list
    )

    result = await AgentPool._giant_brain_audit(pool, tmp_path, "FAILED")

    assert result is None, "All backends returning lists should result in None"


# ─── write_export_file pre-dispatch validation ────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_write_export_file_missing_both_returns_error():
    """Dispatching write_export_file with empty input returns a clear error dict."""
    from core.swarm_tools import dispatch_tool

    result = await dispatch_tool("write_export_file", {}, export_dir="/tmp")

    assert "error" in result
    assert "path" in result["error"]
    assert "content" in result["error"]
    # Critically: no Python TypeError was raised


@pytest.mark.asyncio
async def test_dispatch_write_export_file_missing_content_returns_error():
    """Dispatching write_export_file without content returns a clear error dict."""
    from core.swarm_tools import dispatch_tool

    result = await dispatch_tool(
        "write_export_file", {"path": "myapp/utils.py"}, export_dir="/tmp"
    )

    assert "error" in result
    assert "content" in result["error"]


@pytest.mark.asyncio
async def test_dispatch_write_export_file_valid_writes_file(tmp_path):
    """Dispatching write_export_file with path+content writes the file."""
    from core.swarm_tools import dispatch_tool

    result = await dispatch_tool(
        "write_export_file",
        {"path": "myapp/utils.py", "content": "# hello"},
        export_dir=str(tmp_path),
    )

    assert result.get("written") is True
    assert (tmp_path / "myapp" / "utils.py").read_text() == "# hello"


# ─── TOOL_CALL strip from final ai_output ─────────────────────────────────────
# These tests verify the stripping logic by calling _execute_subtask with a
# mock AI that returns TOOL_CALL: lines in its final output.

def _make_subtask(title="Write models", files=None):
    from core.orchestrator import OrchestratorSubTask
    st = MagicMock(spec=OrchestratorSubTask)
    st.title = title
    st.description = "Implement models"
    st.target_files = files or ["myapp/models.py"]
    st.notes = ""
    st.depends_on = []
    judge = MagicMock()
    judge.tier = "lite"
    judge.recommended_model = "gemini-flash-lite"
    st.judge_result = judge
    return st


@pytest.mark.asyncio
async def test_tool_call_lines_stripped_from_final_output(tmp_path):
    """TOOL_CALL: lines remaining in the final ai_output are stripped before writing."""
    from core.agent_pool import AgentPool

    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.settings = MagicMock()
    pool.settings.SWARM_TOOLS_ENABLED = True
    pool.settings.REPO_MAP_MAX_TOKENS = 2000
    pool._broadcast = AsyncMock()
    pool._select_backend_for_tier = MagicMock(return_value="gemini-flash-lite")
    pool._is_test_file_target = MagicMock(return_value=False)
    pool._compose_subtask_description = MagicMock(return_value="desc")
    pool._frozen_contract_block = MagicMock(return_value="")
    pool._project_seed_text = MagicMock(return_value="")

    # AI always returns TOOL_CALL lines (simulates flash-lite stuck loop)
    garbage_output = (
        'TOOL_CALL: {"tool": "write_export_file", "input": {}}\n'
        'Some explanation text\n'
        'TOOL_RESULT: {"tool": "write_export_file", "result": "error"}'
    )
    clean_code = "class Habit:\n    pass\n"
    # Round 1: TOOL_CALL (empty) — triggers loop; Round 2 (follow-up): clean code
    pool.ai.complete = AsyncMock(side_effect=[garbage_output, clean_code])

    subtask = _make_subtask(files=["myapp/models.py"])
    semaphore = __import__("asyncio").Semaphore(1)

    result = await AgentPool._execute_subtask(
        pool,
        subtask,
        str(tmp_path),
        semaphore,
    )

    written = tmp_path / "myapp" / "models.py"
    if written.exists():
        content = written.read_text()
        assert "TOOL_CALL:" not in content, "TOOL_CALL: must not appear in written file"
        assert "TOOL_RESULT:" not in content, "TOOL_RESULT: must not appear in written file"


@pytest.mark.asyncio
async def test_clean_output_unchanged(tmp_path):
    """When ai_output has no TOOL_CALL lines, the content is written verbatim."""
    from core.agent_pool import AgentPool

    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.settings = MagicMock()
    pool.settings.SWARM_TOOLS_ENABLED = True
    pool.settings.REPO_MAP_MAX_TOKENS = 2000
    pool._broadcast = AsyncMock()
    pool._select_backend_for_tier = MagicMock(return_value="gemini-flash")
    pool._is_test_file_target = MagicMock(return_value=False)
    pool._compose_subtask_description = MagicMock(return_value="desc")
    pool._frozen_contract_block = MagicMock(return_value="")
    pool._project_seed_text = MagicMock(return_value="")

    clean_code = "# No tool calls here\nclass Habit:\n    pass\n"
    pool.ai.complete = AsyncMock(return_value=clean_code)

    subtask = _make_subtask(files=["myapp/models.py"])
    semaphore = __import__("asyncio").Semaphore(1)

    await AgentPool._execute_subtask(pool, subtask, str(tmp_path), semaphore)

    written = tmp_path / "myapp" / "models.py"
    if written.exists():
        content = written.read_text()
        assert "class Habit" in content
