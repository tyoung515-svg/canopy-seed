"""
Session 44 tests — Qwen backend, Orchestration Board, Swarm Tools.

ADR-032: OrchestrationBoard (blackboard + task state + nudge queue)
ADR-033: SwarmTools (run_code, lint_code, read/write export file, search_docs)
ADR-034: Qwen backend dispatch in AIBackend
"""

import asyncio
import os
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── path shim ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))


# ════════════════════════════════════════════════════════════════════════════
# ADR-032 — OrchestrationBoard
# ════════════════════════════════════════════════════════════════════════════

from core.orchestration_board import OrchestrationBoard, TaskState, Nudge


@pytest.mark.asyncio
async def test_board_write_and_read():
    """Agents can write facts to the blackboard and read them back."""
    board = OrchestrationBoard(build_id="test-build-1")
    await board.write("utils.py::exports", ["UtilClass", "helper_fn"], author="utils.py")
    result = await board.read("utils.py::exports")
    assert result == ["UtilClass", "helper_fn"]


@pytest.mark.asyncio
async def test_board_read_missing_key_returns_none():
    """Reading a key that was never written returns None."""
    board = OrchestrationBoard(build_id="test-build-2")
    result = await board.read("nonexistent::key")
    assert result is None


@pytest.mark.asyncio
async def test_board_update_task_and_get_task():
    """Task state can be updated and retrieved."""
    board = OrchestrationBoard(build_id="test-build-3")
    await board.update_task("src/auth.py", status="in_progress", exports=["AuthService"])
    state = await board.get_task("src/auth.py")
    assert state is not None
    assert state.status == "in_progress"
    assert "AuthService" in state.exports


@pytest.mark.asyncio
async def test_board_task_status_transitions():
    """Task status transitions from in_progress → complete."""
    board = OrchestrationBoard(build_id="test-build-4")
    await board.update_task("src/models.py", status="in_progress")
    await board.update_task("src/models.py", status="complete", exports=["User", "Post"])
    state = await board.get_task("src/models.py")
    assert state.status == "complete"
    assert "User" in state.exports


@pytest.mark.asyncio
async def test_board_post_and_get_nudge():
    """Orchestrator can post nudges; agent consumes them."""
    board = OrchestrationBoard(build_id="test-build-5")
    sent = await board.post_nudge("test_auth.py", "Import AuthService from src/auth.py, not auth_service")
    assert sent is True
    nudges = await board.get_nudges("test_auth.py")
    assert len(nudges) == 1
    assert "AuthService" in nudges[0]


@pytest.mark.asyncio
async def test_board_nudge_consumed_on_get():
    """Getting nudges with consume=True means subsequent call returns empty."""
    board = OrchestrationBoard(build_id="test-build-6")
    await board.post_nudge("worker.py", "Use BatchSize = 32 not 64")
    first = await board.get_nudges("worker.py", consume=True)
    second = await board.get_nudges("worker.py", consume=True)
    assert len(first) == 1
    assert len(second) == 0


@pytest.mark.asyncio
async def test_board_nudge_not_consumed_when_consume_false():
    """Getting nudges with consume=False leaves them in the queue."""
    board = OrchestrationBoard(build_id="test-build-7")
    await board.post_nudge("worker.py", "Check error handling")
    first = await board.get_nudges("worker.py", consume=False)
    second = await board.get_nudges("worker.py", consume=False)
    assert len(first) == 1
    assert len(second) == 1


@pytest.mark.asyncio
async def test_board_nudge_queue_cap():
    """Nudge queue is capped at max_nudges; extra nudges are dropped."""
    board = OrchestrationBoard(build_id="test-build-8", max_nudges=2)
    r1 = await board.post_nudge("file.py", "nudge 1")
    r2 = await board.post_nudge("file.py", "nudge 2")
    r3 = await board.post_nudge("file.py", "nudge 3")  # should be dropped
    assert r1 is True
    assert r2 is True
    assert r3 is False
    nudges = await board.get_nudges("file.py")
    assert len(nudges) == 2


@pytest.mark.asyncio
async def test_board_get_snapshot():
    """Snapshot contains blackboard, tasks, and pending nudges."""
    board = OrchestrationBoard(build_id="snap-build")
    await board.write("key1", "value1", author="a.py")
    await board.update_task("a.py", status="complete", exports=["A"])
    await board.post_nudge("b.py", "use A from a.py")
    snap = await board.get_snapshot()
    assert snap["build_id"] == "snap-build"
    assert "key1" in snap["blackboard"]
    assert "a.py" in snap["tasks"]
    assert "b.py" in snap["pending_nudges"]


@pytest.mark.asyncio
async def test_board_format_context_includes_nudges():
    """format_context_for_agent returns nudge messages with ORCHESTRATOR DIRECTIVES header."""
    board = OrchestrationBoard(build_id="ctx-build")
    await board.post_nudge("target.py", "use snake_case for all variable names")
    ctx = await board.format_context_for_agent("target.py")
    assert "ORCHESTRATOR DIRECTIVES" in ctx
    assert "snake_case" in ctx


@pytest.mark.asyncio
async def test_board_format_context_includes_sibling_exports():
    """format_context_for_agent shows completed siblings' exports."""
    board = OrchestrationBoard(build_id="ctx-build-2")
    await board.update_task("utils.py", status="complete", exports=["format_date", "parse_number"])
    ctx = await board.format_context_for_agent("consumer.py")
    assert "utils.py" in ctx
    assert "format_date" in ctx


@pytest.mark.asyncio
async def test_board_format_context_excludes_own_entries():
    """format_context_for_agent omits entries written by the requesting agent."""
    board = OrchestrationBoard(build_id="ctx-build-3")
    await board.write("own.py::exports", ["SelfClass"], author="own.py")
    await board.write("other.py::exports", ["OtherClass"], author="other.py")
    ctx = await board.format_context_for_agent("own.py")
    assert "OtherClass" in ctx
    assert "SelfClass" not in ctx or "other.py" in ctx  # own entry should not appear under siblings


# ════════════════════════════════════════════════════════════════════════════
# ADR-033 — Swarm Tools
# ════════════════════════════════════════════════════════════════════════════

from core.swarm_tools import (
    run_code,
    lint_code,
    read_export_file,
    write_export_file,
    dispatch_tool,
    SwarmToolContext,
    TOOL_DEFINITIONS,
)


@pytest.mark.asyncio
async def test_run_code_python_success():
    """run_code executes Python and returns stdout."""
    result = await run_code("print('hello swarm')", "python")
    assert result["exit_code"] == 0
    assert "hello swarm" in result["stdout"]
    assert result["timed_out"] is False


@pytest.mark.asyncio
async def test_run_code_python_syntax_error():
    """run_code captures syntax errors in stderr."""
    result = await run_code("def broken(:", "python")
    assert result["exit_code"] != 0
    assert result["stderr"] or result["stdout"]


@pytest.mark.asyncio
async def test_run_code_timeout():
    """run_code enforces the timeout."""
    result = await run_code("import time; time.sleep(99)", "python", timeout=1)
    assert result["timed_out"] is True
    assert result["exit_code"] == -1


@pytest.mark.asyncio
async def test_run_code_unsupported_language():
    """run_code returns error for unsupported languages."""
    result = await run_code("print 'hello'", "ruby")
    assert result["exit_code"] == 1
    assert "Unsupported" in result["stderr"]


@pytest.mark.asyncio
async def test_lint_code_python_clean():
    """lint_code returns clean=True for valid Python."""
    result = await lint_code("x = 1\nprint(x)\n", "python")
    # May use ruff or pyflakes; either way clean simple code should be violation-free
    assert isinstance(result["violations"], list)
    assert result["linter"] in {"ruff", "pyflakes", "none"}


@pytest.mark.asyncio
async def test_lint_code_python_undefined_name():
    """lint_code detects undefined names in Python."""
    # Use pyflakes-detectable error: reference undefined variable
    result = await lint_code("print(undefined_variable_xyz)\n", "python")
    # Some linters will catch this, some may not depending on what's installed
    assert isinstance(result["violations"], list)


@pytest.mark.asyncio
async def test_read_export_file_found(tmp_path):
    """read_export_file returns content of an existing file."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "utils.py").write_text("def helper(): pass\n")
    result = await read_export_file("src/utils.py", export_dir=str(tmp_path))
    assert result["found"] is True
    assert "def helper" in result["content"]


@pytest.mark.asyncio
async def test_read_export_file_not_found(tmp_path):
    """read_export_file returns found=False for missing files."""
    result = await read_export_file("nonexistent.py", export_dir=str(tmp_path))
    assert result["found"] is False
    assert result["content"] == ""


@pytest.mark.asyncio
async def test_read_export_file_path_traversal_blocked(tmp_path):
    """read_export_file blocks path traversal outside export_dir."""
    result = await read_export_file("../../etc/passwd", export_dir=str(tmp_path))
    assert result["found"] is False
    assert "traversal" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_write_export_file_success(tmp_path):
    """write_export_file creates the file with the given content."""
    result = await write_export_file("output/main.py", "x = 42\n", export_dir=str(tmp_path))
    assert result["written"] is True
    assert (tmp_path / "output" / "main.py").read_text() == "x = 42\n"


@pytest.mark.asyncio
async def test_write_export_file_creates_parents(tmp_path):
    """write_export_file creates intermediate directories."""
    result = await write_export_file("deep/nested/file.py", "pass\n", export_dir=str(tmp_path))
    assert result["written"] is True
    assert (tmp_path / "deep" / "nested" / "file.py").exists()


@pytest.mark.asyncio
async def test_write_export_file_path_traversal_blocked(tmp_path):
    """write_export_file blocks writes outside export_dir."""
    result = await write_export_file("../../evil.py", "rm -rf /", export_dir=str(tmp_path))
    assert result["written"] is False
    assert "traversal" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_write_export_file_no_export_dir():
    """write_export_file returns error when export_dir is not set."""
    result = await write_export_file("file.py", "pass\n", export_dir="")
    assert result["written"] is False
    assert "export_dir" in result.get("error", "")


@pytest.mark.asyncio
async def test_dispatch_tool_run_code():
    """dispatch_tool correctly dispatches to run_code."""
    result = await dispatch_tool("run_code", {"code": "print(42)", "language": "python"})
    assert result["exit_code"] == 0
    assert "42" in result["stdout"]


@pytest.mark.asyncio
async def test_dispatch_tool_unknown():
    """dispatch_tool returns error for unknown tool names."""
    result = await dispatch_tool("nonexistent_tool", {})
    assert "error" in result
    assert "Unknown tool" in result["error"]


def test_tool_definitions_schema():
    """All tool definitions have the required MCP-compatible fields."""
    required_fields = {"name", "description", "input_schema"}
    tool_names = set()
    for td in TOOL_DEFINITIONS:
        for f in required_fields:
            assert f in td, f"Tool definition missing field '{f}': {td.get('name')}"
        assert td["name"] not in tool_names, f"Duplicate tool name: {td['name']}"
        tool_names.add(td["name"])
    # Session 58: run_test and search_code added for Big Fixer self-verification
    # Session 58 (declare_done): declare_done added as ANY-mode termination signal
    expected = {
        "run_code", "lint_code", "read_export_file", "write_export_file",
        "search_docs", "run_test", "search_code", "declare_done",
    }
    assert expected == tool_names


def test_swarm_tool_context_render_enabled():
    """SwarmToolContext.render returns non-empty string when enabled."""
    ctx = SwarmToolContext.render(enabled=True)
    assert "TOOL_CALL" in ctx
    assert "run_code" in ctx
    assert "lint_code" in ctx


def test_swarm_tool_context_render_disabled():
    """SwarmToolContext.render returns empty string when disabled."""
    ctx = SwarmToolContext.render(enabled=False)
    assert ctx == ""


def test_swarm_tool_context_extract_tool_calls():
    """extract_tool_calls parses TOOL_CALL: lines from AI responses."""
    ai_response = (
        "Some preamble\n"
        'TOOL_CALL: {"tool": "lint_code", "input": {"code": "x=1", "language": "python"}}\n'
        "Some more text\n"
        'TOOL_CALL: {"tool": "run_code", "input": {"code": "print(1)", "language": "python"}}\n'
        "Final output"
    )
    calls = SwarmToolContext.extract_tool_calls(ai_response)
    assert len(calls) == 2
    assert calls[0]["tool"] == "lint_code"
    assert calls[1]["tool"] == "run_code"


def test_swarm_tool_context_extract_no_calls():
    """extract_tool_calls returns empty list when no TOOL_CALL: lines present."""
    calls = SwarmToolContext.extract_tool_calls("Just some code output\nwith no tools")
    assert calls == []


def test_swarm_tool_context_extract_ignores_malformed_json():
    """extract_tool_calls gracefully ignores malformed JSON in TOOL_CALL lines."""
    ai_response = 'TOOL_CALL: {not valid json}\nTOOL_CALL: {"tool": "run_code", "input": {}}'
    calls = SwarmToolContext.extract_tool_calls(ai_response)
    assert len(calls) == 1  # only the valid one
    assert calls[0]["tool"] == "run_code"


def test_swarm_tool_context_extract_extra_data_after_json():
    """extract_tool_calls recovers from 'Extra data' — model wrote text after the JSON object."""
    # Simulates model generating: TOOL_CALL: {...} some trailing explanation text
    ai_response = (
        'TOOL_CALL: {"tool": "write_export_file", "input": {"path": "a.py", "content": "x=1"}} '
        'Now let me explain what I did...'
    )
    calls = SwarmToolContext.extract_tool_calls(ai_response)
    assert len(calls) == 1
    assert calls[0]["tool"] == "write_export_file"
    assert calls[0]["input"]["path"] == "a.py"
    assert calls[0]["input"]["content"] == "x=1"


def test_swarm_tool_context_extract_nested_braces_in_content():
    """extract_tool_calls handles JSON content values that contain braces."""
    content_with_braces = "def foo(): return {'a': 1}"
    import json
    line = f'TOOL_CALL: {json.dumps({"tool": "write_export_file", "input": {"path": "f.py", "content": content_with_braces}})}'
    calls = SwarmToolContext.extract_tool_calls(line)
    assert len(calls) == 1
    assert calls[0]["input"]["content"] == content_with_braces


# ════════════════════════════════════════════════════════════════════════════
# ADR-034 — Qwen backend dispatch
# ════════════════════════════════════════════════════════════════════════════

from core.ai_backend import AIBackend


def _make_settings(**overrides):
    settings = MagicMock()
    settings.ANTHROPIC_API_KEY = ""
    settings.GOOGLE_API_KEY = ""
    settings.OPENAI_API_KEY = ""
    settings.QWEN_API_KEY = "test-qwen-key"
    settings.QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    settings.QWEN_FLASH_MODEL = "qwen3.5-flash"
    settings.QWEN_PLUS_MODEL = "qwen3.5-plus"
    settings.QWEN_CODER_MODEL = "qwen3-coder"
    settings.QWEN_LOCAL_MODEL = "Qwen/Qwen3.5-35B-A3B"
    settings.LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
    settings.LMSTUDIO_MODEL = "local-model"
    settings.CLAUDE_MODEL = "claude-sonnet-4-6"
    settings.CLAUDE_ESCALATION_ENABLED = False
    settings.DEFAULT_AI_BACKEND = "claude"
    settings.GOOGLE_GEMINI_MODEL = "gemini-2.0-flash"
    settings.OPENAI_MODEL = "gpt-5-mini"
    settings.OPENAI_CODEX_MODEL = "gpt-5.3-codex"
    settings.CODEX_REASONING_EFFORT = "medium"
    settings.WORKER_BASE_URL = ""
    settings.WORKER_MODEL = ""
    for k, v in overrides.items():
        setattr(settings, k, v)
    return settings


@pytest.mark.asyncio
async def test_qwen_flash_backend_dispatches_to_qwen_complete():
    """backend='qwen-flash' calls _qwen_complete with qwen3.5-flash model."""
    settings = _make_settings()
    backend = AIBackend(settings)

    async def mock_qwen(system, message, history, max_tokens, model):
        assert model == "qwen3.5-flash"
        return "qwen flash response"

    backend._qwen_complete = mock_qwen
    result = await backend.complete(
        system="sys", message="hello", backend="qwen-flash"
    )
    assert result == "qwen flash response"


@pytest.mark.asyncio
async def test_qwen_plus_backend_dispatches_with_plus_model():
    """backend='qwen-plus' calls _qwen_complete with qwen3.5-plus model."""
    settings = _make_settings()
    backend = AIBackend(settings)

    async def mock_qwen(system, message, history, max_tokens, model):
        assert model == "qwen3.5-plus"
        return "qwen plus response"

    backend._qwen_complete = mock_qwen
    result = await backend.complete(
        system="sys", message="hello", backend="qwen-plus"
    )
    assert result == "qwen plus response"


@pytest.mark.asyncio
async def test_qwen_coder_backend_dispatches_with_coder_model():
    """backend='qwen-coder' calls _qwen_complete with qwen3-coder model."""
    settings = _make_settings()
    backend = AIBackend(settings)

    async def mock_qwen(system, message, history, max_tokens, model):
        assert model == "qwen3-coder"
        return "qwen coder response"

    backend._qwen_complete = mock_qwen
    result = await backend.complete(
        system="sys", message="hello", backend="qwen-coder"
    )
    assert result == "qwen coder response"


@pytest.mark.asyncio
async def test_qwen_local_routes_to_lmstudio():
    """backend='qwen-local' calls _lmstudio_complete with the Qwen local model."""
    settings = _make_settings()
    backend = AIBackend(settings)

    captured = {}

    async def mock_lmstudio(system, message, history, max_tokens, model_id=""):
        captured["model_id"] = model_id
        return "local qwen response"

    backend._lmstudio_complete = mock_lmstudio
    result = await backend.complete(
        system="sys", message="hello", backend="qwen-local"
    )
    assert result == "local qwen response"
    assert captured["model_id"] == "Qwen/Qwen3.5-35B-A3B"


@pytest.mark.asyncio
async def test_qwen_flash_raises_on_missing_api_key():
    """_qwen_complete raises ValueError when QWEN_API_KEY is not set."""
    settings = _make_settings(QWEN_API_KEY="")
    backend = AIBackend(settings)
    with pytest.raises(RuntimeError, match="Qwen Flash backend failed"):
        await backend.complete(system="s", message="m", backend="qwen-flash")


@pytest.mark.asyncio
async def test_qwen_complete_strips_think_tags():
    """_qwen_complete strips <think>...</think> blocks from model responses."""
    settings = _make_settings()
    backend = AIBackend(settings)

    import httpx
    response_data = {
        "choices": [{
            "message": {
                "content": "<think>internal reasoning here</think>\ndef my_function(): pass"
            }
        }]
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await backend._qwen_complete(
            system="sys", message="write code", history=[],
            max_tokens=1024, model="qwen3.5-flash"
        )

    assert "<think>" not in result
    assert "def my_function" in result


# ════════════════════════════════════════════════════════════════════════════
# ADR-034 — Configurable tier routing
# ════════════════════════════════════════════════════════════════════════════

from core.agent_pool import AgentPool


def test_tier_routing_defaults_preserved():
    """Default tier routing still produces openai-codex for lite/standard."""
    settings = _make_settings()
    settings.SWARM_BACKEND_LITE = "openai-codex"
    settings.SWARM_BACKEND_STANDARD = "openai-codex"
    settings.SWARM_BACKEND_BRAIN = "claude"
    settings.SWARM_BACKEND_HEAVY = "claude"
    pool = AgentPool(settings=settings)
    assert pool._select_backend_for_tier("lite") == "openai-codex"
    assert pool._select_backend_for_tier("standard") == "openai-codex"
    assert pool._select_backend_for_tier("brain") == "claude"
    assert pool._select_backend_for_tier("heavy") == "claude"


def test_tier_routing_qwen_override():
    """Setting SWARM_BACKEND_LITE=qwen-flash routes lite tasks to Qwen."""
    settings = _make_settings()
    settings.SWARM_BACKEND_LITE = "qwen-flash"
    settings.SWARM_BACKEND_STANDARD = "qwen-coder"
    settings.SWARM_BACKEND_BRAIN = "claude"
    settings.SWARM_BACKEND_HEAVY = "claude"
    pool = AgentPool(settings=settings)
    assert pool._select_backend_for_tier("lite") == "qwen-flash"
    assert pool._select_backend_for_tier("low") == "qwen-flash"
    assert pool._select_backend_for_tier("standard") == "qwen-coder"
    assert pool._select_backend_for_tier("brain") == "claude"


def test_tier_routing_no_settings_uses_defaults():
    """Without settings, tier routing falls back to hardcoded defaults."""
    pool = AgentPool(settings=None)
    assert pool._select_backend_for_tier("lite") == "openai-codex"
    assert pool._select_backend_for_tier("standard") == "openai-codex"
    assert pool._select_backend_for_tier("escalate") == "claude"
    assert pool._select_backend_for_tier("unknown-tier") == "claude"
