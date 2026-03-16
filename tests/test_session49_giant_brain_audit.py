"""
Session 49 tests — Giant Brain in-depth audit prompt + stale file handling.

Covers:
  Giant Brain audit prompt (ADR-039 Phase 2)
    - canonical_test_file is injected into system prompt
    - delete_file action removes the target file in _run_fixer_pass
    - delete_file on already-absent file is a no-op

  Stale test purge in _run_giant_brain_repair
    - stale test files are removed before Giant Brain passes when memory is provided
    - canonical test file is NOT removed
    - non-test files in tests/ are NOT removed
    - memory=None skips purge (no error)
"""

import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.agent_pool import AgentPool, SwarmMemory, ContractArtifact
from core.ai_backend import AIBackend

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


def _make_pool(return_value=MINIMAL_MANIFEST, side_effect=None):
    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.ai.get_role_chain = MagicMock(return_value=["claude"])
    if side_effect is not None:
        pool.ai.complete = AsyncMock(side_effect=side_effect)
    else:
        pool.ai.complete = AsyncMock(return_value=return_value)
    pool._collect_source_tree = MagicMock(return_value={"myapp/cli.py": "import sys"})
    return pool


def _make_memory(test_file_name: str) -> SwarmMemory:
    mem = SwarmMemory()
    contract = ContractArtifact(
        test_file_name=test_file_name,
        test_file_content="# test",
        public_api_summary="CLI app",
    )
    mem.freeze_contract(contract)
    return mem


# ─── canonical_test_file injected into prompt ─────────────────────────────────

@pytest.mark.asyncio
async def test_giant_brain_audit_embeds_canonical_in_prompt(tmp_path):
    """When canonical_test_file is provided, it appears in the system prompt."""
    captured_prompts = {}

    async def _capture(**kwargs):
        captured_prompts["system"] = kwargs.get("system", "")
        return MINIMAL_MANIFEST

    pool = _make_pool(side_effect=_capture)

    await AgentPool._giant_brain_audit(
        pool,
        export_dir=tmp_path,
        test_output="FAILED",
        canonical_test_file="habit_tracker_test.py",
    )

    assert "habit_tracker_test.py" in captured_prompts["system"], (
        "Canonical test file name must appear in Giant Brain system prompt"
    )
    assert "stale" in captured_prompts["system"].lower(), (
        "System prompt must mention stale file concept"
    )


@pytest.mark.asyncio
async def test_giant_brain_audit_no_canonical_no_filename_in_prompt(tmp_path):
    """Without canonical_test_file, no specific filename is injected into system prompt."""
    captured_prompts = {}

    async def _capture(**kwargs):
        captured_prompts["system"] = kwargs.get("system", "")
        return MINIMAL_MANIFEST

    pool = _make_pool(side_effect=_capture)

    await AgentPool._giant_brain_audit(
        pool,
        export_dir=tmp_path,
        test_output="FAILED",
    )

    # The specific filename clause ("The one true test file") should NOT appear
    assert "The one true test file" not in captured_prompts["system"], (
        "No canonical filename clause should be present when canonical_test_file=None"
    )


# ─── delete_file action in _run_fixer_pass ────────────────────────────────────

@pytest.mark.asyncio
async def test_fixer_pass_delete_file_removes_target(tmp_path):
    """_run_fixer_pass with action=delete_file removes the file without AI call."""
    stale = tmp_path / "tests" / "old_test.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("# stale", encoding="utf-8")

    manifest = {
        "summary": "stale test file",
        "escalate": False,
        "fixes": [
            {
                "file": "tests/old_test.py",
                "complexity": "mechanical",
                "description": "Remove stale test file from prior pipeline run",
                "action": "delete_file",
            }
        ],
    }

    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.ai.complete = AsyncMock()  # should NOT be called
    pool._collect_source_tree = MagicMock(return_value={})

    result = await AgentPool._run_fixer_pass(pool, manifest, tmp_path)

    assert not stale.exists(), "Stale file must be deleted"
    pool.ai.complete.assert_not_called()
    assert "tests/old_test.py" in result


@pytest.mark.asyncio
async def test_fixer_pass_delete_file_absent_is_noop(tmp_path):
    """_run_fixer_pass with action=delete_file on missing file doesn't raise."""
    manifest = {
        "summary": "stale test file",
        "escalate": False,
        "fixes": [
            {
                "file": "tests/nonexistent.py",
                "complexity": "mechanical",
                "description": "Remove stale file",
                "action": "delete_file",
            }
        ],
    }

    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.ai.complete = AsyncMock()
    pool._collect_source_tree = MagicMock(return_value={})

    # Should not raise
    result = await AgentPool._run_fixer_pass(pool, manifest, tmp_path)
    pool.ai.complete.assert_not_called()


# ─── Stale purge in _run_giant_brain_repair ───────────────────────────────────

def _make_repair_pool():
    """Minimal pool mock sufficient for _run_giant_brain_repair stale-purge path."""
    pool = MagicMock(spec=AgentPool)
    pool.ai = MagicMock()
    pool.ai.get_role_chain = MagicMock(return_value=["claude"])
    pool.ai.complete = AsyncMock(return_value=MINIMAL_MANIFEST)
    pool._collect_source_tree = MagicMock(return_value={})
    pool._collect_test_output = MagicMock(return_value="FAILED")
    pool._naming_triage = AsyncMock(return_value=[])
    pool._broadcast = AsyncMock()
    pool._run_fixer_pass = AsyncMock(return_value=[])
    pool.settings = None

    # Fake SwarmSummary
    summary = MagicMock()
    summary.failed = 1
    summary.passed = 0
    summary.install_hard_fail = False
    pool._rerun_tests = AsyncMock(return_value=MagicMock(failed=0, passed=1))

    return pool, summary


@pytest.mark.asyncio
async def test_stale_test_purge_removes_non_canonical(tmp_path):
    """_run_giant_brain_repair deletes stale test files before passes."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    canonical = tests_dir / "habit_tracker_test.py"
    canonical.write_text("# canonical", encoding="utf-8")
    stale = tests_dir / "python_cli_habit_tracker_test.py"
    stale.write_text("# stale", encoding="utf-8")

    mem = _make_memory("habit_tracker_test.py")
    pool, summary = _make_repair_pool()

    await AgentPool._run_giant_brain_repair(
        pool,
        swarm_summary=summary,
        export_dir=tmp_path,
        project_context={"project_name": "test"},
        session_id="s1",
        memory=mem,
    )

    assert not stale.exists(), "Stale test file must be purged"
    assert canonical.exists(), "Canonical test file must be preserved"


@pytest.mark.asyncio
async def test_stale_purge_preserves_non_test_files(tmp_path):
    """_run_giant_brain_repair only deletes test files, not other files."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    canonical = tests_dir / "habit_tracker_test.py"
    canonical.write_text("# canonical", encoding="utf-8")
    conftest = tests_dir / "conftest.py"  # not a test file by name
    conftest.write_text("# conftest", encoding="utf-8")

    mem = _make_memory("habit_tracker_test.py")
    pool, summary = _make_repair_pool()

    await AgentPool._run_giant_brain_repair(
        pool,
        swarm_summary=summary,
        export_dir=tmp_path,
        project_context={"project_name": "test"},
        session_id="s1",
        memory=mem,
    )

    assert conftest.exists(), "conftest.py must not be purged"
    assert canonical.exists(), "Canonical test file must be preserved"


@pytest.mark.asyncio
async def test_stale_purge_skipped_when_no_memory(tmp_path):
    """_run_giant_brain_repair with memory=None does not crash or purge."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    some_test = tests_dir / "some_test.py"
    some_test.write_text("# test", encoding="utf-8")

    pool, summary = _make_repair_pool()

    await AgentPool._run_giant_brain_repair(
        pool,
        swarm_summary=summary,
        export_dir=tmp_path,
        project_context={"project_name": "test"},
        session_id="s1",
        memory=None,
    )

    assert some_test.exists(), "No memory → no purge, test file untouched"
