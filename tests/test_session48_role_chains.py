"""
Session 48 tests — ADR-039 role chain wiring.

Covers:
  Phase 0 — contract_generator role chain
    - get_role_chain("contract_generator") default is gemini-flash,qwen-122b
      (Session 51: swapped from qwen-122b,openai-codex — Qwen consumed all 3
       revision attempts before payload failure; flash is more reliable)
    - ROLE_CONTRACT_CHAIN env override is respected
    - _generate_contract uses primary backend (gemini-flash)
    - _generate_contract falls back on exception
    - _generate_contract raises when all backends exhausted

  Giant Brain — giant_brain role chain
    - get_role_chain("giant_brain") default is gemini-flash,gemini,claude
    - ROLE_GIANT_BRAIN_CHAIN env override is respected
    - _giant_brain_audit returns manifest on clean JSON
    - _giant_brain_audit retries at 2x tokens on truncation error
    - _giant_brain_audit moves to next backend when 2x retry also truncates
    - _giant_brain_audit returns None when all backends exhausted

  Bug 2 — complexity tier boundary
    - score 2 resolves to lite tier
    - score 3 resolves to standard tier (boundary raised from 3 to 2)

  Bug 3 — tester swarm pycache purge
    - _purge_import_cache removes __pycache__ dirs
    - _purge_import_cache removes .pytest_cache
    - run() resets _editable_install_done for the export key
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ai_backend import AIBackend

# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_ai(settings=None):
    ai = MagicMock(spec=AIBackend)
    ai.settings = settings
    # Delegate get_role_chain to the real implementation
    ai.get_role_chain = lambda role: AIBackend.get_role_chain(ai, role)
    return ai


def _make_settings(**overrides):
    s = MagicMock()
    s.ROLE_CONTRACT_CHAIN = overrides.get("ROLE_CONTRACT_CHAIN", None)
    s.ROLE_GIANT_BRAIN_CHAIN = overrides.get("ROLE_GIANT_BRAIN_CHAIN", None)
    # return None for everything else so default kicks in
    s.__getattr__ = lambda self, name: None
    return s


MINIMAL_MANIFEST = json.dumps({
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

# ─── Phase 0 — contract_generator role chain ──────────────────────────────────

def test_contract_generator_default_chain():
    ai = _make_ai()
    chain = ai.get_role_chain("contract_generator")
    # Session 58 post-run: Flash 3.0 HIGH+schema → "response is not a JSON object" in production log.
    # customtools (Pro 3.1, thinking_level=LOW) is reliable for schema-constrained contract output.
    assert chain == ["gemini-customtools", "gemini-flash-high", "claude"], (
        "default contract_generator chain is gemini-customtools,gemini-flash-high,claude "
        "(via gemini profile — Session 58 post-run fix)"
    )


def test_contract_generator_env_override():
    with patch.dict("os.environ", {"ROLE_CONTRACT_CHAIN": "openai-codex,gemini-flash"}):
        import importlib
        import config.settings as _s
        importlib.reload(_s)
        s = _s.Settings()
        assert s.ROLE_CONTRACT_CHAIN == "openai-codex,gemini-flash"
        ai = _make_ai(settings=s)
        chain = ai.get_role_chain("contract_generator")
        assert chain == ["openai-codex", "gemini-flash"]


# ─── Giant Brain — giant_brain role chain ─────────────────────────────────────

def test_giant_brain_default_chain():
    ai = _make_ai()
    chain = ai.get_role_chain("giant_brain")
    # Session 58 post-run: Flash 3.0 HIGH+JSON unreliable for large analysis prompts.
    # customtools (Pro 3.1, thinking_level=LOW) chosen as primary for reliable JSON manifest output.
    assert chain == ["gemini-customtools", "gemini-flash-high", "claude"], (
        "default giant_brain chain is gemini-customtools,gemini-flash-high,claude "
        "(via gemini profile — Session 58 post-run fix)"
    )


def test_giant_brain_env_override():
    with patch.dict("os.environ", {"ROLE_GIANT_BRAIN_CHAIN": "gemini,claude"}):
        import importlib
        import config.settings as _s
        importlib.reload(_s)
        s = _s.Settings()
        assert s.ROLE_GIANT_BRAIN_CHAIN == "gemini,claude"
        ai = _make_ai(settings=s)
        chain = ai.get_role_chain("giant_brain")
        assert chain == ["gemini", "claude"]


@pytest.mark.asyncio
async def test_giant_brain_audit_returns_manifest_on_valid_json(tmp_path):
    """_giant_brain_audit returns a parsed manifest when backend returns valid JSON."""
    from core.agent_pool import AgentPool

    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.ai.get_role_chain = MagicMock(return_value=["gemini-flash", "gemini", "claude"])
    pool.ai.complete = AsyncMock(return_value=MINIMAL_MANIFEST)
    pool._collect_source_tree = MagicMock(return_value={"myapp/cli.py": "import sys"})

    result = await AgentPool._giant_brain_audit(
        pool,
        export_dir=tmp_path,
        test_output="FAILED test_cli.py::test_main",
    )

    assert result is not None
    assert "fixes" in result
    assert result["fixes"][0]["file"] == "myapp/cli.py"
    # Should have tried primary backend first
    pool.ai.complete.assert_called()
    first_call_kwargs = pool.ai.complete.call_args_list[0].kwargs
    assert first_call_kwargs.get("backend") == "gemini-flash"


@pytest.mark.asyncio
async def test_giant_brain_audit_retries_2x_tokens_on_truncation(tmp_path):
    """When JSON is truncated, _giant_brain_audit retries same backend at 2x tokens."""
    from core.agent_pool import AgentPool

    truncated = '{"summary": "missing import", "escalate": false, "fixes": [{"file": "x.py"'
    call_count = 0

    async def _side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        max_tok = kwargs.get("max_tokens", 0)
        if max_tok <= 16384:
            return truncated  # first call: truncated
        return MINIMAL_MANIFEST  # second call (2x budget): valid

    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.ai.get_role_chain = MagicMock(return_value=["gemini-flash"])
    pool.ai.complete = AsyncMock(side_effect=_side_effect)
    pool._collect_source_tree = MagicMock(return_value={"x.py": "pass"})

    result = await AgentPool._giant_brain_audit(
        pool,
        export_dir=tmp_path,
        test_output="FAILED",
    )

    assert result is not None, "Should recover from truncation via 2x retry"
    assert call_count == 2, "Should have called backend exactly twice (base + 2x)"


@pytest.mark.asyncio
async def test_giant_brain_audit_falls_back_when_2x_also_truncates(tmp_path):
    """When 2x retry also truncates, moves to next backend in chain."""
    from core.agent_pool import AgentPool

    truncated = '{"summary": "oops", "escalate": false, "fixes": [{"file":'
    backends_seen = []

    async def _side_effect(**kwargs):
        backends_seen.append(kwargs.get("backend"))
        if kwargs.get("backend") == "gemini":
            return MINIMAL_MANIFEST  # second backend succeeds
        return truncated  # primary truncates even at 2x

    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.ai.get_role_chain = MagicMock(return_value=["gemini-flash", "gemini"])
    pool.ai.complete = AsyncMock(side_effect=_side_effect)
    pool._collect_source_tree = MagicMock(return_value={"x.py": "pass"})

    result = await AgentPool._giant_brain_audit(
        pool,
        export_dir=tmp_path,
        test_output="FAILED",
    )

    assert result is not None, "Should fall back to gemini and succeed"
    # gemini-flash called twice (base + 2x), then gemini called once
    gemini_flash_calls = [b for b in backends_seen if b == "gemini-flash"]
    assert len(gemini_flash_calls) == 2
    assert "gemini" in backends_seen


@pytest.mark.asyncio
async def test_giant_brain_audit_returns_none_when_all_exhausted(tmp_path):
    """Returns None when all backends fail."""
    from core.agent_pool import AgentPool

    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.ai.get_role_chain = MagicMock(return_value=["gemini-flash", "gemini"])
    pool.ai.complete = AsyncMock(side_effect=RuntimeError("API unavailable"))
    pool._collect_source_tree = MagicMock(return_value={"x.py": "pass"})

    result = await AgentPool._giant_brain_audit(
        pool,
        export_dir=tmp_path,
        test_output="FAILED",
    )

    assert result is None


# ─── Bug 2 — complexity tier boundary ─────────────────────────────────────────

def test_score_2_resolves_to_lite():
    """Score of 2 stays in lite tier (T1 = flash-lite)."""
    from core.complexity_judge import _resolve_tier
    thresholds = {
        "tier_bounds": [[0, 2], [3, 6], [7, 10], [11, 13], [14, 15]],
        "tier_names": ["lite", "standard", "brain", "heavy", "escalate"],
        "tier_models": [
            "gemini-flash-lite", "gemini-flash",
            "claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-6",
        ],
    }
    tier, model, flag = _resolve_tier(2, thresholds)
    assert tier == "lite"


def test_score_3_resolves_to_standard():
    """Score of 3 now resolves to standard tier (T2 = flash), not lite."""
    from core.complexity_judge import _resolve_tier
    thresholds = {
        "tier_bounds": [[0, 2], [3, 6], [7, 10], [11, 13], [14, 15]],
        "tier_names": ["lite", "standard", "brain", "heavy", "escalate"],
        "tier_models": [
            "gemini-flash-lite", "gemini-flash",
            "claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-6",
        ],
    }
    tier, model, flag = _resolve_tier(3, thresholds)
    assert tier == "standard", (
        "score=3 must map to standard (T2/flash) after boundary raise"
    )


def test_score_6_resolves_to_standard():
    """Score of 6 is the top of the standard band."""
    from core.complexity_judge import _resolve_tier
    thresholds = {
        "tier_bounds": [[0, 2], [3, 6], [7, 10], [11, 13], [14, 15]],
        "tier_names": ["lite", "standard", "brain", "heavy", "escalate"],
        "tier_models": [
            "gemini-flash-lite", "gemini-flash",
            "claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-6",
        ],
    }
    tier, _, _ = _resolve_tier(6, thresholds)
    assert tier == "standard"


# ─── Bug 3 — tester swarm pycache purge ───────────────────────────────────────

def test_purge_import_cache_removes_pycache(tmp_path):
    """_purge_import_cache deletes __pycache__ dirs recursively."""
    from core.tester_swarm import TesterSwarm

    pkg = tmp_path / "myapp"
    pkg.mkdir()
    pycache = pkg / "__pycache__"
    pycache.mkdir()
    (pycache / "cli.cpython-310.pyc").write_bytes(b"fake")

    swarm = TesterSwarm(ai_backend=MagicMock())
    swarm._purge_import_cache(tmp_path)

    assert not pycache.exists(), "__pycache__ dir should have been removed"


def test_purge_import_cache_removes_pytest_cache(tmp_path):
    """_purge_import_cache deletes .pytest_cache at the root."""
    from core.tester_swarm import TesterSwarm

    cache = tmp_path / ".pytest_cache"
    cache.mkdir()
    (cache / "v").mkdir()

    swarm = TesterSwarm(ai_backend=MagicMock())
    swarm._purge_import_cache(tmp_path)

    assert not cache.exists(), ".pytest_cache dir should have been removed"


def test_purge_import_cache_removes_nested_pycache(tmp_path):
    """_purge_import_cache handles __pycache__ at multiple nesting levels."""
    from core.tester_swarm import TesterSwarm

    nested = tmp_path / "myapp" / "sub"
    nested.mkdir(parents=True)
    deep_cache = nested / "__pycache__"
    deep_cache.mkdir()
    (deep_cache / "foo.pyc").write_bytes(b"x")

    swarm = TesterSwarm(ai_backend=MagicMock())
    swarm._purge_import_cache(tmp_path)

    assert not deep_cache.exists()


@pytest.mark.asyncio
async def test_run_resets_editable_install_done(tmp_path):
    """run() clears _editable_install_done so pip reinstalls on each pipeline run."""
    from core.tester_swarm import TesterSwarm

    swarm = TesterSwarm(ai_backend=MagicMock())
    export_key = str(tmp_path.resolve())
    swarm._editable_install_done[export_key] = True  # simulate stale state

    # Patch out everything that would actually run
    swarm._ensure_db = AsyncMock()
    swarm._detect_project_language = MagicMock(return_value="python")
    swarm._discover_test_files = MagicMock(return_value=[])
    swarm._broadcast = AsyncMock()

    await swarm.run(str(tmp_path))

    assert export_key not in swarm._editable_install_done or \
           swarm._editable_install_done.get(export_key) is None, (
        "run() should clear the install-done flag so pip reinstalls"
    )
