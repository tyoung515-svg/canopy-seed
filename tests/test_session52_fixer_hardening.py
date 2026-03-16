"""
Session 52 tests — Fixer hardening + codex → flash migration.

Covers:
  _apply_single_fix Fallback 3 — remapped path that doesn't exist gets created
    - alias remap fires but remapped file missing → file created, repair proceeds
    - fallback 3 creation failure logs warning and returns cleanly (no exception)

  Big Fixer backend chain
    - Big Fixer uses gemini-flash as primary (not openai-codex)

  Mechanical fix backend
    - _run_fixer_pass dispatches mechanical fixes via gemini-flash (not openai-codex)
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
    mock_ai.complete = AsyncMock(return_value="# fixed content")
    pool = AgentPool(ai_backend=mock_ai)
    return pool, mock_ai


# ---------------------------------------------------------------------------
# _apply_single_fix — Fallback 3 now fires even after alias remap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback3_fires_after_alias_remap(tmp_path):
    """When alias remap finds a plausible path but the file doesn't exist,
    Fallback 3 must create the file so the repair AI call can proceed."""
    pool, mock_ai = _make_pool(tmp_path)

    # Real package dir is 'battery_spec_scanner_hub' (with __init__.py)
    real_pkg = tmp_path / "battery_spec_scanner_hub"
    real_pkg.mkdir()
    (real_pkg / "__init__.py").write_text("")
    # hub.py does NOT exist yet

    fix = {
        "file": "battery_hub/hub.py",  # alias name — doesn't exist as dir
        "complexity": "mechanical",
        "description": "Add missing hub module",
        "action": "add_feature",
    }

    manifest = {"fixes": [fix]}
    await pool._run_fixer_pass(manifest, tmp_path)

    # After the fix pass, hub.py must now exist (created by Fallback 3 + AI write)
    fixed_file = real_pkg / "hub.py"
    assert fixed_file.exists(), (
        "Fallback 3 should have created hub.py at the remapped path"
    )


@pytest.mark.asyncio
async def test_fallback3_fires_for_nonexistent_file_in_existing_pkg(tmp_path):
    """When Giant Brain targets a file that doesn't exist (no alias needed),
    Fallback 3 creates it so the repair can proceed — not a read error."""
    pool, mock_ai = _make_pool(tmp_path)

    pkg = tmp_path / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # exceptions.py does NOT exist

    fix = {
        "file": "myapp/exceptions.py",
        "complexity": "mechanical",
        "description": "Add missing exceptions module",
        "action": "add_feature",
    }

    manifest = {"fixes": [fix]}
    await pool._run_fixer_pass(manifest, tmp_path)

    assert (pkg / "exceptions.py").exists(), (
        "Fallback 3 should create exceptions.py when it doesn't exist"
    )
    # AI should have been called (not skipped with a warning)
    assert mock_ai.complete.called


@pytest.mark.asyncio
async def test_fallback3_creation_failure_logs_warning_and_skips(tmp_path):
    """If Fallback 3's mkdir/write raises, the function logs a warning and
    returns cleanly without propagating the exception."""
    pool, mock_ai = _make_pool(tmp_path)

    fix = {
        "file": "missing_pkg/missing_file.py",
        "complexity": "mechanical",
        "description": "Fix something",
        "action": "fix_content",
    }

    manifest = {"fixes": [fix]}

    # Patch Path.mkdir to raise so Fallback 3 fails
    original_mkdir = Path.mkdir

    def _failing_mkdir(self, *args, **kwargs):
        if "missing_pkg" in str(self):
            raise PermissionError("Simulated permission denied")
        original_mkdir(self, *args, **kwargs)

    with patch.object(Path, "mkdir", _failing_mkdir):
        # Should not raise — logs warning and returns
        await pool._run_fixer_pass(manifest, tmp_path)

    # AI must NOT have been called (we returned early)
    mock_ai.complete.assert_not_called()


# ---------------------------------------------------------------------------
# Big Fixer backend — gemini-flash primary
# ---------------------------------------------------------------------------

def test_big_fixer_backend_chain_is_gemini_customtools():
    """ADR-030 Big Fixer backend must use gemini-customtools (Session 56 rewrite).
    Session 52 migrated from openai-codex → gemini-flash.
    Session 56 migrated from gemini-flash → gemini-customtools (native tool calling).
    Session 58 migrated from hardcoded tuple → get_role_chain("big_fixer") dynamic lookup.
    Verified by inspecting _run_big_fixer source for the role-chain call pattern."""
    import inspect
    from core.agent_pool import AgentPool

    src = inspect.getsource(AgentPool._run_big_fixer)
    # Session 58: hardcoded ("gemini-customtools", "gemini") tuple replaced by
    # self.ai.get_role_chain("big_fixer") so the env/profile system is honoured.
    assert 'get_role_chain' in src, (
        "_run_big_fixer must use get_role_chain() for its backend chain (Session 58)"
    )
    assert '"big_fixer"' in src, (
        '_run_big_fixer must pass "big_fixer" role key to get_role_chain (Session 58)'
    )
    assert '"openai-codex"' not in src, (
        "openai-codex must not appear in the Big Fixer backend loop"
    )


# ---------------------------------------------------------------------------
# Mechanical fix backend — gemini-flash
# ---------------------------------------------------------------------------

def test_mechanical_fix_uses_gemini_flash_not_codex(tmp_path):
    """_run_fixer_pass mechanical fixes must use gemini-flash backend."""
    target = tmp_path / "module.py"
    target.write_text("def old(): pass")

    manifest = {
        "fixes": [
            {
                "file": "module.py",
                "complexity": "mechanical",
                "description": "Rename old to new_fn",
                "action": "rename_function",
            }
        ]
    }

    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value="def new_fn(): pass")

    from core.agent_pool import AgentPool
    pool = AgentPool(ai_backend=mock_ai)
    asyncio.run(pool._run_fixer_pass(manifest, tmp_path))

    assert mock_ai.complete.called
    backend_used = mock_ai.complete.call_args.kwargs.get("backend")
    assert backend_used == "gemini-flash", (
        f"Mechanical fix must use gemini-flash, got: {backend_used}"
    )
    assert backend_used != "openai-codex"
