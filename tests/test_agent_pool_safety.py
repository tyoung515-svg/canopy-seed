import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.agent_pool import AgentConfig, AgentPool, AgentType, ContractArtifact, RepoMap, SwarmMemory, TaskRecord, TaskSpec
from core.ai_backend import AIBackend
from core.orchestrator import OrchestratorSubTask, ProjectOrchestrator
from core.tester_swarm import SwarmSummary, TestResult


async def _collect_events(event_list, event_type, data):
    event_list.append((event_type, data))


def _make_contract_artifact(
    name: str = "test_unit_converter.py",
    content: str = "def test_contract():\n    assert True\n",
    summary: str = "UnitConverter.convert(value: float, from_unit: str, to_unit: str) -> float; raises ValueError",
) -> ContractArtifact:
    return ContractArtifact(
        test_file_name=name,
        test_file_content=content,
        public_api_summary=summary,
    )


def test_swarm_memory_seed_stores_rules():
    memory = SwarmMemory()

    rules = {"naming": {"package": "calc_core"}, "constraints": ["use snake_case"]}
    memory.seed(rules)

    assert memory.rules == rules


def test_contract_artifact_fields():
    artifact = ContractArtifact(
        test_file_name="test_unit_converter.py",
        test_file_content="def test_x():\n    assert True\n",
        public_api_summary="Converter.convert(value: float) -> float; raises ValueError",
    )

    assert artifact.test_file_name == "test_unit_converter.py"
    assert "def test_x" in artifact.test_file_content
    assert "Converter.convert" in artifact.public_api_summary


def test_contract_artifact_critic_notes_default_empty():
    artifact = ContractArtifact(
        test_file_name="test_unit_converter.py",
        test_file_content="def test_x():\n    assert True\n",
        public_api_summary="Converter.convert(value: float) -> float; raises ValueError",
    )

    assert artifact.critic_notes == ""


def test_swarm_memory_frozen_initially():
    memory = SwarmMemory()

    assert memory.is_contract_frozen() is False
    assert memory.get_contract() is None


def test_swarm_memory_freeze_contract():
    memory = SwarmMemory()
    artifact = _make_contract_artifact()

    memory.freeze_contract(artifact)

    assert memory.is_contract_frozen() is True
    assert memory.get_contract() == artifact


def test_swarm_memory_get_block_for_subtask_returns_contract():
    memory = SwarmMemory()
    artifact = _make_contract_artifact()

    memory.freeze_contract(artifact)

    assert memory.get_block_for_subtask("default") == artifact
    assert memory.get_block_for_subtask("auth") == artifact


def test_swarm_memory_publish_guard_on_frozen_keys(caplog):
    memory = SwarmMemory()
    memory.publish("test_file", {"exports": ["a"], "file_path": "a.py", "tier": 1})
    baseline = memory.read("test_file")
    memory.freeze_contract(_make_contract_artifact())

    caplog.set_level("WARNING")
    memory.publish("test_file", {"exports": ["b"], "file_path": "b.py", "tier": 2})

    assert memory.read("test_file") == baseline
    assert "frozen contract key" in caplog.text


def test_swarm_memory_publish_allowed_on_non_frozen_keys():
    memory = SwarmMemory()
    memory.freeze_contract(_make_contract_artifact())

    memory.publish("helper_module", {"exports": ["helper"], "file_path": "helper.py", "tier": 1})

    assert memory.read("helper_module")["exports"] == ["helper"]


def test_swarm_memory_publish_and_read_round_trip():
    memory = SwarmMemory()

    manifest = {"exports": ["convert", "batch_convert"], "file_path": "converter.py", "tier": 1}
    memory.publish("converter", manifest)

    assert memory.read("converter") == manifest


def test_swarm_memory_context_for_tier_two_only_includes_tier_one():
    memory = SwarmMemory()
    memory.publish("units", {"exports": ["METERS"], "file_path": "units.py", "tier": 1})
    memory.publish("converter", {"exports": ["convert"], "file_path": "converter.py", "tier": 2})
    memory.publish("runner", {"exports": ["run"], "file_path": "runner.py", "tier": 3})

    context = memory.context_for_tier(2)

    assert "units (tier 1)" in context
    assert "converter (tier 2)" not in context
    assert "runner (tier 3)" not in context


def test_swarm_memory_context_for_tier_three_includes_tiers_one_and_two():
    memory = SwarmMemory()
    memory.publish("units", {"exports": ["METERS"], "file_path": "units.py", "tier": 1})
    memory.publish("converter", {"exports": ["convert"], "file_path": "converter.py", "tier": 2})
    memory.publish("runner", {"exports": ["run"], "file_path": "runner.py", "tier": 3})

    context = memory.context_for_tier(3)

    assert "units (tier 1)" in context
    assert "converter (tier 2)" in context
    assert "runner (tier 3)" not in context


def test_swarm_memory_context_for_tier_one_is_empty():
    memory = SwarmMemory()
    memory.publish("units", {"exports": ["METERS"], "file_path": "units.py", "tier": 1})

    assert memory.context_for_tier(1) == ""


def test_swarm_memory_context_for_tier_empty_registry_is_empty():
    memory = SwarmMemory()

    assert memory.context_for_tier(2) == ""


def test_swarm_memory_context_is_readable_and_contains_exports():
    memory = SwarmMemory()
    memory.publish(
        "units",
        {
            "exports": ["METERS", "FEET", "convert_length"],
            "file_path": "units.py",
            "tier": 1,
        },
    )

    context = memory.context_for_tier(2)

    assert context.startswith("## Available modules from previous tiers")
    assert "- units (tier 1): exports [METERS, FEET, convert_length] at units.py" in context


def test_swarm_memory_wrapper_adds_instruction_header_when_context_present():
    pool = AgentPool()
    raw_context = "## Available modules from previous tiers\n- units (tier 1): exports [METERS] at units.py"

    wrapped = pool._wrap_swarm_memory_context(raw_context)

    assert "## Modules already written by earlier agents" in wrapped


def test_swarm_memory_wrapper_includes_do_not_redefine_instruction():
    pool = AgentPool()
    raw_context = "## Available modules from previous tiers\n- units (tier 1): exports [METERS] at units.py"

    wrapped = pool._wrap_swarm_memory_context(raw_context)

    assert "Do not redefine or reimplement them" in wrapped


def test_swarm_memory_wrapper_empty_context_returns_empty_string():
    pool = AgentPool()

    wrapped = pool._wrap_swarm_memory_context("")

    assert wrapped == ""


def test_swarm_memory_wrapper_places_your_task_divider_before_description():
    pool = AgentPool()
    raw_context = "## Available modules from previous tiers\n- units (tier 1): exports [METERS] at units.py"
    description = "Implement converter module"

    assembled = pool._compose_subtask_description_with_memory_context(description, raw_context)

    assert "## Your task\n\nImplement converter module" in assembled


def test_estimate_contract_token_budget_typescript_project():
    pool = AgentPool()
    seed = (
        "Build a TypeScript utility package with modules: auth, billing, telemetry, parser, storage "
        "and with tests."
    )

    budget = pool._estimate_contract_token_budget(seed)

    assert budget >= 16384


def test_estimate_contract_token_budget_simple_python_utility():
    pool = AgentPool()
    seed = "Create a simple basic minimal single file Python helper utility."

    budget = pool._estimate_contract_token_budget(seed)

    assert budget <= 4096


def test_estimate_contract_token_budget_large_platform():
    pool = AgentPool()
    seed = (
        "Comprehensive TypeScript REST API platform with authentication, database, and full test suite. "
        "Modules: auth, billing, telemetry, scheduler, ingestion."
    )

    budget = pool._estimate_contract_token_budget(seed)

    assert budget >= 24576


def test_phase0_codex_prompt_injects_target_language(mock_ai_backend):
    """Phase 0 contract prompt must inject target language regardless of which
    backend from ROLE_CONTRACT_CHAIN is used (ADR-039)."""
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {
        "name": "Language Inject",
        "description": "Build a TypeScript utility with parser and formatter modules",
    }

    contract_json = json.dumps({
        "test_file_name": "test_language_inject.spec.ts",
        "test_file_content": "def test_contract():\n    assert True\n",
        "public_api_summary": "Parser.parse(text: str) -> dict",
    })

    # Session 58: critic uses get_role_chain("auditor")[0] → "gemini-customtools"
    _critic_backends = {"gemini-customtools"}

    async def fake_complete(**kwargs):
        backend = kwargs.get("backend")
        if backend in _critic_backends:
            return "APPROVED"
        # Any contract_generator backend: verify prompt content
        assert "Target language: TypeScript" in kwargs.get("system", "")
        assert "Vitest" in kwargs.get("message", "")
        assert "pytest" not in kwargs.get("message", "").lower()
        return contract_json

    mock_ai_backend.complete = AsyncMock(side_effect=fake_complete)

    artifact = asyncio.run(pool._generate_contract(project_context))

    assert artifact.test_file_name == "test_language_inject.spec.ts"
    assert artifact.public_api_summary.startswith("Parser.parse")


def test_phase0_contract_user_prompt_typescript_uses_spec_ts_extension(mock_ai_backend):
    """Phase 0 TypeScript contract must request .spec.ts extension regardless of backend."""
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {
        "name": "Battery Cell Lib",
        "description": "Build a TypeScript library with cell registry and discharge simulator",
    }

    contract_json = json.dumps({
        "test_file_name": "test_battery_cell_lib.spec.ts",
        "test_file_content": "import { describe, it, expect } from 'vitest'\n",
        "public_api_summary": "CellRegistry.addCell(id: string) -> void",
    })

    # Session 58: critic uses get_role_chain("auditor")[0] → "gemini-customtools"
    _critic_backends = {"gemini-customtools"}

    async def fake_complete(**kwargs):
        backend = kwargs.get("backend")
        if backend in _critic_backends:
            return "APPROVED"
        prompt = kwargs.get("message", "")
        assert ".spec.ts" in prompt
        assert ".py" not in prompt
        return contract_json

    mock_ai_backend.complete = AsyncMock(side_effect=fake_complete)
    artifact = asyncio.run(pool._generate_contract(project_context))

    assert artifact.test_file_name.endswith(".spec.ts")


def test_phase0_contract_user_prompt_python_unchanged(mock_ai_backend):
    """Phase 0 Python contract must request pytest and .py extension regardless of backend."""
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {
        "name": "Simple Utility",
        "description": "Build a simple Python helper for unit conversion",
    }

    contract_json = json.dumps({
        "test_file_name": "simple_utility_test.py",
        "test_file_content": "def test_contract():\n    assert True\n",
        "public_api_summary": "convert(value: float, from_unit: str, to_unit: str) -> float",
    })

    # Session 58: critic uses get_role_chain("auditor")[0] → "gemini-customtools"
    _critic_backends = {"gemini-customtools"}

    async def fake_complete(**kwargs):
        backend = kwargs.get("backend")
        if backend in _critic_backends:
            return "APPROVED"
        prompt = kwargs.get("message", "")
        assert "pytest" in prompt.lower()
        assert ".py" in prompt
        return contract_json

    mock_ai_backend.complete = AsyncMock(side_effect=fake_complete)
    artifact = asyncio.run(pool._generate_contract(project_context))

    assert artifact.test_file_name.endswith(".py")


def test_repo_map_extracts_typescript_exports(tmp_path):
    src = tmp_path / "src" / "types.ts"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "\n".join([
            "export interface BatteryCell { id: string }",
            "export type CellId = string",
            "export const CELL_COUNT = 0",
        ]),
        encoding="utf-8",
    )

    repo_map = RepoMap.build(str(tmp_path), "typescript", max_tokens=2000)

    assert "# Repo Map — files already written" in repo_map
    assert "## src/types.ts" in repo_map
    assert "export interface BatteryCell" in repo_map
    assert "export type CellId" in repo_map


def test_repo_map_extracts_python_signatures(tmp_path):
    src = tmp_path / "module.py"
    src.write_text(
        "\n".join([
            "class BatteryCell:",
            "    def __init__(self):",
            "        pass",
            "",
            "def build_report(cell_id: str):",
            "    return {}",
        ]),
        encoding="utf-8",
    )

    repo_map = RepoMap.build(str(tmp_path), "python", max_tokens=2000)

    assert "## module.py" in repo_map
    assert "class BatteryCell:" in repo_map
    assert "def build_report(cell_id: str):" in repo_map


def test_repo_map_skips_node_modules_and_test_files(tmp_path):
    node_mod = tmp_path / "node_modules" / "pkg" / "index.ts"
    node_mod.parent.mkdir(parents=True, exist_ok=True)
    node_mod.write_text("export const SHOULD_SKIP = true", encoding="utf-8")

    test_file = tmp_path / "tests" / "registry.spec.ts"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("export const SHOULD_SKIP_TEST = true", encoding="utf-8")

    kept = tmp_path / "src" / "registry.ts"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text("export function getCellById(id: string) {}", encoding="utf-8")

    repo_map = RepoMap.build(str(tmp_path), "typescript", max_tokens=2000)

    assert "src/registry.ts" in repo_map
    assert "SHOULD_SKIP" not in repo_map
    assert "registry.spec.ts" not in repo_map


def test_repo_map_empty_dir_returns_empty_string(tmp_path):
    repo_map = RepoMap.build(str(tmp_path), "python", max_tokens=2000)
    assert repo_map == ""


def test_subtask_description_includes_contract_when_frozen():
    pool = AgentPool()
    memory = SwarmMemory()
    artifact = _make_contract_artifact(summary="Converter.convert(value: float) -> float; raises ValueError")
    memory.freeze_contract(artifact)

    assembled = pool._compose_subtask_description(
        description="Implement converter module",
        raw_context="",
        memory=memory,
    )

    assert "## Frozen Test Contract (READ ONLY — do not modify)" in assembled
    assert artifact.public_api_summary in assembled
    assert f"tests/{artifact.test_file_name}" in assembled


def test_subtask_description_no_contract_when_not_frozen():
    pool = AgentPool()
    memory = SwarmMemory()

    assembled = pool._compose_subtask_description(
        description="Implement converter module",
        raw_context="",
        memory=memory,
    )

    assert "Frozen Test Contract" not in assembled


def _make_subtask(target_files):
    return OrchestratorSubTask(
        title="Safety Test",
        description="Validate execution safety",
        target_files=target_files,
        domain_layer="backend",
        ambiguity_score=0,
        dependency_score=0,
        notes="",
    )


def test_path_traversal_blocked(mock_ai_backend, tmp_path):
    events = []
    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=lambda t, d: _collect_events(events, t, d))
    mock_ai_backend.response = "print('blocked')"

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    subtask = _make_subtask(["../../core/evil.py"])

    asyncio.run(pool._execute_subtask(subtask, str(export_dir), asyncio.Semaphore(1)))

    blocked_target = tmp_path / "core" / "evil.py"
    assert not blocked_target.exists()
    assert any(name == "subtask_warning" for name, _ in events)


def test_legitimate_file_written(mock_ai_backend, tmp_path):
    events = []
    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=lambda t, d: _collect_events(events, t, d))
    mock_ai_backend.response = "print('ok')"

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    subtask = _make_subtask(["backend/api.py"])

    asyncio.run(pool._execute_subtask(subtask, str(export_dir), asyncio.Semaphore(1)))

    created_file = export_dir / "backend" / "api.py"
    assert created_file.exists()


def test_execute_subtask_injects_depends_on_file_content(mock_ai_backend, tmp_path):
    pool = AgentPool(ai_backend=mock_ai_backend)
    mock_ai_backend.response = "print('ok')"

    export_dir = tmp_path / "export"
    dep_file = export_dir / "backend" / "models.py"
    dep_file.parent.mkdir(parents=True, exist_ok=True)
    dep_file.write_text("class User:\n    pass\n", encoding="utf-8")

    subtask = OrchestratorSubTask(
        title="Wire API",
        description="Implement backend api module",
        target_files=["backend/api.py"],
        domain_layer="backend",
        ambiguity_score=0,
        dependency_score=0,
        depends_on=["backend/models.py"],
        notes="",
    )

    asyncio.run(pool._execute_subtask(subtask, str(export_dir), asyncio.Semaphore(1)))

    assert mock_ai_backend.calls
    prompt_message = mock_ai_backend.calls[0]["message"]
    assert "## Required Dependencies Already Implemented" in prompt_message
    assert "### backend/models.py" in prompt_message
    assert "class User:" in prompt_message


def test_snapshot_created_for_existing_file(mock_ai_backend, tmp_path):
    events = []
    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=lambda t, d: _collect_events(events, t, d))
    mock_ai_backend.response = "print('updated')"

    export_dir = tmp_path / "export"
    target = export_dir / "backend" / "api.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('old')\n", encoding="utf-8")

    subtask = _make_subtask(["backend/api.py"])

    with patch("core.snapshot.SnapshotManager") as mock_snapshot_cls:
        snapshot_instance = mock_snapshot_cls.return_value
        snapshot_instance.create_snapshot = AsyncMock(return_value="snap-1")

        asyncio.run(pool._execute_subtask(subtask, str(export_dir), asyncio.Semaphore(1)))

        assert snapshot_instance.create_snapshot.await_count == 1


def test_snapshot_skipped_for_new_file(mock_ai_backend, tmp_path):
    events = []
    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=lambda t, d: _collect_events(events, t, d))
    mock_ai_backend.response = "print('new')"

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    subtask = _make_subtask(["backend/new_file.py"])

    with patch("core.snapshot.SnapshotManager") as mock_snapshot_cls:
        snapshot_instance = mock_snapshot_cls.return_value
        snapshot_instance.create_snapshot = AsyncMock(return_value="snap-2")

        asyncio.run(pool._execute_subtask(subtask, str(export_dir), asyncio.Semaphore(1)))

        snapshot_instance.create_snapshot.assert_not_called()


def test_run_task_uses_name_key_for_orchestrated_build(mock_ai_backend):
    events = []
    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=lambda t, d: _collect_events(events, t, d))

    record = TaskRecord(task_id="task-1", agent_id="agent-1", description="Build it")
    agent = AgentConfig(
        id="agent-1",
        name="Test Agent",
        agent_type=AgentType.CLAUDE,
        endpoint="",
        model_id="claude-sonnet-4-6",
        capabilities=["code"],
    )
    task = TaskSpec(context={"name": "Battery Cell Tool", "description": "Build system"})

    with patch.object(pool, "run_orchestrated_build", new_callable=AsyncMock) as mocked_orchestrated:
        asyncio.run(pool._run_task(record, agent, task))

        mocked_orchestrated.assert_awaited_once()


def test_dep_injection_ignores_typescript_path_aliases(mock_ai_backend, tmp_path):
    pool = AgentPool(ai_backend=mock_ai_backend)

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    package_json = export_dir / "package.json"
    package_json.write_text(
        '{"name":"x","version":"1.0.0","dependencies":{},"devDependencies":{}}',
        encoding="utf-8",
    )

    source_file = export_dir / "src" / "app.ts"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "\n".join([
            "import Button from '@/components/Button'",
            "import api from '@/utils/api'",
            "import type { Foo } from '@/types'",
            "import { Box } from '@mui/material'",
        ]),
        encoding="utf-8",
    )

    asyncio.run(pool._inject_missing_dependencies(export_dir))

    data = json.loads(package_json.read_text(encoding="utf-8"))
    deps = data.get("dependencies", {})

    assert "@/components" not in deps
    assert "@/utils" not in deps
    assert "@/types" not in deps
    assert "@mui/material" in deps


def test_backend_tier_routing_aliases_and_fallback(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)

    assert pool._select_backend_for_tier("lite") == "openai-codex"
    assert pool._select_backend_for_tier("low") == "openai-codex"
    assert pool._select_backend_for_tier("standard") == "openai-codex"
    assert pool._select_backend_for_tier("HIGH") == "openai-codex"
    assert pool._select_backend_for_tier("escalate") == "claude"
    assert pool._select_backend_for_tier("unknown-tier") == "claude"


def test_dep_injection_skips_invalid_package_json(mock_ai_backend, tmp_path):
    pool = AgentPool(ai_backend=mock_ai_backend)

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    package_json = export_dir / "package.json"
    package_json.write_text("{invalid json", encoding="utf-8")

    source_file = export_dir / "index.ts"
    source_file.write_text("import React from 'react'", encoding="utf-8")

    asyncio.run(pool._inject_missing_dependencies(export_dir))

    assert package_json.read_text(encoding="utf-8") == "{invalid json"


def test_dep_injection_normalizes_dependency_sections(mock_ai_backend, tmp_path):
    pool = AgentPool(ai_backend=mock_ai_backend)

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    package_json = export_dir / "package.json"
    package_json.write_text(
        json.dumps({
            "name": "x",
            "version": "1.0.0",
            "dependencies": [],
            "devDependencies": "oops",
        }),
        encoding="utf-8",
    )

    source_file = export_dir / "src" / "main.ts"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "\n".join([
            "import React from 'react'",
            "import { render } from 'react-dom'",
            "import fs from 'node:fs'",
        ]),
        encoding="utf-8",
    )

    asyncio.run(pool._inject_missing_dependencies(export_dir))

    data = json.loads(package_json.read_text(encoding="utf-8"))
    assert isinstance(data["dependencies"], dict)
    assert isinstance(data["devDependencies"], dict)
    assert data["dependencies"]["react"] == "*"
    assert data["dependencies"]["react-dom"] == "*"
    assert "fs" not in data["dependencies"]


def test_dedup_jsx_when_tsx_exists_removes_only_duplicates(mock_ai_backend, tmp_path):
    pool = AgentPool(ai_backend=mock_ai_backend)

    export_dir = tmp_path / "export"
    keep_only_jsx = export_dir / "ui" / "Button.jsx"
    dedup_jsx = export_dir / "ui" / "Card.jsx"
    dedup_tsx = export_dir / "ui" / "Card.tsx"

    keep_only_jsx.parent.mkdir(parents=True, exist_ok=True)
    keep_only_jsx.write_text("export default function Button() {}", encoding="utf-8")
    dedup_jsx.write_text("export default function Card() {}", encoding="utf-8")
    dedup_tsx.write_text("export default function Card(): JSX.Element { return <div /> }", encoding="utf-8")

    removed = pool._dedup_jsx_when_tsx_exists(export_dir)

    assert str(dedup_jsx) in removed
    assert not dedup_jsx.exists()
    assert dedup_tsx.exists()
    assert keep_only_jsx.exists()


def test_run_orchestrated_build_reports_dedup_count(mock_ai_backend):
    events = []
    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=lambda t, d: _collect_events(events, t, d))

    subtask = OrchestratorSubTask(
        title="Task",
        description="Desc",
        target_files=["backend/api.py"],
        domain_layer="backend",
        ambiguity_score=0,
        dependency_score=0,
        notes="",
    )

    project_context = {"name": "Dedup Project", "description": "test"}
    task_spec = TaskSpec(context=project_context)

    with patch.object(pool, "_generate_contract", new_callable=AsyncMock, return_value=_make_contract_artifact()), \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch.object(pool, "_execute_subtask", new_callable=AsyncMock, return_value={"status": "done"}), \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls, \
         patch.object(pool, "_dedup_jsx_when_tsx_exists", return_value=["a.jsx", "b.jsx"]) as dedup_mock:

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[subtask])

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[]))

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(return_value=type("FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"})())

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

        dedup_mock.assert_called_once()

    build_complete = [payload for name, payload in events if name == "build_complete"]
    assert build_complete
    assert build_complete[-1]["dedup_removed"] == 2


def test_generate_contract_called_before_swarm(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {"name": "Phase Zero Order", "description": "test"}
    task_spec = TaskSpec(context=project_context)
    call_order = []
    critic_calls = 0
    critic_calls = 0

    async def fake_generate(_ctx):
        call_order.append("contract")
        return _make_contract_artifact()

    async def fake_decompose(_ctx):
        call_order.append("orchestrator")
        return []

    with patch.object(pool, "_generate_contract", new_callable=AsyncMock, side_effect=fake_generate), \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(side_effect=fake_decompose)

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[]))

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(return_value=type("FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"})())

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

    assert call_order[:2] == ["contract", "orchestrator"]


def test_contract_frozen_only_after_critic_approval(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {"name": "Critic Approval Project", "description": "test"}
    task_spec = TaskSpec(context=project_context)

    contract_json_1 = json.dumps({
        "test_file_name": "test_contract_a.py",
        "test_file_content": "def test_contract_a():\n    assert True\n",
        "public_api_summary": "A.foo() -> str; raises ValueError",
    })
    contract_json_2 = json.dumps({
        "test_file_name": "test_contract_b.py",
        "test_file_content": "def test_contract_b():\n    assert True\n",
        "public_api_summary": "B.bar() -> int; raises TypeError",
    })

    call_order = []
    # Session 58: critic uses get_role_chain("auditor")[0] → "gemini-customtools"
    _critic_backends = {"gemini-customtools"}

    async def fake_complete(**kwargs):
        backend = kwargs.get("backend")
        if backend in _critic_backends:
            if "critic1" not in call_order:
                call_order.append("critic1")
                return "1. API summary says int but test asserts string"
            call_order.append("critic2")
            return "APPROVED"
        # Any contract_generator backend
        if "gen1" not in call_order:
            call_order.append("gen1")
            return contract_json_1
        call_order.append("gen2")
        return contract_json_2

    mock_ai_backend.complete = AsyncMock(side_effect=fake_complete)

    with patch("core.agent_pool.SwarmMemory") as memory_cls, \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        memory = memory_cls.return_value
        memory.seed.return_value = None
        memory.context_for_tier.return_value = ""
        memory.is_contract_frozen.return_value = True

        def freeze_side_effect(_contract):
            call_order.append("freeze")

        memory.freeze_contract.side_effect = freeze_side_effect

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[])

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[]))

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(return_value=type("FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"})())

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

    assert "critic2" in call_order
    assert "freeze" in call_order
    assert call_order.index("freeze") > call_order.index("critic2")


def test_contract_force_approved_at_revision_cap(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {"name": "Critic Cap Project", "description": "test"}
    task_spec = TaskSpec(context=project_context)

    contract_json_1 = json.dumps({
        "test_file_name": "test_contract_a.py",
        "test_file_content": "def test_contract_a():\n    assert True\n",
        "public_api_summary": "A.foo() -> str; raises ValueError",
    })
    contract_json_2 = json.dumps({
        "test_file_name": "test_contract_b.py",
        "test_file_content": "def test_contract_b():\n    assert True\n",
        "public_api_summary": "B.bar() -> int; raises TypeError",
    })

    mock_ai_backend.complete = AsyncMock(side_effect=[
        contract_json_1,
        "1. Missing edge-case coverage",
        contract_json_2,
        "1. Completeness issue remains",
        contract_json_2,
        "1. Still not complete",
    ])

    with patch("core.agent_pool.SwarmMemory") as memory_cls, \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        memory = memory_cls.return_value
        memory.seed.return_value = None
        memory.context_for_tier.return_value = ""
        memory.is_contract_frozen.return_value = True

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[])

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[]))

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(return_value=type("FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"})())

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

        assert memory.freeze_contract.call_count == 1
        frozen_contract = memory.freeze_contract.call_args.args[0]
        assert frozen_contract.critic_notes != ""
        assert "Force-approved after 2 revisions" in frozen_contract.critic_notes


def test_contract_frozen_despite_critic_api_failure(mock_ai_backend):
    """Critic API 503 / RuntimeError degrades gracefully: contract is still frozen,
    critic_notes carries the service error message, and run_orchestrated_build returns
    a valid SwarmPlan without crashing."""
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {"name": "Resilience Project", "description": "test"}
    task_spec = TaskSpec(context=project_context)

    contract_json = json.dumps({
        "test_file_name": "test_resilience.py",
        "test_file_content": "def test_placeholder():\n    assert True\n",
        "public_api_summary": "Resilience.run() -> None; raises RuntimeError",
    })

    # Generator succeeds; Critic raises a service error (mimics 503 → RuntimeError)
    mock_ai_backend.complete = AsyncMock(side_effect=[
        contract_json,
        RuntimeError("503 Service Unavailable"),
    ])

    with patch("core.agent_pool.SwarmMemory") as memory_cls, \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        memory = memory_cls.return_value
        memory.seed.return_value = None
        memory.context_for_tier.return_value = ""
        memory.is_contract_frozen.return_value = True

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[])

        tester = tester_cls.return_value
        tester.run = AsyncMock(
            return_value=SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[])
        )

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(
            return_value=type(
                "FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"}
            )()
        )

        # Must NOT raise — critic outage degrades gracefully
        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

        # Contract was frozen exactly once despite the critic failure
        assert memory.freeze_contract.call_count == 1
        frozen_contract = memory.freeze_contract.call_args.args[0]

        # critic_notes carries the exact spec-compliant message
        assert "Critic unavailable — service error:" in frozen_contract.critic_notes
        assert "Proceeding with unreviewed draft." in frozen_contract.critic_notes

        # The contract payload is still valid and usable
        assert frozen_contract.test_file_name == "test_resilience.py"
        assert frozen_contract.public_api_summary != ""


def test_contract_revision1_parse_failure_reverts_to_revision0(mock_ai_backend):
    """If Revision 1 (critic-informed draft) fails to parse or returns missing fields,
    the pipeline reverts to the Revision 0 contract, populates critic_notes with the
    revert reason + original critic feedback, and does NOT crash."""
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {"name": "Revert Project", "description": "test"}
    task_spec = TaskSpec(context=project_context)

    revision0_json = json.dumps({
        "test_file_name": "test_revert.py",
        "test_file_content": "def test_placeholder():\n    assert True\n",
        "public_api_summary": "Revert.run() -> None; raises ValueError",
    })
    critic_feedback = "1. Missing edge-case tests\n2. Return type not asserted"
    # Revision 1 generator returns malformed JSON (simulates Flash wrapping in fences)
    revision1_bad = "```json\n{broken json"

    mock_ai_backend.complete = AsyncMock(side_effect=[
        revision0_json,          # Generator revision 0 — succeeds
        critic_feedback,         # Critic — returns feedback, not APPROVED
        revision1_bad,           # Generator revision 1 — bad JSON → RuntimeError
    ])

    with patch("core.agent_pool.SwarmMemory") as memory_cls, \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        memory = memory_cls.return_value
        memory.seed.return_value = None
        memory.context_for_tier.return_value = ""
        memory.is_contract_frozen.return_value = True

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[])

        tester = tester_cls.return_value
        tester.run = AsyncMock(
            return_value=SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[])
        )

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(
            return_value=type(
                "FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"}
            )()
        )

        # Must NOT raise — revision 1 parse failure reverts to revision 0
        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

        # Contract frozen exactly once, using the revision 0 draft
        assert memory.freeze_contract.call_count == 1
        frozen_contract = memory.freeze_contract.call_args.args[0]

        # Frozen contract is the revision 0 payload, not an empty/broken one
        assert frozen_contract.test_file_name == "test_revert.py"
        assert frozen_contract.public_api_summary != ""

        # critic_notes records the revert reason and preserves the critic's original feedback
        assert "Revision parse failed" in frozen_contract.critic_notes
        assert "Revision 0 draft" in frozen_contract.critic_notes
        assert "Missing edge-case tests" in frozen_contract.critic_notes


def test_contract_revision0_failure_reraises(mock_ai_backend):
    """If Revision 0 fails (no valid contract exists yet), RuntimeError propagates —
    there is nothing to salvage and the build must fail explicitly."""
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {"name": "Fail Project", "description": "test"}
    task_spec = TaskSpec(context=project_context)

    # Revision 0 generator returns completely broken output
    mock_ai_backend.complete = AsyncMock(return_value="not json at all")

    with patch("core.agent_pool.SwarmMemory") as memory_cls, \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        memory = memory_cls.return_value
        memory.seed.return_value = None
        memory.context_for_tier.return_value = ""
        memory.is_contract_frozen.return_value = True

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[])

        tester = tester_cls.return_value
        tester.run = AsyncMock(
            return_value=SwarmSummary(total=0, passed=0, failed=0, duration=0.0, results=[])
        )

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(
            return_value=type(
                "FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"}
            )()
        )

        # Revision 0 failure — no prior contract to revert to — must raise
        with pytest.raises(RuntimeError):
            asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

        # Nothing should be frozen
        memory.freeze_contract.assert_not_called()


def test_contract_test_file_written_to_export_dir(mock_ai_backend):
    events = []

    async def collect_events(event_type, data):
        events.append((event_type, data))

    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=collect_events)
    project_context = {"name": "Contract File Write", "description": "test"}
    task_spec = TaskSpec(context=project_context)
    artifact = _make_contract_artifact(
        name="test_contract_generated.py",
        content="def test_generated_contract():\n    assert 2 + 2 == 4\n",
        summary="Generated API summary",
    )

    with patch.object(pool, "_generate_contract", new_callable=AsyncMock, return_value=artifact), \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[])

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[]))

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(return_value=type("FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"})())

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

    build_started = [payload for name, payload in events if name == "build_started"]
    assert build_started
    export_dir = Path(build_started[-1]["export_dir"])

    contract_file = export_dir / "tests" / artifact.test_file_name
    assert contract_file.exists()
    assert contract_file.read_text(encoding="utf-8") == artifact.test_file_content


def test_orchestrator_prompt_excludes_test_generation(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {"name": "Prompt Contract", "description": "test"}
    task_spec = TaskSpec(context=project_context)
    captured_prompt = {"value": ""}

    async def fake_decompose(context):
        captured_prompt["value"] = ProjectOrchestrator(mock_ai_backend)._build_decompose_prompt(context)
        return []

    with patch.object(pool, "_generate_contract", new_callable=AsyncMock, return_value=_make_contract_artifact()), \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(side_effect=fake_decompose)

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[]))

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(return_value=type("FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"})())

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

    assert "Do NOT include any subtask that writes or modifies test files" in captured_prompt["value"]


def test_phase1_dispatch_filters_test_file_subtasks(mock_ai_backend, caplog):
    pool = AgentPool(ai_backend=mock_ai_backend)
    project_context = {"name": "Dispatch Filter Project", "description": "test"}
    task_spec = TaskSpec(context=project_context)

    implementation_subtask = _make_subtask(["unit_converter/converter.py"])
    test_subtask = _make_subtask(["tests/test_foo.py"])
    test_subtask.title = "Write tests"

    caplog.set_level("WARNING")

    with patch.object(pool, "_generate_contract", new_callable=AsyncMock, return_value=_make_contract_artifact()), \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch.object(pool, "_execute_subtask", new_callable=AsyncMock, return_value={"status": "done", "files": ["unit_converter/converter.py"]}) as exec_mock, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[test_subtask, implementation_subtask])

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[]))

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(return_value=type("FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"})())

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

    assert exec_mock.await_count == 1
    assert "Phase 1 dispatch: skipping test-file subtask 'Write tests' — contract is frozen" in caplog.text


def test_collect_source_tree_returns_relative_paths(tmp_path):
    """Test _collect_source_tree returns {relative_path: content} for all files."""
    from core.agent_pool import AgentPool
    
    pool = AgentPool()
    
    # Create a temp dir with 2 files
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    
    file1 = export_dir / "file1.py"
    file1.write_text("print('hello')", encoding="utf-8")
    
    subdir = export_dir / "subdir"
    subdir.mkdir()
    file2 = subdir / "file2.txt"
    file2.write_text("world", encoding="utf-8")
    
    # Call _collect_source_tree
    result = pool._collect_source_tree(export_dir)
    
    # Assert both relative paths appear as keys and content matches
    assert "file1.py" in result
    assert result["file1.py"] == "print('hello')"
    
    assert "subdir/file2.txt" in result
    assert result["subdir/file2.txt"] == "world"


def test_collect_source_tree_skips_node_modules(tmp_path):
    """Test _collect_source_tree skips node_modules directories."""
    from core.agent_pool import AgentPool
    
    pool = AgentPool()
    
    # Create a temp dir with a node_modules directory
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    
    # Create a regular file
    regular = export_dir / "app.js"
    regular.write_text("console.log('app')", encoding="utf-8")
    
    # Create node_modules with a file inside
    nm_dir = export_dir / "node_modules"
    nm_dir.mkdir()
    nm_file = nm_dir / "foo.js"
    nm_file.write_text("console.log('foo')", encoding="utf-8")
    
    # Call _collect_source_tree
    result = pool._collect_source_tree(export_dir)
    
    # Assert regular file is included
    assert "app.js" in result
    
    # Assert node_modules file is NOT included
    assert "node_modules/foo.js" not in result


def test_naming_triage_not_called_from_repair_loop(mock_ai_backend, tmp_path):
    """Session 51: _run_repair_loop must not call _naming_triage (ADR-016 removed)."""
    pool = AgentPool(ai_backend=mock_ai_backend)

    class MockSwarmSummary:
        failed = 1
        results = []

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(pool, "_naming_triage", new_callable=AsyncMock, return_value=[]) as triage_mock, \
         patch("core.orchestrator.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls:

        orchestrator_cls.return_value.repair_audit = AsyncMock(return_value=[])
        orchestrator_cls.return_value.score_subtask.return_value = None
        tester_cls.return_value.run = AsyncMock(
            return_value=type("Summary", (), {"failed": 0, "passed": 1, "results": []})()
        )

        asyncio.run(
            pool._run_repair_loop(
                swarm_summary=MockSwarmSummary(),
                export_dir=export_dir,
                project_context={"project_name": "x", "description": "y"},
                session_id="session-test",
            )
        )

    triage_mock.assert_not_called()


def test_claude_haiku_backend_dispatches_correctly():
    class MockSettings:
        DEFAULT_AI_BACKEND = "lmstudio"
        CLAUDE_ESCALATION_ENABLED = False
        ANTHROPIC_API_KEY = ""
        GOOGLE_API_KEY = ""
        GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
        LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
        LMSTUDIO_MODEL = "local-model"
        WORKER_BASE_URL = "http://localhost:8080/v1"
        WORKER_MODEL = "worker-model"
        CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

    backend = AIBackend(MockSettings())

    with patch.object(backend, "_claude_complete", new_callable=AsyncMock, return_value="ok") as claude_mock:
        result = asyncio.run(
            backend.complete(
                system="s",
                message="m",
                backend="claude-haiku",
                history=[],
                max_tokens=777,
            )
        )

    assert result == "ok"
    claude_mock.assert_awaited_once()
    call_kwargs = claude_mock.await_args.kwargs
    assert call_kwargs.get("model") == "claude-haiku-4-5-20251001"


def test_collect_source_tree_skips_common_cache_dirs(tmp_path):
    """Test _collect_source_tree skips cache/venv directories."""
    pool = AgentPool()

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    keep_file = export_dir / "src" / "main.py"
    keep_file.parent.mkdir(parents=True, exist_ok=True)
    keep_file.write_text("print('ok')", encoding="utf-8")

    pycache_file = export_dir / "__pycache__" / "x.pyc"
    pycache_file.parent.mkdir(parents=True, exist_ok=True)
    pycache_file.write_text("binary", encoding="utf-8")

    venv_file = export_dir / ".venv" / "lib" / "site.py"
    venv_file.parent.mkdir(parents=True, exist_ok=True)
    venv_file.write_text("print('venv')", encoding="utf-8")

    result = pool._collect_source_tree(export_dir)

    assert "src/main.py" in result
    assert "__pycache__/x.pyc" not in result
    assert ".venv/lib/site.py" not in result


def test_run_repair_loop_emits_round_events_and_stops_when_clean(mock_ai_backend, tmp_path):
    events = []

    async def collect_events(event_type, data):
        events.append((event_type, data))

    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=collect_events)
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    subtask = _make_subtask(["backend/fix.py"])
    initial_summary = SwarmSummary(total=2, passed=0, failed=2, duration=0.1, results=[])
    repaired_summary = SwarmSummary(total=2, passed=2, failed=0, duration=0.2, results=[])

    with patch("core.orchestrator.ProjectOrchestrator") as orchestrator_cls, \
         patch.object(pool, "_execute_subtask", new_callable=AsyncMock, return_value={"status": "done", "files": ["backend/fix.py"]}) as exec_mock, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls:

        orchestrator = orchestrator_cls.return_value
        orchestrator.repair_audit = AsyncMock(return_value=[subtask])
        orchestrator.score_subtask.return_value = None

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=repaired_summary)

        final_summary, repair_history = asyncio.run(
            pool._run_repair_loop(
                swarm_summary=initial_summary,
                export_dir=export_dir,
                project_context={"name": "Repair Loop Project"},
                session_id="session-1",
            )
        )

    assert final_summary.failed == 0
    assert len(repair_history) == 1
    assert repair_history[0]["subtask_count"] == 1
    assert exec_mock.await_count == 1
    assert tester.run.await_count == 1

    started = [payload for name, payload in events if name == "repair_round_started"]
    completed = [payload for name, payload in events if name == "repair_round_complete"]
    assert len(started) == 1
    assert len(completed) == 1
    assert completed[0]["tests_failed"] == 0


def test_run_repair_loop_uses_high_token_override_for_subtasks(mock_ai_backend, tmp_path):
    pool = AgentPool(ai_backend=mock_ai_backend)
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    subtask = _make_subtask(["backend/fix.py"])
    initial_summary = SwarmSummary(total=1, passed=0, failed=1, duration=0.1, results=[])
    repaired_summary = SwarmSummary(total=1, passed=1, failed=0, duration=0.2, results=[])

    with patch("core.orchestrator.ProjectOrchestrator") as orchestrator_cls, \
         patch.object(pool, "_execute_subtask", new_callable=AsyncMock, return_value={"status": "done", "files": ["backend/fix.py"]}) as exec_mock, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls:

        orchestrator = orchestrator_cls.return_value
        orchestrator.repair_audit = AsyncMock(return_value=[subtask])
        orchestrator.score_subtask.return_value = None

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=repaired_summary)

        asyncio.run(
            pool._run_repair_loop(
                swarm_summary=initial_summary,
                export_dir=export_dir,
                project_context={"name": "Repair Loop Project"},
                session_id="session-override",
            )
        )

    assert exec_mock.await_count == 1
    assert exec_mock.await_args.kwargs["max_tokens"] == 32768


def test_run_repair_loop_skips_naming_triage_goes_to_rounds(mock_ai_backend, tmp_path):
    """Session 51: _run_repair_loop must skip naming triage and go straight to
    repair rounds. _naming_triage must never be awaited."""
    events = []

    async def collect_events(event_type, data):
        events.append((event_type, data))

    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=collect_events)
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    initial_summary = SwarmSummary(
        total=1, passed=0, failed=1, duration=0.1,
        results=[
            TestResult(
                source_file="",
                test_file="tests/test_api.py",
                passed=False,
                failure_output="ModuleNotFoundError: No module named 'app'",
                failure_summary="",
                duration_seconds=0.01,
            )
        ],
    )
    clean_summary = SwarmSummary(total=1, passed=1, failed=0, duration=0.2, results=[])

    with patch("core.orchestrator.ProjectOrchestrator") as orchestrator_cls, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch.object(pool, "_naming_triage", new_callable=AsyncMock, return_value=[]) as triage_mock:

        orchestrator = orchestrator_cls.return_value
        orchestrator.repair_audit = AsyncMock(return_value=[])
        orchestrator.score_subtask.return_value = None

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=clean_summary)

        final_summary, repair_history = asyncio.run(
            pool._run_repair_loop(
                swarm_summary=initial_summary,
                export_dir=export_dir,
                project_context={"name": "Repair Loop Project"},
                session_id="session-round0",
            )
        )

    triage_mock.assert_not_called()
    # repair_history should NOT contain a round-0 naming_triage entry
    triage_rounds = [r for r in repair_history if r.get("phase") == "naming_triage"]
    assert triage_rounds == [], f"Unexpected triage rounds in history: {triage_rounds}"
    # No repair_round_started event should be fired for naming_triage phase
    triage_events = [payload for name, payload in events if payload.get("phase") == "naming_triage"]
    assert triage_events == [], f"Unexpected triage SSE events: {triage_events}"


def test_classify_error_gear_infra(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)

    gear = pool._classify_error_gear(RuntimeError("Gemini thinking-only response"))

    assert gear == "openai-codex"


def test_classify_error_gear_infra_404_not_found(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)

    gear = pool._classify_error_gear(RuntimeError("HTTP 404 Not Found for model endpoint"))

    assert gear == "openai-codex"


def test_classify_error_gear_parse(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)

    gear = pool._classify_error_gear(RuntimeError("output truncated while parsing response"))

    assert gear == "gemini"


def test_classify_error_gear_logic(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)

    gear = pool._classify_error_gear(RuntimeError("SyntaxError: invalid syntax"))

    assert gear == "claude"


def test_classify_error_gear_unknown(mock_ai_backend):
    pool = AgentPool(ai_backend=mock_ai_backend)

    gear = pool._classify_error_gear(RuntimeError("something unrecognised"))

    assert gear is None


def test_execute_subtask_gear_shifts_on_flash_failure(mock_ai_backend, tmp_path):
    pool = AgentPool(ai_backend=mock_ai_backend)

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    subtask = _make_subtask(["backend/api.py"])
    mock_ai_backend.complete = AsyncMock(side_effect=[RuntimeError("thinking-only response"), "print('ok')"])

    result = asyncio.run(pool._execute_subtask(subtask, str(export_dir), asyncio.Semaphore(1)))

    assert result["status"] == "done"
    assert mock_ai_backend.complete.await_count == 2
    assert mock_ai_backend.complete.await_args_list[0].kwargs["backend"] == "openai-codex"
    assert mock_ai_backend.complete.await_args_list[1].kwargs["backend"] == "openai-codex"
    assert (export_dir / "backend" / "api.py").exists()


def test_execute_subtask_gear_shifts_to_haiku_after_flash_minimal_failure(mock_ai_backend, tmp_path):
    pool = AgentPool(ai_backend=mock_ai_backend)

    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    subtask = _make_subtask(["backend/api.py"])
    mock_ai_backend.complete = AsyncMock(side_effect=[
        RuntimeError("thinking-only response"),
        RuntimeError("openai temporary outage"),
        "print('ok')",
    ])

    result = asyncio.run(pool._execute_subtask(subtask, str(export_dir), asyncio.Semaphore(1)))

    assert result["status"] == "done"
    assert mock_ai_backend.complete.await_count == 3
    assert mock_ai_backend.complete.await_args_list[0].kwargs["backend"] == "openai-codex"
    assert mock_ai_backend.complete.await_args_list[1].kwargs["backend"] == "openai-codex"
    assert mock_ai_backend.complete.await_args_list[2].kwargs["backend"] == "claude-haiku"
    assert (export_dir / "backend" / "api.py").exists()


def test_gemini_flash_backend_uses_2_5_flash_model():
    class MockSettings:
        DEFAULT_AI_BACKEND = "lmstudio"
        CLAUDE_ESCALATION_ENABLED = False
        ANTHROPIC_API_KEY = ""
        GOOGLE_API_KEY = ""
        OPENAI_API_KEY = ""
        GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
        LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
        LMSTUDIO_MODEL = "local-model"
        WORKER_BASE_URL = "http://localhost:8080/v1"
        WORKER_MODEL = "worker-model"
        CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

    backend = AIBackend(MockSettings())

    with patch.object(backend, "_gemini_complete", new_callable=AsyncMock, return_value="ok") as gemini_mock:
        result = asyncio.run(
            backend.complete(
                system="s",
                message="m",
                backend="gemini-flash",
                history=[],
            )
        )

    assert result == "ok"
    gemini_mock.assert_awaited_once()
    assert gemini_mock.await_args.kwargs["model"] == "gemini-3-flash-preview"


def test_gemini_flash_lite_backend_uses_flash_lite_model_setting():
    # ADR-035: gemini-flash-lite now reads GEMINI_FLASH_LITE_MODEL setting
    # (default: gemini-3.1-flash-lite-preview), not hardcoded gemini-3-flash-preview
    class MockSettings:
        DEFAULT_AI_BACKEND = "lmstudio"
        CLAUDE_ESCALATION_ENABLED = False
        ANTHROPIC_API_KEY = ""
        GOOGLE_API_KEY = ""
        OPENAI_API_KEY = ""
        GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
        GEMINI_FLASH_LITE_MODEL = "gemini-3.1-flash-lite-preview"  # ADR-035
        LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
        LMSTUDIO_MODEL = "local-model"
        WORKER_BASE_URL = "http://localhost:8080/v1"
        WORKER_MODEL = "worker-model"
        CLAUDE_MODEL = "claude-sonnet-4-6"

    backend = AIBackend(MockSettings())

    with patch.object(backend, "_gemini_complete", new_callable=AsyncMock, return_value="ok") as gemini_mock:
        result = asyncio.run(
            backend.complete(
                system="s",
                message="m",
                backend="gemini-flash-lite",
                history=[],
            )
        )

    assert result == "ok"
    gemini_mock.assert_awaited_once()
    # ADR-035: now uses GEMINI_FLASH_LITE_MODEL (gemini-3.1-flash-lite-preview)
    assert gemini_mock.await_args.kwargs["model"] == "gemini-3.1-flash-lite-preview"


def test_flash_backend_payload_sends_minimal_thinking_config(monkeypatch):
    # Session 58: gemini-flash now uses thinking_level="MINIMAL" explicitly.
    # The payload must include thinkingConfig with thinkingLevel="MINIMAL" (not absent).
    class MockSettings:
        DEFAULT_AI_BACKEND = "lmstudio"
        CLAUDE_ESCALATION_ENABLED = False
        ANTHROPIC_API_KEY = ""
        GOOGLE_API_KEY = "test-google-key"
        OPENAI_API_KEY = ""
        GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview-customtools"
        GEMINI_FLASH_MODEL = "gemini-3-flash-preview"
        LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
        LMSTUDIO_MODEL = "local-model"
        WORKER_BASE_URL = "http://localhost:8080/v1"
        WORKER_MODEL = "worker-model"
        CLAUDE_MODEL = "claude-sonnet-4-6"

    backend = AIBackend(MockSettings())
    captured = {"payload": None}

    class FakeStreamResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}'

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, json=None):
            captured["payload"] = json
            return FakeStreamResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: FakeAsyncClient())

    result = asyncio.run(
        backend.complete(
            system="s",
            message="m",
            backend="gemini-flash",
            history=[],
        )
    )

    assert result == "ok"
    generation = captured["payload"].get("generationConfig", {})
    # Session 58: gemini-flash uses thinking_level="MINIMAL" explicitly — thinkingConfig must be present
    assert "thinkingConfig" in generation
    assert generation["thinkingConfig"].get("thinkingLevel") == "MINIMAL"


def test_openai_nano_backend_reachable_via_http_fallback(monkeypatch):
    class MockSettings:
        DEFAULT_AI_BACKEND = "lmstudio"
        CLAUDE_ESCALATION_ENABLED = False
        ANTHROPIC_API_KEY = ""
        GOOGLE_API_KEY = ""
        OPENAI_API_KEY = "test-openai-key"
        OPENAI_MODEL = "gpt-5-nano"
        GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
        LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
        LMSTUDIO_MODEL = "local-model"
        WORKER_BASE_URL = "http://localhost:8080/v1"
        WORKER_MODEL = "worker-model"
        CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

    backend = AIBackend(MockSettings())

    import builtins
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "openai":
            raise ImportError("openai not installed")
        return real_import(name, globals, locals, fromlist, level)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "nano-ok"}}]}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            assert url == "https://api.openai.com/v1/chat/completions"
            assert headers and headers.get("Authorization") == "Bearer test-openai-key"
            assert json and json.get("model") == "gpt-5-nano"
            return FakeResponse()

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: FakeAsyncClient())

    result = asyncio.run(
        backend.complete(
            system="s",
            message="m",
            backend="openai-nano",
            history=[],
        )
    )

    assert result == "nano-ok"


def test_claude_fallback_to_lmstudio_preserves_max_tokens():
    class MockSettings:
        DEFAULT_AI_BACKEND = "lmstudio"
        CLAUDE_ESCALATION_ENABLED = False
        ANTHROPIC_API_KEY = "test-anthropic-key"
        GOOGLE_API_KEY = ""
        OPENAI_API_KEY = ""
        OPENAI_MODEL = "gpt-5-nano"
        GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
        LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
        LMSTUDIO_MODEL = "local-model"
        WORKER_BASE_URL = "http://localhost:8080/v1"
        WORKER_MODEL = "worker-model"
        CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

    backend = AIBackend(MockSettings())
    backend._claude_complete = AsyncMock(side_effect=RuntimeError("claude temporary outage"))
    backend._lmstudio_complete = AsyncMock(return_value="lmstudio-ok")

    result = asyncio.run(
        backend.complete(
            system="s",
            message="m",
            backend="claude",
            history=[],
            max_tokens=16384,
        )
    )

    assert "responded via LM Studio instead" in result
    backend._lmstudio_complete.assert_awaited_once()
    call_kwargs = backend._lmstudio_complete.await_args.kwargs
    assert call_kwargs.get("max_tokens") == 16384


def test_claude_haiku_backend_uses_streaming_mode():
    class MockSettings:
        DEFAULT_AI_BACKEND = "lmstudio"
        CLAUDE_ESCALATION_ENABLED = False
        ANTHROPIC_API_KEY = "test-key"
        GOOGLE_API_KEY = ""
        OPENAI_API_KEY = ""
        GOOGLE_GEMINI_MODEL = "gemini-3.1-pro-preview"
        LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
        LMSTUDIO_MODEL = "local-model"
        WORKER_BASE_URL = "http://localhost:8080/v1"
        WORKER_MODEL = "worker-model"
        CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

    backend = AIBackend(MockSettings())

    class FakeStreamContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get_final_text(self):
            return "streamed-haiku"

    class FakeMessages:
        def __init__(self):
            self.stream_called = False
            self.stream_kwargs = None

        def stream(self, **kwargs):
            self.stream_called = True
            self.stream_kwargs = kwargs
            return FakeStreamContext()

    class FakeClaudeClient:
        def __init__(self):
            self.messages = FakeMessages()

    fake_client = FakeClaudeClient()

    with patch.object(backend, "_get_claude_client", return_value=fake_client):
        result = asyncio.run(
            backend.complete(
                system="s",
                message="m",
                backend="claude-haiku",
                history=[],
                max_tokens=16384,
            )
        )

    assert result == "streamed-haiku"
    assert fake_client.messages.stream_called is True
    assert fake_client.messages.stream_kwargs["model"] == "claude-haiku-4-5-20251001"


def test_run_orchestrated_build_invokes_repair_loop_after_phase1_failures(mock_ai_backend):
    events = []

    async def collect_events(event_type, data):
        events.append((event_type, data))

    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=collect_events)

    subtask = _make_subtask(["backend/api.py"])
    project_context = {"name": "Repair Fallback Project", "description": "test"}
    task_spec = TaskSpec(context=project_context)

    initial_fail_summary = SwarmSummary(total=2, passed=1, failed=1, duration=0.1, results=[])
    repaired_summary = SwarmSummary(total=2, passed=2, failed=0, duration=0.2, results=[])

    with patch.object(pool, "_generate_contract", new_callable=AsyncMock, return_value=_make_contract_artifact()), \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch.object(pool, "_execute_subtask", new_callable=AsyncMock, return_value={"status": "done"}), \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch.object(pool, "_run_giant_brain_repair", new_callable=AsyncMock, return_value=(repaired_summary, [{"round": 1}])) as repair_mock, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[subtask])

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=initial_fail_summary)

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(return_value=type("FixSummaryLike", (), {"fixed": 0, "failed": 0, "giant_brain_review": "ok"})())

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

        repair_mock.assert_awaited_once()

    build_complete = [payload for name, payload in events if name == "build_complete"]
    assert len(build_complete) == 1
    assert build_complete[0]["tests_failed"] == 0


def test_run_orchestrated_build_emits_user_guided_debug_state_on_persistent_failure(mock_ai_backend):
    """
    Test that run_orchestrated_build emits user_guided_debug_state when
    repair loop exhausts all 3 rounds and tests still fail.
    """
    events = []
    
    async def collect_events(event_type, data):
        events.append((event_type, data))
    
    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=collect_events)
    
    # Create a dummy subtask
    subtask = OrchestratorSubTask(
        title="Task",
        description="Desc",
        target_files=["file.py"],
        domain_layer="backend",
        ambiguity_score=0,
        dependency_score=0,
        notes="",
    )
    
    project_context = {"name": "Repair Test", "description": "test"}
    task_spec = TaskSpec(context=project_context)
    
    # Always fail swarm — use total>1 so collection_error stays False and the
    # ADR-030 router takes the pass_rate < LOW_THRESHOLD dump path (0% < 70%).
    failing_summary = SwarmSummary(
        total=10, passed=0, failed=10, duration=0.1, results=[]
    )
    with patch.object(pool, "_generate_contract", new_callable=AsyncMock, return_value=_make_contract_artifact()), \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch.object(pool, "_run_giant_brain_repair", new_callable=AsyncMock, return_value=(failing_summary, [{"round": 1}])) as repair_mock, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[subtask])

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=failing_summary)

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(return_value=type("FixSummaryLike", (), {
            "fixed": 0,
            "failed": 0,
            "giant_brain_review": "ok"
        })())

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))
        repair_mock.assert_awaited_once()

    # Assert user_guided_debug_state was emitted
    user_debug_events = [payload for name, payload in events if name == "user_guided_debug_state"]
    assert len(user_debug_events) == 1, f"Expected 1 user_guided_debug_state, got {len(user_debug_events)}"

    debug_payload = user_debug_events[0]
    assert "repair_history" in debug_payload, "repair_history must be in user_guided_debug_state"
    assert debug_payload["tests_still_failing"] == 10
    assert debug_payload["repair_rounds"] > 0
    
    # Assert build_complete was NOT emitted (because we returned early)
    build_complete_events = [payload for name, payload in events if name == "build_complete"]
    assert len(build_complete_events) == 0, f"build_complete should NOT be emitted after persistent repair failure, but got {len(build_complete_events)}"
    
    # Initial swarm run happens before giant brain repair is invoked.
    assert tester.run.await_count >= 1


def test_execute_subtask_respects_max_tokens_override():
    """Test that _execute_subtask respects max_tokens override in ai.complete call."""
    from unittest.mock import MagicMock
    from core.agent_pool import AgentPool

    # Create mock AI backend
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value="print('test')")

    pool = AgentPool(ai_backend=mock_ai)

    # Create a dummy subtask with one target file
    subtask = _make_subtask(["backend/test.py"])

    # Call _execute_subtask with max_tokens=16384
    asyncio.run(pool._execute_subtask(subtask, "/tmp/export", asyncio.Semaphore(1), max_tokens=16384))

    # Assert ai.complete was called with max_tokens=16384
    assert mock_ai.complete.called
    call_kwargs = mock_ai.complete.call_args.kwargs
    assert call_kwargs["max_tokens"] == 16384, f"Expected max_tokens=16384, got {call_kwargs['max_tokens']}"


# ──────────────────────────────────────────────────────────────────────────────
# ADR-023 Giant Brain tests
# ──────────────────────────────────────────────────────────────────────────────


def _make_failing_swarm_summary(file="tests/test_foo.py", test_name="test_bar", output="AssertionError"):
    """Build a minimal SwarmSummary with one failing test result."""
    result = type("LegacyFailResult", (), {
        "file": file,
        "test_name": test_name,
        "passed": False,
        "output": output,
        "duration": 0.1,
    })()
    return SwarmSummary(total=1, passed=0, failed=1, duration=0.1, results=[result])


# ── _collect_test_output ──────────────────────────────────────────────────────

def test_collect_test_output_formats_failing_results():
    """_collect_test_output must include file, test name, and output for each failure."""
    from unittest.mock import MagicMock
    pool = AgentPool(ai_backend=MagicMock())
    summary = _make_failing_swarm_summary(
        file="tests/test_converter.py",
        test_name="test_convert_units",
        output="NameError: name 'convert_units' is not defined",
    )
    out = pool._collect_test_output(summary)
    assert "tests/test_converter.py" in out
    assert "test_convert_units" in out
    assert "NameError" in out


def test_collect_test_output_no_failing_results_returns_str_of_summary():
    """_collect_test_output must not crash on an all-passing summary."""
    from unittest.mock import MagicMock
    pool = AgentPool(ai_backend=MagicMock())
    summary = SwarmSummary(total=2, passed=2, failed=0, duration=0.5, results=[])
    out = pool._collect_test_output(summary)
    assert isinstance(out, str)


# ── _giant_brain_audit ────────────────────────────────────────────────────────

def test_giant_brain_audit_parses_valid_json_manifest(tmp_path):
    """_giant_brain_audit must parse a clean JSON manifest from the LLM response."""
    from unittest.mock import MagicMock
    manifest_json = json.dumps({
        "summary": "Function renamed in source but not in tests",
        "escalate": False,
        "fixes": [
            {
                "file": "converter.py",
                "complexity": "mechanical",
                "description": "Rename 'convert' to 'convert_units'",
                "action": "rename_function",
            }
        ],
    })
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value=manifest_json)
    mock_ai.get_role_chain = MagicMock(return_value=["gemini-flash"])
    pool = AgentPool(ai_backend=mock_ai)
    # Write a stub source file so _collect_source_tree has something to read
    (tmp_path / "converter.py").write_text("def convert(): pass")
    result = asyncio.run(pool._giant_brain_audit(tmp_path, "AssertionError: convert_units not found"))
    assert result is not None
    assert result["escalate"] is False
    assert len(result["fixes"]) == 1
    assert result["fixes"][0]["complexity"] == "mechanical"


def test_giant_brain_audit_escalation_flag_returned(tmp_path):
    """_giant_brain_audit must return escalate=True when Giant Brain flags unrealistic tests."""
    from unittest.mock import MagicMock
    manifest_json = json.dumps({
        "summary": "Tests expect behaviour that violates the spec",
        "escalate": True,
        "fixes": [],
    })
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value=manifest_json)
    mock_ai.get_role_chain = MagicMock(return_value=["gemini-flash"])
    pool = AgentPool(ai_backend=mock_ai)
    result = asyncio.run(pool._giant_brain_audit(tmp_path, "some test output"))
    assert result is not None
    assert result["escalate"] is True
    assert result["fixes"] == []


def test_giant_brain_audit_strips_code_fences_from_response(tmp_path):
    """_giant_brain_audit must tolerate markdown code fences wrapping the JSON."""
    from unittest.mock import MagicMock
    manifest_json = json.dumps({
        "summary": "Missing export",
        "escalate": False,
        "fixes": [],
    })
    fenced_response = f"```json\n{manifest_json}\n```"
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value=fenced_response)
    mock_ai.get_role_chain = MagicMock(return_value=["gemini-flash"])
    pool = AgentPool(ai_backend=mock_ai)
    result = asyncio.run(pool._giant_brain_audit(tmp_path, "test output"))
    assert result is not None
    assert result["escalate"] is False


def test_giant_brain_audit_returns_none_on_llm_failure(tmp_path):
    """_giant_brain_audit must return None when the LLM call raises."""
    from unittest.mock import MagicMock
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(side_effect=RuntimeError("Claude unavailable"))
    pool = AgentPool(ai_backend=mock_ai)
    result = asyncio.run(pool._giant_brain_audit(tmp_path, "test output"))
    assert result is None


def test_giant_brain_audit_assigns_default_complexity_when_missing(tmp_path):
    """Fixes missing 'complexity' must default to 'mechanical'."""
    from unittest.mock import MagicMock
    manifest_json = json.dumps({
        "summary": "Missing import",
        "escalate": False,
        "fixes": [{"file": "foo.py", "description": "add import", "action": "add_import"}],
    })
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value=manifest_json)
    mock_ai.get_role_chain = MagicMock(return_value=["gemini-flash"])
    pool = AgentPool(ai_backend=mock_ai)
    result = asyncio.run(pool._giant_brain_audit(tmp_path, "test output"))
    assert result["fixes"][0]["complexity"] == "mechanical"


# ── _run_fixer_pass ───────────────────────────────────────────────────────────

def test_run_fixer_pass_empty_fixes_returns_empty_list(tmp_path):
    """_run_fixer_pass with an empty fixes list must return [] without calling the LLM."""
    from unittest.mock import MagicMock
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value="")
    pool = AgentPool(ai_backend=mock_ai)
    result = asyncio.run(pool._run_fixer_pass({"fixes": []}, tmp_path))
    assert result == []
    mock_ai.complete.assert_not_awaited()


def test_run_fixer_pass_mechanical_fix_uses_gemini_flash_backend(tmp_path):
    """Mechanical fixes must be dispatched with backend='gemini-flash'.
    Session 52: swapped from openai-codex — direct completion, no tool calls."""
    from unittest.mock import MagicMock
    target = tmp_path / "converter.py"
    target.write_text("def old_name(): pass")
    manifest = {
        "fixes": [
            {
                "file": "converter.py",
                "complexity": "mechanical",
                "description": "Rename old_name to new_name",
                "action": "rename_function",
            }
        ]
    }
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value="def new_name(): pass")
    pool = AgentPool(ai_backend=mock_ai)
    asyncio.run(pool._run_fixer_pass(manifest, tmp_path))
    assert mock_ai.complete.called
    call_kwargs = mock_ai.complete.call_args.kwargs
    assert call_kwargs.get("backend") == "gemini-flash"


def test_run_fixer_pass_architectural_fix_uses_gemini_backend(tmp_path):
    """Architectural fixes must be dispatched with backend='gemini-customtools' (Session 58)."""
    from unittest.mock import MagicMock
    target = tmp_path / "logic.py"
    target.write_text("# placeholder")
    manifest = {
        "fixes": [
            {
                "file": "logic.py",
                "complexity": "architectural",
                "description": "Add temperature offset logic",
                "action": "add_feature",
            }
        ]
    }
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value="# full implementation")
    pool = AgentPool(ai_backend=mock_ai)
    asyncio.run(pool._run_fixer_pass(manifest, tmp_path))
    assert mock_ai.complete.called
    call_kwargs = mock_ai.complete.call_args.kwargs
    assert call_kwargs.get("backend") == "gemini-customtools"


def test_run_fixer_pass_writes_updated_content_to_file(tmp_path):
    """_run_fixer_pass must write the LLM-returned content back to the target file."""
    from unittest.mock import MagicMock
    target = tmp_path / "module.py"
    target.write_text("def old(): pass")
    manifest = {
        "fixes": [
            {
                "file": "module.py",
                "complexity": "mechanical",
                "description": "Rename old to new",
                "action": "rename_function",
            }
        ]
    }
    updated_source = "def new(): pass"
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value=updated_source)
    pool = AgentPool(ai_backend=mock_ai)
    asyncio.run(pool._run_fixer_pass(manifest, tmp_path))
    assert target.read_text() == updated_source


def test_run_fixer_pass_preserves_version_in_init_py(tmp_path):
    """Mechanical fixer pass should write content that preserves __version__ when model output includes it."""
    from unittest.mock import MagicMock

    init_file = tmp_path / "__init__.py"
    init_file.write_text(
        "__version__ = \"1.2.3\"\nfrom .converter import convert_units\n",
        encoding="utf-8",
    )

    manifest = {
        "fixes": [
            {
                "file": "__init__.py",
                "complexity": "mechanical",
                "description": "Fix import path",
                "action": "add_import",
            }
        ]
    }

    updated_source = "__version__ = \"1.2.3\"\nfrom .converter import convert_units\n"
    mock_ai = MagicMock()
    mock_ai.complete = AsyncMock(return_value=updated_source)

    pool = AgentPool(ai_backend=mock_ai)
    asyncio.run(pool._run_fixer_pass(manifest, tmp_path))

    content = init_file.read_text(encoding="utf-8")
    assert "__version__" in content


def test_collect_test_output_includes_install_error():
    from unittest.mock import MagicMock

    pool = AgentPool(ai_backend=MagicMock())
    summary = SwarmSummary(total=0, passed=0, failed=0, duration=0.0, results=[], install_error="ValueError: version not found")

    out = pool._collect_test_output(summary)

    assert "Package Install Error" in out


def test_collect_test_output_install_success_prefix():
    from unittest.mock import MagicMock

    pool = AgentPool(ai_backend=MagicMock())
    summary = SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[], install_error="", install_hard_fail=False)

    out = pool._collect_test_output(summary)

    assert out.startswith("## Install Status: SUCCESS")


def test_collect_test_output_install_hard_fail_prefix():
    from unittest.mock import MagicMock

    pool = AgentPool(ai_backend=MagicMock())
    summary = SwarmSummary(total=0, passed=0, failed=0, duration=0.1, results=[], install_error="pip fail", install_hard_fail=True)

    out = pool._collect_test_output(summary)

    assert out.startswith("## Install Status: HARD FAIL")


def test_naming_triage_not_called_from_giant_brain_repair(mock_ai_backend, tmp_path):
    """Session 51: _naming_triage must not be called from _run_giant_brain_repair.
    ADR-016 triage was removed; Giant Brain 3-phase audit replaces it entirely."""
    pool = AgentPool(ai_backend=mock_ai_backend)
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    initial_summary = SwarmSummary(total=1, passed=0, failed=1, duration=0.1, results=[])

    with patch("core.orchestrator.ProjectOrchestrator") as orchestrator_cls, \
         patch.object(pool, "_naming_triage", new_callable=AsyncMock, return_value=[]) as triage_mock, \
         patch.object(pool, "_giant_brain_audit", new_callable=AsyncMock, return_value=None), \
         patch.object(pool, "_rerun_tests", new_callable=AsyncMock, return_value=initial_summary):

        orchestrator_cls.return_value.score_subtask.return_value = None

        asyncio.run(
            pool._run_giant_brain_repair(
                swarm_summary=initial_summary,
                export_dir=export_dir,
                project_context={"name": "x"},
                session_id="session-test",
            )
        )

    triage_mock.assert_not_called()


def test_naming_triage_not_called_from_giant_brain_repair_test_file_case(mock_ai_backend, tmp_path):
    """Session 51: confirms test-file subtask filtering logic is no longer needed
    because _naming_triage is never invoked from _run_giant_brain_repair."""
    pool = AgentPool(ai_backend=mock_ai_backend)
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    initial_summary = SwarmSummary(total=1, passed=0, failed=1, duration=0.1, results=[])

    with patch("core.orchestrator.ProjectOrchestrator") as orchestrator_cls, \
         patch.object(pool, "_naming_triage", new_callable=AsyncMock, return_value=[]) as triage_mock, \
         patch.object(pool, "_execute_subtask", new_callable=AsyncMock) as exec_mock, \
         patch.object(pool, "_giant_brain_audit", new_callable=AsyncMock, return_value=None), \
         patch.object(pool, "_rerun_tests", new_callable=AsyncMock, return_value=initial_summary):

        orchestrator_cls.return_value.score_subtask.return_value = None

        asyncio.run(
            pool._run_giant_brain_repair(
                swarm_summary=initial_summary,
                export_dir=export_dir,
                project_context={"name": "x"},
                session_id="session-test-2",
            )
        )

    triage_mock.assert_not_called()
    # _execute_subtask should NOT be called for triage (Giant Brain handles repair)
    exec_mock.assert_not_called()


def test_giant_brain_skips_on_install_hard_fail(mock_ai_backend, tmp_path):
    events = []

    async def collect_events(event_type, data):
        events.append((event_type, data))

    pool = AgentPool(ai_backend=mock_ai_backend, sse_broadcast=collect_events)
    hard_fail_summary = SwarmSummary(
        total=0,
        passed=0,
        failed=0,
        duration=0.1,
        results=[],
        install_error="install failed",
        install_hard_fail=True,
    )

    with patch.object(pool, "_giant_brain_audit", new_callable=AsyncMock) as audit_mock:
        final_summary, repair_history = asyncio.run(
            pool._run_giant_brain_repair(
                swarm_summary=hard_fail_summary,
                export_dir=tmp_path,
                project_context={"name": "Hard Fail Project"},
                session_id="session-hard-fail",
            )
        )

    assert final_summary.install_hard_fail is True
    assert repair_history == []
    audit_mock.assert_not_awaited()

    user_debug_events = [payload for name, payload in events if name == "user_guided_debug_state"]
    assert user_debug_events
    assert "Install hard fail" in user_debug_events[-1]["message"]


def test_gemini_flash_high_backend_routes_with_high_thinking_budget():
    """gemini-flash-high must call _gemini_complete with thinking_level='HIGH'."""
    from core.ai_backend import AIBackend
    from unittest.mock import AsyncMock, patch, MagicMock

    mock_settings = MagicMock()
    mock_settings.GOOGLE_API_KEY = "test-key"
    mock_settings.GOOGLE_GEMINI_MODEL = "gemini-3.0-flash"

    backend = AIBackend(mock_settings)

    captured = {}

    async def fake_gemini_complete(system, message, history, **kwargs):
        captured.update(kwargs)
        return "ok"

    with patch.object(backend, "_gemini_complete", side_effect=fake_gemini_complete):
        import asyncio
        result = asyncio.run(backend.complete(
            system="sys",
            message="msg",
            backend="gemini-flash-high",
        ))

    assert result == "ok"
    assert captured.get("thinking_level") == "HIGH"
    # Session 56: model now reads from settings.GEMINI_FLASH_MODEL, not hardcoded
    assert captured.get("model") == mock_settings.GEMINI_FLASH_MODEL


def test_gemini_flash_minimal_backend_routes_with_low_thinking_budget():
    """gemini-flash-minimal must call _gemini_complete with thinking_level='MINIMAL'."""
    from core.ai_backend import AIBackend
    from unittest.mock import AsyncMock, patch, MagicMock

    mock_settings = MagicMock()
    mock_settings.GOOGLE_API_KEY = "test-key"
    mock_settings.GOOGLE_GEMINI_MODEL = "gemini-3.0-flash"

    backend = AIBackend(mock_settings)

    captured = {}

    async def fake_gemini_complete(system, message, history, **kwargs):
        captured.update(kwargs)
        return "ok"

    with patch.object(backend, "_gemini_complete", side_effect=fake_gemini_complete):
        import asyncio
        result = asyncio.run(backend.complete(
            system="sys",
            message="msg",
            backend="gemini-flash-minimal",
        ))

    assert result == "ok"
    assert captured.get("thinking_level") == "MINIMAL"
    # Session 56: model now reads from settings.GEMINI_FLASH_MODEL, not hardcoded
    assert captured.get("model") == mock_settings.GEMINI_FLASH_MODEL


def test_thinking_level_override_passes_through_to_gemini_customtools():
    """Session 58: caller-supplied thinking_level overrides backend default on gemini-customtools.

    Big Fixer passes thinking_level="MINIMAL" so the large structured prompt
    doesn't exhaust the token budget on thought tokens before any tool call.
    """
    from core.ai_backend import AIBackend
    from unittest.mock import patch, MagicMock

    mock_settings = MagicMock()
    mock_settings.GOOGLE_API_KEY = "test-key"
    backend = AIBackend(mock_settings)
    captured = {}

    async def fake_gemini_complete(system, message, history, **kwargs):
        captured.update(kwargs)
        return "ok"

    with patch.object(backend, "_gemini_complete", side_effect=fake_gemini_complete):
        import asyncio
        asyncio.run(backend.complete(
            system="sys",
            message="msg",
            backend="gemini-customtools",
            thinking_level="MINIMAL",
        ))

    # Override must reach _gemini_complete — not the "LOW" default
    assert captured.get("thinking_level") == "MINIMAL"


def test_thinking_level_override_passes_through_to_gemini_flash_high():
    """Session 58: caller-supplied thinking_level overrides "HIGH" default on gemini-flash-high."""
    from core.ai_backend import AIBackend
    from unittest.mock import patch, MagicMock

    mock_settings = MagicMock()
    mock_settings.GOOGLE_API_KEY = "test-key"
    backend = AIBackend(mock_settings)
    captured = {}

    async def fake_gemini_complete(system, message, history, **kwargs):
        captured.update(kwargs)
        return "ok"

    with patch.object(backend, "_gemini_complete", side_effect=fake_gemini_complete):
        import asyncio
        asyncio.run(backend.complete(
            system="sys",
            message="msg",
            backend="gemini-flash-high",
            thinking_level="MINIMAL",
        ))

    # Override must reach _gemini_complete — not the "HIGH" default
    assert captured.get("thinking_level") == "MINIMAL"


def test_giant_brain_audit_retries_once_on_transient_failure(tmp_path):
    """ADR-039: transient backend failure moves to the next backend in the chain.
    Primary raises → fallback succeeds → manifest returned.
    """
    from unittest.mock import MagicMock

    valid_manifest_json = json.dumps({
        "summary": "Recovered after transient connection drop",
        "escalate": False,
        "fixes": [
            {
                "file": "converter.py",
                "complexity": "mechanical",
                "description": "Restore expected symbol name",
                "action": "rename_function",
            }
        ],
    })

    mock_ai = MagicMock()
    # Two backends: primary raises, fallback returns valid JSON
    mock_ai.get_role_chain = MagicMock(return_value=["gemini-flash", "gemini"])
    mock_ai.complete = AsyncMock(side_effect=[RuntimeError("connection dropped"), valid_manifest_json])
    pool = AgentPool(ai_backend=mock_ai)

    result = asyncio.run(pool._giant_brain_audit(tmp_path, "test output"))

    assert result is not None

