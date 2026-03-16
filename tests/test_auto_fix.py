import asyncio

from core.auto_fix import AutoFixLoop
from core.tester_swarm import SwarmSummary, TestResult


def test_run_returns_empty_summary_when_no_failures(mock_ai_backend, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    loop = AutoFixLoop(mock_ai_backend)
    swarm = SwarmSummary(
        results=[
            TestResult(test_file="tests/test_sample.py", source_file="core/sample.py", passed=True, failure_output="")
        ]
    )

    summary = asyncio.run(loop.run(swarm, export_dir=str(tmp_path), session_id="sess-empty"))

    assert summary.fixed == 0
    assert summary.failed == 0
    assert summary.restored == 0


def test_fix_succeeds_on_first_attempt(mock_ai_backend, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source_file = tmp_path / "sample_module.py"
    test_file = tmp_path / "test_sample_module.py"

    source_file.write_text("def add(a, b):\n    return 3\n", encoding="utf-8")
    test_file.write_text(
        "from sample_module import add\n\n"
        "def test_add():\n"
        "    assert add(1, 1) == 2\n",
        encoding="utf-8",
    )

    mock_ai_backend.response = "def add(a, b):\n    return a + b\n"

    loop = AutoFixLoop(mock_ai_backend)
    swarm = SwarmSummary(
        results=[
            TestResult(
                test_file=str(test_file),
                source_file=str(source_file),
                passed=False,
                failure_output="assert 3 == 2",
            )
        ]
    )

    summary = asyncio.run(loop.run(swarm, export_dir=str(tmp_path), session_id="sess-success"))

    assert summary.fixed == 1
    assert summary.failed == 0
    assert summary.restored == 0
    assert len(summary.attempts) == 1
    assert summary.attempts[0].success is True
    assert summary.attempts[0].attempt_number == 1


def test_fix_restores_file_after_3_failures(mock_ai_backend, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source_file = tmp_path / "broken_module.py"
    test_file = tmp_path / "test_broken_module.py"

    original = "def value():\n    return 1\n"
    source_file.write_text(original, encoding="utf-8")
    test_file.write_text(
        "from broken_module import value\n\n"
        "def test_value():\n"
        "    assert value() == 2\n",
        encoding="utf-8",
    )

    mock_ai_backend.response = "def value():\n    return 0\n"

    loop = AutoFixLoop(mock_ai_backend)
    swarm = SwarmSummary(
        results=[
            TestResult(
                test_file=str(test_file),
                source_file=str(source_file),
                passed=False,
                failure_output="assert 1 == 2",
            )
        ]
    )

    summary = asyncio.run(loop.run(swarm, export_dir=str(tmp_path), session_id="sess-restore"))

    assert summary.fixed == 0
    assert summary.failed == 1
    assert summary.restored == 1
    assert len(summary.attempts) == 3
    assert source_file.read_text(encoding="utf-8") == original


def test_giant_brain_review_called_with_diffs(mock_ai_backend, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source_file = tmp_path / "review_module.py"
    test_file = tmp_path / "test_review_module.py"

    source_file.write_text("def flag():\n    return False\n", encoding="utf-8")
    test_file.write_text(
        "from review_module import flag\n\n"
        "def test_flag():\n"
        "    assert flag() is True\n",
        encoding="utf-8",
    )

    async def custom_complete(**kwargs):
        mock_ai_backend.calls.append(kwargs)
        if "Review these diffs" in kwargs.get("message", ""):
            return "Looks good overall; low residual risk."
        return "def flag():\n    return True\n"

    mock_ai_backend.complete = custom_complete

    loop = AutoFixLoop(mock_ai_backend)
    swarm = SwarmSummary(
        results=[
            TestResult(
                test_file=str(test_file),
                source_file=str(source_file),
                passed=False,
                failure_output="assert False is True",
            )
        ]
    )

    summary = asyncio.run(loop.run(swarm, export_dir=str(tmp_path), session_id="sess-review"))

    assert summary.fixed == 1
    assert any("Review these diffs" in call.get("message", "") for call in mock_ai_backend.calls)


def test_retry_continues_when_ai_complete_raises_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source_file = tmp_path / "retry_module.py"
    test_file = tmp_path / "test_retry_module.py"

    source_file.write_text("def value():\n    return 1\n", encoding="utf-8")
    test_file.write_text(
        "from retry_module import value\n\n"
        "def test_value():\n"
        "    assert value() == 2\n",
        encoding="utf-8",
    )

    calls = {"count": 0}
    events = []

    async def flaky_complete(**kwargs):
        calls["count"] += 1
        if "Review these diffs" in kwargs.get("message", ""):
            return "Review complete."
        if calls["count"] == 1:
            raise RuntimeError("temporary backend timeout")
        return "def value():\n    return 2\n"

    async def collect_event(event_type, payload):
        events.append((event_type, payload))

    class FlakyBackend:
        complete = staticmethod(flaky_complete)

    loop = AutoFixLoop(FlakyBackend(), sse_broadcast=collect_event)
    swarm = SwarmSummary(
        results=[
            TestResult(
                test_file=str(test_file),
                source_file=str(source_file),
                passed=False,
                failure_output="assert 1 == 2",
            )
        ]
    )

    summary = asyncio.run(loop.run(swarm, export_dir=str(tmp_path), session_id="sess-retry-ai-exc"))

    failed_events = [event for event in events if event[0] == "fix_failed"]

    assert len(failed_events) == 1
    assert failed_events[0][1]["attempt"] == 1
    assert "temporary backend timeout" in failed_events[0][1]["error"]
    assert summary.fixed == 1
    assert summary.failed == 0
    assert calls["count"] >= 2
