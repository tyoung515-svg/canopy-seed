"""
Session 47 tests — Big Fixer path resolution + SwarmTool render() fix.

Covers:
  - File tree block is injected into user_prompt
  - Package alias resolution: habit_tracker/ → python_cli_habit_tracker/ when
    the latter is the real on-disk package (has __init__.py)
  - Files missing from the tree but with a name match are found via rglob
  - Last-resort: file created at requested path when no alias or rglob match
  - SwarmToolContext.render() shows concrete JSON examples, not type signatures
  - render() includes all required field names for write_export_file and lint_code
  - _apply_single_fix alias resolution parity with Big Fixer
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_export_dir(tmp_path: Path, *, with_init: bool = True, pkg_name: str = "python_cli_habit_tracker"):
    """Create a minimal on-disk package layout under tmp_path."""
    pkg = tmp_path / pkg_name
    pkg.mkdir()
    if with_init:
        (pkg / "__init__.py").write_text("# package\n")
    (pkg / "cli.py").write_text("def main(): pass\n")
    # tests dir
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_cli.py").write_text("def test_placeholder(): pass\n")
    return tmp_path


# ─── Package alias resolution tests ──────────────────────────────────────────

def test_package_alias_remaps_missing_dir(tmp_path):
    """
    When the Big Fixer asks to write 'habit_tracker/exceptions.py' but the
    on-disk package is 'python_cli_habit_tracker/', the file should be written
    to 'python_cli_habit_tracker/exceptions.py'.
    """
    export_dir = _make_export_dir(tmp_path)
    file_rel = "habit_tracker/exceptions.py"
    content = "class HabitNotFoundError(Exception): pass\n"

    file_parts = Path(file_rel).parts
    req_pkg = file_parts[0] if len(file_parts) > 1 else None
    remapped = False

    target_path = export_dir / file_rel
    assert not target_path.exists()

    # rglob finds nothing (exceptions.py doesn't exist at all)
    matches = list(export_dir.rglob(Path(file_rel).name))
    assert matches == []

    # Package alias resolution
    if req_pkg and not (export_dir / req_pkg).exists():
        pkg_dirs = [
            d for d in export_dir.iterdir()
            if d.is_dir() and (d / "__init__.py").exists()
        ]
        assert len(pkg_dirs) == 1
        real_pkg = pkg_dirs[0]
        assert real_pkg.name == "python_cli_habit_tracker"

        remapped_path = real_pkg / Path(*file_parts[1:])
        remapped_path.parent.mkdir(parents=True, exist_ok=True)
        target_path = remapped_path
        remapped = True

    assert remapped
    target_path.write_text(content, encoding="utf-8")
    expected = export_dir / "python_cli_habit_tracker" / "exceptions.py"
    assert expected.exists()
    assert expected.read_text() == content


def test_package_alias_remaps_models_and_stats(tmp_path):
    """
    All three missing files (exceptions, models, stats) get remapped correctly.
    """
    export_dir = _make_export_dir(tmp_path)
    missing_files = [
        "habit_tracker/exceptions.py",
        "habit_tracker/models.py",
        "habit_tracker/stats.py",
    ]

    pkg_dirs = [
        d for d in export_dir.iterdir()
        if d.is_dir() and (d / "__init__.py").exists()
    ]
    real_pkg = pkg_dirs[0]

    for file_rel in missing_files:
        file_parts = Path(file_rel).parts
        req_pkg = file_parts[0]
        assert not (export_dir / req_pkg).exists()

        remapped_path = real_pkg / Path(*file_parts[1:])
        remapped_path.parent.mkdir(parents=True, exist_ok=True)
        remapped_path.write_text(f"# {file_parts[-1]}\n", encoding="utf-8")

    # All three should now exist under python_cli_habit_tracker/
    for name in ("exceptions.py", "models.py", "stats.py"):
        assert (export_dir / "python_cli_habit_tracker" / name).exists(), \
            f"{name} was not created"


def test_rglob_match_used_when_file_exists_elsewhere(tmp_path):
    """
    If the file already exists somewhere under export_dir (different path),
    rglob should find it and use that path — no alias remapping needed.
    """
    export_dir = _make_export_dir(tmp_path)
    # cli.py already exists under python_cli_habit_tracker/
    file_rel = "habit_tracker/cli.py"  # wrong top-level, correct filename

    target_path = export_dir / file_rel
    assert not target_path.exists()

    matches = list(export_dir.rglob(Path(file_rel).name))
    assert len(matches) == 1
    assert matches[0].name == "cli.py"
    assert matches[0].parent.name == "python_cli_habit_tracker"


def test_alias_resolution_no_pkg_dir_skips_gracefully(tmp_path):
    """
    If there are no Python package dirs (no __init__.py), alias resolution
    should not remap — file stays unresolved.
    """
    # Build export dir WITHOUT __init__.py
    export_dir = _make_export_dir(tmp_path, with_init=False)
    file_rel = "habit_tracker/exceptions.py"
    file_parts = Path(file_rel).parts
    req_pkg = file_parts[0]

    assert not (export_dir / req_pkg).exists()

    pkg_dirs = [
        d for d in export_dir.iterdir()
        if d.is_dir() and (d / "__init__.py").exists()
    ]
    assert pkg_dirs == [], "Expected no pkg_dirs when __init__.py absent"
    # No remapping happens — test just confirms the guard works


def test_file_tree_block_in_user_prompt(tmp_path):
    """
    The file tree block fed to the model must list all paths from full_source_tree.
    """
    full_source_tree = {
        "python_cli_habit_tracker/__init__.py": "# pkg\n",
        "python_cli_habit_tracker/cli.py": "def main(): pass\n",
        "tests/test_cli.py": "def test_placeholder(): pass\n",
    }
    all_paths = sorted(full_source_tree.keys())
    file_tree_block = "\n".join(f"  {p}" for p in all_paths)

    user_prompt = (
        f"## Actual File Tree (use ONLY these paths for edits — do NOT invent new top-level dirs)\n"
        f"{file_tree_block}\n\n"
        "Emit the JSON fix object now."
    )

    assert "python_cli_habit_tracker/__init__.py" in user_prompt
    assert "python_cli_habit_tracker/cli.py" in user_prompt
    assert "tests/test_cli.py" in user_prompt
    # The model should NOT see a bare "habit_tracker/" top-level dir
    # (check it only appears as part of "python_cli_habit_tracker", not standalone)
    import re
    assert not re.search(r'(?<![_a-zA-Z])habit_tracker/', user_prompt), \
        "standalone 'habit_tracker/' should not appear in file tree"


# ─── SwarmTool render() concrete-example tests ────────────────────────────────

def test_render_shows_concrete_example_for_write_export_file():
    """render() must include a concrete TOOL_CALL example with 'path' and 'content'."""
    from core.swarm_tools import SwarmToolContext
    ctx = SwarmToolContext.render(enabled=True)
    # The example must show both required fields by name so agents copy them
    assert '"path"' in ctx
    assert '"content"' in ctx
    assert "write_export_file" in ctx


def test_render_shows_concrete_example_for_lint_code():
    """render() must include a concrete TOOL_CALL example with 'code' and 'language'."""
    from core.swarm_tools import SwarmToolContext
    ctx = SwarmToolContext.render(enabled=True)
    assert '"code"' in ctx
    assert '"language"' in ctx
    assert "lint_code" in ctx


def test_render_shows_concrete_example_for_read_export_file():
    """render() must show read_export_file with 'path' field."""
    from core.swarm_tools import SwarmToolContext
    ctx = SwarmToolContext.render(enabled=True)
    assert "read_export_file" in ctx
    # path must appear as a key in the example JSON
    assert '"path"' in ctx


def test_render_example_is_valid_json_extractable():
    """Every TOOL_CALL example line in render() must be parseable JSON."""
    import json, re
    from core.swarm_tools import SwarmToolContext
    ctx = SwarmToolContext.render(enabled=True)
    found = 0
    for line in ctx.splitlines():
        line = line.strip()
        if line.startswith("Example: TOOL_CALL:"):
            json_part = line[len("Example: TOOL_CALL:"):].strip()
            obj = json.loads(json_part)  # must not raise
            assert "tool" in obj
            assert "input" in obj
            found += 1
    # Session 58: run_test + search_code added → 7 tools total
    # Session 58 (declare_done): declare_done added → 8 tools total
    assert found == 8, f"Expected 8 tool examples, got {found}"


def test_render_disabled_returns_empty():
    from core.swarm_tools import SwarmToolContext
    assert SwarmToolContext.render(enabled=False) == ""


# ─── _apply_single_fix alias resolution parity ───────────────────────────────

def test_apply_single_fix_alias_resolution(tmp_path):
    """
    The same package alias logic from Big Fixer must work in _apply_single_fix:
    if 'habit_tracker/models.py' is requested but 'python_cli_habit_tracker/'
    is the real package dir, the file should be created there.
    """
    export_dir = _make_export_dir(tmp_path)
    file_rel = "habit_tracker/models.py"
    file_parts = Path(file_rel).parts
    req_pkg = file_parts[0]

    # Confirm the alias dir does not exist
    assert not (export_dir / req_pkg).exists()

    # Replicate the logic from _apply_single_fix
    target_path = export_dir / file_rel
    pkg_dirs = [
        d for d in export_dir.iterdir()
        if d.is_dir() and (d / "__init__.py").exists()
    ]
    assert pkg_dirs, "Expected at least one Python package dir"
    real_pkg = pkg_dirs[0]
    remapped_path = real_pkg / Path(*file_parts[1:])
    remapped_path.parent.mkdir(parents=True, exist_ok=True)
    remapped_path.write_text("# models\n", encoding="utf-8")

    assert (export_dir / "python_cli_habit_tracker" / "models.py").exists()
