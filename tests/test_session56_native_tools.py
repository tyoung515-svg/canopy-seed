"""
Session 56 — Native Gemini Function Calling + Big Fixer tool integration tests.
ADR: Session 56
Baseline: 379 -> target 405
"""

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.agent_pool import AgentPool
from core.ai_backend import AIBackend
from core.swarm_tools import TOOL_DEFINITIONS
from core.tester_swarm import SwarmSummary


class DummySettings:
    def __init__(self):
        self.ANTHROPIC_API_KEY = ""
        self.CLAUDE_ESCALATION_ENABLED = False
        self.CLAUDE_MODEL = "claude-sonnet"
        self.DEFAULT_AI_BACKEND = "gemini"
        self.GEMINI_API_KEY = ""
        self.GEMINI_FLASH_MODEL = "gemini-3-flash-preview"
        self.GOOGLE_API_KEY = "test-google-key"
        self.GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
        self.LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
        self.LMSTUDIO_MODEL = "local-model"


class _FakeStreamResponse:
    def __init__(self, lines: List[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakePostResponse:
    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClientFactory:
    def __init__(self, stream_batches: List[List[str]], post_payload: Dict[str, Any] = None):
        self.stream_batches = list(stream_batches)
        self.stream_calls = 0
        self.payloads: List[Dict[str, Any]] = []
        self.post_calls: List[Dict[str, Any]] = []
        self.post_payload = post_payload or {
            "candidates": [{"content": {"parts": [{"text": "fallback"}]}}]
        }

    def __call__(self, *args, **kwargs):
        factory = self

        class _FakeAsyncClient:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

            def stream(self_inner, method, url, json=None):
                factory.payloads.append(json)
                idx = factory.stream_calls
                factory.stream_calls += 1
                if idx >= len(factory.stream_batches):
                    raise AssertionError("Unexpected extra stream() call")
                return _FakeStreamResponse(factory.stream_batches[idx])

            async def post(self_inner, url, json=None):
                factory.post_calls.append({"url": url, "json": json})
                return _FakePostResponse(factory.post_payload)

        return _FakeAsyncClient()


def _build_ai(settings=None):
    return AIBackend(settings or DummySettings())


def _make_summary(failed: int, passed: int = 0, total: int = None) -> SwarmSummary:
    resolved_total = total if total is not None else (failed + passed)
    return SwarmSummary(total=resolved_total, passed=passed, failed=failed, duration=0.01, results=[])


def _make_pool(tmp_path: Path, max_attempts: int = 2):
    settings = MagicMock()
    settings.BIG_FIXER_MAX_ATTEMPTS = max_attempts

    ai = MagicMock()
    ai.complete = AsyncMock(return_value="BIG_FIXER_DONE")
    # Session 58: _run_big_fixer now calls get_role_chain("big_fixer") so configure the mock
    ai.get_role_chain = MagicMock(
        return_value=["gemini-customtools", "gemini-flash-high", "claude"]
    )

    pool = AgentPool(ai_backend=ai, settings=settings)
    pool._broadcast = AsyncMock()
    pool._collect_test_output = MagicMock(return_value="AssertionError: expected 1 == 2")
    pool._collect_source_tree = MagicMock(return_value={"src/app.py": "def run():\n    return 1\n"})
    return pool, ai


def _sample_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "read_export_file",
            "description": "Read file from export directory",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        }
    ]


def _sse(events: List[Dict[str, Any]]) -> List[str]:
    lines = [f"data: {json.dumps(evt)}" for evt in events]
    lines.append("data: [DONE]")
    return lines


def _text_event(text: str) -> Dict[str, Any]:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _function_event(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"candidates": [{"content": {"parts": parts}}]}


def _supports_complete_native_tools() -> bool:
    sig = inspect.signature(AIBackend.complete)
    return "tools" in sig.parameters and "tool_dispatcher" in sig.parameters


def _supports_gemini_native_tools() -> bool:
    sig = inspect.signature(AIBackend._gemini_complete)
    return "tools" in sig.parameters and "tool_dispatcher" in sig.parameters


def _require_complete_native_tools():
    if not _supports_complete_native_tools():
        pytest.skip("AIBackend.complete tools/tool_dispatcher interface not present")


def _require_gemini_native_tools():
    if not _supports_gemini_native_tools():
        pytest.skip("AIBackend._gemini_complete native tool loop not present")


def _supports_thought_signature_echo() -> bool:
    backend = _build_ai()
    dispatcher = AsyncMock(return_value={"ok": True})
    fake_httpx = _FakeAsyncClientFactory(
        stream_batches=[
            _sse([
                _function_event([
                    {"thoughtSignature": "probe-signature"},
                    {"functionCall": {"name": "read_export_file", "args": {"path": "src/a.py"}}},
                ])
            ]),
            _sse([_text_event("done")]),
        ]
    )

    try:
        with patch("httpx.AsyncClient", fake_httpx):
            _call_gemini_complete(
                backend,
                tools=_sample_tools(),
                tool_dispatcher=dispatcher,
            )
    except Exception:
        return False

    if len(fake_httpx.payloads) < 2:
        return False

    second_payload = fake_httpx.payloads[1]
    for turn in second_payload.get("contents", []):
        for part in turn.get("parts", []):
            echoed = part.get("thought")
            if echoed is None:
                echoed = part.get("thoughtSignature")
            if echoed == "probe-signature":
                return True
    return False


def _require_thought_signature_echo():
    if not _supports_thought_signature_echo():
        pytest.skip("thought-signature echo not present")


def _call_gemini_complete(
    backend: AIBackend,
    *,
    json_mode: bool = False,
    tools: List[Dict[str, Any]] = None,
    tool_dispatcher=None,
    tool_mode: str = "AUTO",
) -> str:
    sig = inspect.signature(backend._gemini_complete)
    kwargs: Dict[str, Any] = {
        "system": "system",
        "message": "message",
        "history": [],
    }
    if "json_mode" in sig.parameters:
        kwargs["json_mode"] = json_mode
    if "response_schema" in sig.parameters:
        kwargs["response_schema"] = None
    if "max_tokens" in sig.parameters:
        kwargs["max_tokens"] = 128
    if "model" in sig.parameters:
        kwargs["model"] = "gemini-3.1-pro-preview-customtools"
    if "tools" in sig.parameters:
        kwargs["tools"] = tools
    if "tool_dispatcher" in sig.parameters:
        kwargs["tool_dispatcher"] = tool_dispatcher
    if "tool_mode" in sig.parameters:
        kwargs["tool_mode"] = tool_mode
    return asyncio.run(backend._gemini_complete(**kwargs))


# ---------------------------------------------------------------------------
# Group A — _gemini_complete with tools (8)
# ---------------------------------------------------------------------------

def test_gemini_complete_no_tools_returns_text():
    backend = _build_ai()
    fake_httpx = _FakeAsyncClientFactory(stream_batches=[_sse([_text_event("hello from gemini")])])

    with patch("httpx.AsyncClient", fake_httpx):
        result = _call_gemini_complete(backend, json_mode=False)

    assert isinstance(result, str)
    assert result == "hello from gemini"


def test_gemini_complete_with_tools_injects_function_declarations():
    _require_gemini_native_tools()
    backend = _build_ai()
    fake_httpx = _FakeAsyncClientFactory(stream_batches=[_sse([_text_event("done")])])

    with patch("httpx.AsyncClient", fake_httpx):
        _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=AsyncMock(return_value={"ok": True}),
        )

    payload = fake_httpx.payloads[0]
    assert "tools" in payload
    assert "functionDeclarations" in payload["tools"][0]


def test_gemini_complete_tool_config_set_to_auto():
    _require_gemini_native_tools()
    backend = _build_ai()
    fake_httpx = _FakeAsyncClientFactory(stream_batches=[_sse([_text_event("done")])])

    with patch("httpx.AsyncClient", fake_httpx):
        _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=AsyncMock(return_value={"ok": True}),
        )

    payload = fake_httpx.payloads[0]
    assert payload["tool_config"]["function_calling_config"]["mode"] == "AUTO"


def test_gemini_complete_function_call_dispatches_tool():
    _require_gemini_native_tools()
    backend = _build_ai()
    dispatcher = AsyncMock(return_value={"content": "ok"})

    call_parts = [{"functionCall": {"name": "read_export_file", "args": {"path": "src/app.py"}}}]
    fake_httpx = _FakeAsyncClientFactory(
        stream_batches=[
            _sse([_function_event(call_parts)]),
            _sse([_text_event("tool round complete")]),
        ]
    )

    with patch("httpx.AsyncClient", fake_httpx):
        result = _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=dispatcher,
        )

    assert result == "tool round complete"
    dispatcher.assert_awaited_once_with("read_export_file", {"path": "src/app.py"})


def test_gemini_complete_thought_signature_echoed():
    _require_gemini_native_tools()
    _require_thought_signature_echo()
    backend = _build_ai()
    dispatcher = AsyncMock(return_value={"ok": True})
    thought_sig = "tsig-001"

    call_parts = [
        {"thoughtSignature": thought_sig},
        {"functionCall": {"name": "lint_code", "args": {"code": "x=1", "language": "python"}}},
    ]
    fake_httpx = _FakeAsyncClientFactory(
        stream_batches=[
            _sse([_function_event(call_parts)]),
            _sse([_text_event("BIG_FIXER_DONE")]),
        ]
    )

    with patch("httpx.AsyncClient", fake_httpx):
        _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=dispatcher,
        )

    second_payload = fake_httpx.payloads[1]
    contents = second_payload.get("contents", [])
    found = False
    for turn in contents:
        if turn.get("role") != "model":
            continue
        for part in turn.get("parts", []):
            echoed = part.get("thought")
            if echoed is None:
                echoed = part.get("thoughtSignature")
            if echoed == thought_sig:
                found = True
                break
    assert found


def test_gemini_complete_tool_loop_continues_until_text():
    _require_gemini_native_tools()
    backend = _build_ai()
    dispatcher = AsyncMock(return_value={"ok": True})

    call_parts = [{"functionCall": {"name": "run_code", "args": {"code": "print(1)", "language": "python"}}}]
    fake_httpx = _FakeAsyncClientFactory(
        stream_batches=[
            _sse([_function_event(call_parts)]),
            _sse([_text_event("final answer")]),
        ]
    )

    with patch("httpx.AsyncClient", fake_httpx):
        result = _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=dispatcher,
        )

    assert result == "final answer"
    assert fake_httpx.stream_calls == 2


def test_gemini_complete_no_dispatcher_with_function_call_raises():
    _require_gemini_native_tools()
    backend = _build_ai()

    call_parts = [{"functionCall": {"name": "read_export_file", "args": {"path": "a.py"}}}]
    fake_httpx = _FakeAsyncClientFactory(stream_batches=[_sse([_function_event(call_parts)])])

    with patch("httpx.AsyncClient", fake_httpx):
        with pytest.raises(RuntimeError):
            _call_gemini_complete(backend, tools=_sample_tools(), tool_dispatcher=None)


def test_gemini_complete_json_mode_and_tools_mutual_exclusion():
    _require_gemini_native_tools()
    backend = _build_ai()
    fake_httpx = _FakeAsyncClientFactory(stream_batches=[_sse([_text_event("ok")])])

    with patch("httpx.AsyncClient", fake_httpx):
        _call_gemini_complete(
            backend,
            json_mode=True,
            tools=_sample_tools(),
            tool_dispatcher=AsyncMock(return_value={"ok": True}),
        )

    payload = fake_httpx.payloads[0]
    gen_cfg = payload.get("generationConfig", {})
    # Session 57: field renamed response_mime_type → responseMimeType (camelCase per Gemini REST spec)
    assert "responseMimeType" not in gen_cfg  # json_mode must be suppressed when tools are provided
    assert "response_mime_type" not in gen_cfg  # guard: old snake_case form must never appear


# ---------------------------------------------------------------------------
# Group B — gemini-customtools backend routing (4)
# ---------------------------------------------------------------------------

def test_gemini_customtools_backend_routes_to_customtools_model():
    _require_complete_native_tools()
    backend = _build_ai()
    gemini_mock = AsyncMock(return_value="ok")

    with patch.object(backend, "_gemini_complete", gemini_mock):
        asyncio.run(
            backend.complete(
                system="sys",
                message="msg",
                backend="gemini-customtools",
                tools=_sample_tools(),
                tool_dispatcher=AsyncMock(return_value={"ok": True}),
                max_tokens=321,
            )
        )

    assert gemini_mock.await_count == 1
    assert gemini_mock.await_args.kwargs.get("model") == "gemini-3.1-pro-preview-customtools"


def test_gemini_customtools_backend_passes_tools_through():
    _require_complete_native_tools()
    backend = _build_ai()
    gemini_mock = AsyncMock(return_value="ok")
    tools = _sample_tools()

    with patch.object(backend, "_gemini_complete", gemini_mock):
        asyncio.run(
            backend.complete(
                system="sys",
                message="msg",
                backend="gemini-customtools",
                tools=tools,
                tool_dispatcher=AsyncMock(return_value={"ok": True}),
            )
        )

    assert gemini_mock.await_args.kwargs.get("tools") == tools


def test_gemini_customtools_backend_passes_dispatcher_through():
    _require_complete_native_tools()
    backend = _build_ai()
    gemini_mock = AsyncMock(return_value="ok")
    dispatcher = AsyncMock(return_value={"ok": True})

    with patch.object(backend, "_gemini_complete", gemini_mock):
        asyncio.run(
            backend.complete(
                system="sys",
                message="msg",
                backend="gemini-customtools",
                tools=_sample_tools(),
                tool_dispatcher=dispatcher,
            )
        )

    assert gemini_mock.await_args.kwargs.get("tool_dispatcher") is dispatcher


def test_gemini_customtools_raises_on_failure_no_internal_fallback():
    # Session 58: internal fallback removed — customtools backend raises immediately
    # on failure so the outer role chain (gemini-customtools → gemini-flash-high → claude)
    # can try the next backend cleanly without circular fallback.
    _require_complete_native_tools()
    backend = _build_ai()
    gemini_mock = AsyncMock(side_effect=RuntimeError("primary failed"))

    with patch.object(backend, "_gemini_complete", gemini_mock):
        with pytest.raises(RuntimeError, match="Gemini CustomTools backend failed"):
            asyncio.run(
                backend.complete(
                    system="sys",
                    message="msg",
                    backend="gemini-customtools",
                    tools=_sample_tools(),
                    tool_dispatcher=AsyncMock(return_value={"ok": True}),
                )
            )

    # Only one _gemini_complete call — no internal second attempt
    assert gemini_mock.await_count == 1
    first_model = gemini_mock.await_args_list[0].kwargs.get("model")
    assert first_model == "gemini-3.1-pro-preview-customtools"


# ---------------------------------------------------------------------------
# Group C — Big Fixer tool dispatch (8)
# ---------------------------------------------------------------------------

def test_big_fixer_uses_gemini_customtools_backend(tmp_path):
    pool, ai = _make_pool(tmp_path)
    pool._rerun_tests = AsyncMock(return_value=_make_summary(failed=0, passed=1, total=1))

    asyncio.run(
        pool._run_big_fixer(
            swarm_summary=_make_summary(failed=1, passed=0, total=1),
            export_dir=tmp_path,
            repair_history=[],
            project_context={"project_name": "demo"},
            session_id="s56-13",
        )
    )

    assert ai.complete.await_args.kwargs.get("backend") == "gemini-customtools"


def test_big_fixer_passes_tool_definitions(tmp_path):
    pool, ai = _make_pool(tmp_path)
    pool._rerun_tests = AsyncMock(return_value=_make_summary(failed=0, passed=1, total=1))

    asyncio.run(
        pool._run_big_fixer(
            swarm_summary=_make_summary(failed=1, passed=0, total=1),
            export_dir=tmp_path,
            repair_history=[],
            project_context={"name": "demo"},
            session_id="s56-14",
        )
    )

    assert ai.complete.await_args.kwargs.get("tools") == TOOL_DEFINITIONS


def test_big_fixer_passes_tool_dispatcher(tmp_path):
    pool, ai = _make_pool(tmp_path)
    pool._rerun_tests = AsyncMock(return_value=_make_summary(failed=0, passed=1, total=1))

    asyncio.run(
        pool._run_big_fixer(
            swarm_summary=_make_summary(failed=1, passed=0, total=1),
            export_dir=tmp_path,
            repair_history=[],
            project_context={"name": "demo"},
            session_id="s56-15",
        )
    )

    dispatcher = ai.complete.await_args.kwargs.get("tool_dispatcher")
    assert dispatcher is not None
    assert callable(dispatcher)


def test_big_fixer_dispatcher_bound_to_export_dir(tmp_path):
    pool, ai = _make_pool(tmp_path)
    pool._rerun_tests = AsyncMock(return_value=_make_summary(failed=0, passed=1, total=1))

    dispatch_mock = AsyncMock(return_value={"ok": True})
    with patch("core.swarm_tools.dispatch_tool", dispatch_mock):
        asyncio.run(
            pool._run_big_fixer(
                swarm_summary=_make_summary(failed=1, passed=0, total=1),
                export_dir=tmp_path,
                repair_history=[],
                project_context={"name": "demo"},
                session_id="s56-16",
            )
        )

        dispatcher = ai.complete.await_args.kwargs.get("tool_dispatcher")
        result = asyncio.run(dispatcher("read_export_file", {"path": "src/app.py"}))

    assert result == {"ok": True}
    dispatch_mock.assert_awaited_once_with(
        "read_export_file",
        {"path": "src/app.py"},
        export_dir=str(tmp_path),
    )


def test_big_fixer_reruns_tests_after_complete(tmp_path):
    pool, _ = _make_pool(tmp_path)
    pool._rerun_tests = AsyncMock(return_value=_make_summary(failed=0, passed=1, total=1))

    asyncio.run(
        pool._run_big_fixer(
            swarm_summary=_make_summary(failed=1, passed=0, total=1),
            export_dir=tmp_path,
            repair_history=[],
            project_context={"name": "demo"},
            session_id="s56-17",
        )
    )

    pool._rerun_tests.assert_awaited_once()


def test_big_fixer_returns_on_zero_failures(tmp_path):
    pool, ai = _make_pool(tmp_path)
    pool._rerun_tests = AsyncMock(return_value=_make_summary(failed=0, passed=3, total=3))

    summary, history = asyncio.run(
        pool._run_big_fixer(
            swarm_summary=_make_summary(failed=2, passed=1, total=3),
            export_dir=tmp_path,
            repair_history=[],
            project_context={"name": "demo"},
            session_id="s56-18",
        )
    )

    assert ai.complete.await_count == 1
    assert summary.failed == 0
    assert isinstance(history, list)


def test_big_fixer_continues_to_attempt_2_on_remaining_failures(tmp_path):
    pool, ai = _make_pool(tmp_path)
    pool._rerun_tests = AsyncMock(
        side_effect=[
            _make_summary(failed=1, passed=0, total=1),
            _make_summary(failed=0, passed=1, total=1),
        ]
    )

    summary, history = asyncio.run(
        pool._run_big_fixer(
            swarm_summary=_make_summary(failed=2, passed=0, total=2),
            export_dir=tmp_path,
            repair_history=[],
            project_context={"name": "demo"},
            session_id="s56-19",
        )
    )

    assert ai.complete.await_count == 2
    assert summary.failed == 0
    assert len(history) == 2


def test_big_fixer_returns_tuple_swarm_summary_and_history(tmp_path):
    pool, _ = _make_pool(tmp_path)
    pool._rerun_tests = AsyncMock(return_value=_make_summary(failed=0, passed=1, total=1))

    result = asyncio.run(
        pool._run_big_fixer(
            swarm_summary=_make_summary(failed=1, passed=0, total=1),
            export_dir=tmp_path,
            repair_history=[],
            project_context={"name": "demo"},
            session_id="s56-20",
        )
    )

    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], SwarmSummary)
    assert isinstance(result[1], list)


# ---------------------------------------------------------------------------
# Group D — thoughtSignature 400 prevention (3)
# ---------------------------------------------------------------------------

def test_thought_signature_included_in_model_turn():
    _require_gemini_native_tools()
    _require_thought_signature_echo()
    backend = _build_ai()
    dispatcher = AsyncMock(return_value={"ok": True})
    thought_sig = "signature-123"

    call_parts = [
        {"thoughtSignature": thought_sig},
        {"functionCall": {"name": "read_export_file", "args": {"path": "src/a.py"}}},
    ]
    fake_httpx = _FakeAsyncClientFactory(
        stream_batches=[
            _sse([_function_event(call_parts)]),
            _sse([_text_event("done")]),
        ]
    )

    with patch("httpx.AsyncClient", fake_httpx):
        _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=dispatcher,
        )

    model_turns = [c for c in fake_httpx.payloads[1].get("contents", []) if c.get("role") == "model"]
    assert any(
        any(
            (part.get("thought") == thought_sig) or (part.get("thoughtSignature") == thought_sig)
            for part in turn.get("parts", [])
        )
        for turn in model_turns
    )


def test_thought_signature_is_exact_echo():
    _require_gemini_native_tools()
    _require_thought_signature_echo()
    backend = _build_ai()
    dispatcher = AsyncMock(return_value={"ok": True})
    thought_sig = "sig:/+==ABC_987"

    call_parts = [
        {"thoughtSignature": thought_sig},
        {"functionCall": {"name": "lint_code", "args": {"code": "x=1", "language": "python"}}},
    ]
    fake_httpx = _FakeAsyncClientFactory(
        stream_batches=[
            _sse([_function_event(call_parts)]),
            _sse([_text_event("done")]),
        ]
    )

    with patch("httpx.AsyncClient", fake_httpx):
        _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=dispatcher,
        )

    payload = fake_httpx.payloads[1]
    echoed = None
    for turn in payload.get("contents", []):
        for part in turn.get("parts", []):
            if "thought" in part:
                echoed = part["thought"]
                break
            if "thoughtSignature" in part:
                echoed = part["thoughtSignature"]
                break
    assert echoed == thought_sig


def test_multiple_function_calls_in_one_turn_all_dispatched():
    _require_gemini_native_tools()
    backend = _build_ai()
    dispatcher = AsyncMock(side_effect=[{"a": 1}, {"b": 2}])

    call_parts = [
        {"functionCall": {"name": "read_export_file", "args": {"path": "src/a.py"}}},
        {"functionCall": {"name": "read_export_file", "args": {"path": "src/b.py"}}},
    ]
    fake_httpx = _FakeAsyncClientFactory(
        stream_batches=[
            _sse([_function_event(call_parts)]),
            _sse([_text_event("done")]),
        ]
    )

    with patch("httpx.AsyncClient", fake_httpx):
        _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=dispatcher,
        )

    assert dispatcher.await_count == 2
    first = dispatcher.await_args_list[0].args
    second = dispatcher.await_args_list[1].args
    assert first == ("read_export_file", {"path": "src/a.py"})
    assert second == ("read_export_file", {"path": "src/b.py"})


# ---------------------------------------------------------------------------
# Group E — regression (3)
# ---------------------------------------------------------------------------

def test_existing_gemini_complete_no_tools_unchanged():
    backend = _build_ai()
    fake_httpx = _FakeAsyncClientFactory(stream_batches=[_sse([_text_event("regression-safe")])])

    with patch("httpx.AsyncClient", fake_httpx):
        result = _call_gemini_complete(backend, json_mode=False)

    assert result == "regression-safe"


def test_big_fixer_max_attempts_respected(tmp_path):
    pool, ai = _make_pool(tmp_path, max_attempts=2)
    pool._rerun_tests = AsyncMock(
        side_effect=[
            _make_summary(failed=1, passed=0, total=1),
            _make_summary(failed=1, passed=0, total=1),
        ]
    )

    summary, history = asyncio.run(
        pool._run_big_fixer(
            swarm_summary=_make_summary(failed=2, passed=0, total=2),
            export_dir=tmp_path,
            repair_history=[],
            project_context={"name": "demo"},
            session_id="s56-25",
        )
    )

    assert ai.complete.await_count == 2
    assert summary.failed == 1
    assert len(history) == 2


def test_big_fixer_all_backends_fail_gracefully(tmp_path):
    pool, ai = _make_pool(tmp_path, max_attempts=2)
    ai.complete = AsyncMock(side_effect=RuntimeError("backend boom"))
    pool._rerun_tests = AsyncMock(return_value=_make_summary(failed=0, passed=1, total=1))

    initial = _make_summary(failed=3, passed=0, total=3)
    summary, history = asyncio.run(
        pool._run_big_fixer(
            swarm_summary=initial,
            export_dir=tmp_path,
            repair_history=[],
            project_context={"name": "demo"},
            session_id="s56-26",
        )
    )

    # Session 58: chain is 3 backends (gemini-customtools, gemini-flash-high, claude)
    # × 2 max_attempts = 6 total calls
    assert ai.complete.await_count == 6
    pool._rerun_tests.assert_not_called()
    assert isinstance(summary, SwarmSummary)
    assert summary.failed == 3
    assert isinstance(history, list)
    assert len(history) == 2
    assert all(entry.get("note") == "all backends failed" for entry in history)


# ---------------------------------------------------------------------------
# Group F — tool_mode="ANY" + declare_done interception (Session 58)
# ---------------------------------------------------------------------------

def _supports_tool_mode() -> bool:
    sig = inspect.signature(AIBackend._gemini_complete)
    return "tool_mode" in sig.parameters


def _require_tool_mode():
    if not _supports_tool_mode():
        pytest.skip("tool_mode parameter not yet present in _gemini_complete")


def test_gemini_complete_tool_mode_any_sets_function_config():
    """tool_mode='ANY' must set function_calling_config mode to 'ANY' in the Gemini payload."""
    _require_gemini_native_tools()
    _require_tool_mode()
    backend = _build_ai()
    fake_httpx = _FakeAsyncClientFactory(stream_batches=[_sse([_text_event("done")])])

    with patch("httpx.AsyncClient", fake_httpx):
        _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=AsyncMock(return_value={"ok": True}),
            tool_mode="ANY",
        )

    payload = fake_httpx.payloads[0]
    assert payload["tool_config"]["function_calling_config"]["mode"] == "ANY"


def test_gemini_complete_tool_mode_any_skips_thinking_config():
    """tool_mode='ANY' must NOT set thinkingConfig — avoids MINIMAL+functionDeclarations HTTP 400."""
    _require_gemini_native_tools()
    _require_tool_mode()
    backend = _build_ai()
    fake_httpx = _FakeAsyncClientFactory(stream_batches=[_sse([_text_event("done")])])

    with patch("httpx.AsyncClient", fake_httpx):
        _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=AsyncMock(return_value={"ok": True}),
            tool_mode="ANY",
        )

    gen_cfg = fake_httpx.payloads[0].get("generationConfig", {})
    assert "thinkingConfig" not in gen_cfg, (
        "tool_mode='ANY' must omit thinkingConfig to avoid MINIMAL+functionDeclarations 400"
    )


def test_gemini_complete_declare_done_intercepted_returns_summary():
    """declare_done call must be intercepted before dispatch and return summary as text."""
    _require_gemini_native_tools()
    _require_tool_mode()
    backend = _build_ai()
    dispatcher = AsyncMock(return_value={"ok": True})

    declare_parts = [
        {"functionCall": {
            "name": "declare_done",
            "args": {"summary": "Fixed import error in processor.py"},
        }}
    ]
    fake_httpx = _FakeAsyncClientFactory(
        stream_batches=[_sse([_function_event(declare_parts)])]
    )

    with patch("httpx.AsyncClient", fake_httpx):
        result = _call_gemini_complete(
            backend,
            tools=_sample_tools(),
            tool_dispatcher=dispatcher,
            tool_mode="ANY",
        )

    assert result == "Fixed import error in processor.py"
    # dispatcher must NOT have been called — declare_done is intercepted before dispatch
    dispatcher.assert_not_awaited()
    # Only one API call — loop terminates on declare_done, no second round
    assert fake_httpx.stream_calls == 1


def test_big_fixer_passes_tool_mode_any(tmp_path):
    """_run_big_fixer must pass tool_mode='ANY' to complete() (not thinking_level='MINIMAL')."""
    pool, ai = _make_pool(tmp_path)
    pool._rerun_tests = AsyncMock(return_value=_make_summary(failed=0, passed=1, total=1))

    asyncio.run(
        pool._run_big_fixer(
            swarm_summary=_make_summary(failed=1, passed=0, total=1),
            export_dir=tmp_path,
            repair_history=[],
            project_context={"name": "demo"},
            session_id="s58-tool-mode",
        )
    )

    call_kwargs = ai.complete.await_args.kwargs
    assert call_kwargs.get("tool_mode") == "ANY", (
        f"Expected tool_mode='ANY' but got {call_kwargs.get('tool_mode')!r}. "
        "thinking_level='MINIMAL' causes HTTP 400 on customtools+functionDeclarations."
    )
    # Ensure the old MINIMAL thinking_level is NOT passed
    assert call_kwargs.get("thinking_level") is None, (
        "thinking_level should not be set for Big Fixer — tool_mode='ANY' replaces it"
    )
