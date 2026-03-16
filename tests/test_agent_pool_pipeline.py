import asyncio
from unittest.mock import AsyncMock, patch

from core.agent_pool import AgentPool, ContractArtifact, TaskSpec
from core.auto_fix import FixSummary
from core.orchestrator import OrchestratorSubTask
from core.tester_swarm import SwarmSummary


async def _collect_events(events, event_type, data):
    events.append((event_type, data))


def test_run_orchestrated_build_calls_tester_and_autofix_on_failures(mock_ai_backend):
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

    project_context = {"name": "Pipeline Project", "description": "test"}
    task_spec = TaskSpec(context=project_context)

    contract = ContractArtifact(
        test_file_name="test_pipeline_contract.py",
        test_file_content="def test_contract():\n    assert True\n",
        public_api_summary="Pipeline API",
    )

    with patch.object(pool, "_generate_contract", new_callable=AsyncMock, return_value=contract), \
         patch("core.agent_pool.ProjectOrchestrator") as orchestrator_cls, \
         patch.object(pool, "_execute_subtask", new_callable=AsyncMock, return_value={"status": "done"}) as execute_mock, \
            patch.object(pool, "_run_giant_brain_repair", new_callable=AsyncMock, return_value=(SwarmSummary(total=1, passed=1, failed=0, duration=0.1, results=[]), [{"round": 1, "phase": "giant_brain_audit"}])) as repair_mock, \
         patch("core.tester_swarm.TesterSwarm") as tester_cls, \
         patch("core.auto_fix.AutoFixLoop") as autofix_cls:

        orchestrator = orchestrator_cls.return_value
        orchestrator.decompose_and_score = AsyncMock(return_value=[subtask])

        tester = tester_cls.return_value
        tester.run = AsyncMock(return_value=SwarmSummary(total=1, passed=0, failed=1, duration=0.1, results=[]))

        fixer = autofix_cls.return_value
        fixer.run = AsyncMock(
            return_value=FixSummary(fixed=1, failed=0, restored=0, attempts=[], giant_brain_review="ok")
        )

        asyncio.run(pool.run_orchestrated_build(project_context, task_spec))

        orchestrator.decompose_and_score.assert_awaited_once()
        execute_mock.assert_awaited_once()
        tester.run.assert_awaited_once()
        repair_mock.assert_awaited_once()
        fixer.run.assert_awaited_once()
