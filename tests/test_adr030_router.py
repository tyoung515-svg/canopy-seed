"""
Unit tests for ADR-030 three-tier threshold router and Big Fixer infrastructure.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path


def make_swarm_summary(passed, failed, total=None, install_hard_fail=False):
    """Helper: create a minimal SwarmSummary-like object."""
    s = MagicMock()
    s.passed = passed
    s.failed = failed
    s.total = total if total is not None else (passed + failed)
    s.install_hard_fail = install_hard_fail
    s.results = []
    return s


class TestPostGiantBrainRouterThresholds:
    """ADR-030: _post_giant_brain_router routes correctly for all three tiers."""

    def _make_pool(self):
        from core.agent_pool import AgentPool
        pool = AgentPool.__new__(AgentPool)
        pool.settings = MagicMock()
        pool.settings.BIG_FIXER_LOW_THRESHOLD = 70
        pool.settings.BIG_FIXER_HIGH_THRESHOLD = 95
        pool.settings.BIG_FIXER_MAX_ATTEMPTS = 2
        pool.ai = MagicMock()
        pool.ai._gemini_pro_degraded = False
        pool._sse_broadcast = None
        return pool

    def test_dump_tier_below_low_threshold(self):
        """Pass rate 50% < 70% → DUMP (user_guided_debug_state, no Big Fixer)."""
        pool = self._make_pool()
        summary = make_swarm_summary(passed=5, failed=5, total=10)
        broadcast_events = []

        async def fake_broadcast(event, data):
            broadcast_events.append(event)

        pool._broadcast = fake_broadcast
        pool._run_big_fixer = AsyncMock()
        pool._run_fix3 = AsyncMock()

        result = asyncio.run(pool._post_giant_brain_router(
            swarm_summary=summary,
            export_dir=Path("/tmp/fake"),
            repair_history=[],
            project_context={"project_name": "test"},
            session_id="test-session",
        ))

        assert "user_guided_debug_state" in broadcast_events
        pool._run_big_fixer.assert_not_called()
        pool._run_fix3.assert_not_called()

    def test_big_fixer_tier_in_mid_range(self):
        """Pass rate 85% (70–95%) → Big Fixer directly, no Fix #3."""
        pool = self._make_pool()
        summary = make_swarm_summary(passed=85, failed=15, total=100)
        fixed_summary = make_swarm_summary(passed=100, failed=0, total=100)

        pool._broadcast = AsyncMock()
        pool._run_big_fixer = AsyncMock(return_value=(fixed_summary, []))
        pool._run_fix3 = AsyncMock()

        asyncio.run(pool._post_giant_brain_router(
            swarm_summary=summary,
            export_dir=Path("/tmp/fake"),
            repair_history=[],
            project_context={"project_name": "test"},
            session_id="test-session",
        ))

        pool._run_big_fixer.assert_called_once()
        pool._run_fix3.assert_not_called()

    def test_fix3_tier_above_high_threshold(self):
        """Pass rate 97% ≥ 95% → Fix #3 first."""
        pool = self._make_pool()
        summary = make_swarm_summary(passed=97, failed=3, total=100)
        fixed_summary = make_swarm_summary(passed=100, failed=0, total=100)

        pool._broadcast = AsyncMock()
        pool._run_fix3 = AsyncMock(return_value=fixed_summary)
        pool._run_big_fixer = AsyncMock()

        result = asyncio.run(pool._post_giant_brain_router(
            swarm_summary=summary,
            export_dir=Path("/tmp/fake"),
            repair_history=[],
            project_context={"project_name": "test"},
            session_id="test-session",
        ))

        pool._run_fix3.assert_called_once()
        pool._run_big_fixer.assert_not_called()
        assert result.failed == 0

    def test_fix3_falls_through_to_big_fixer_on_failure(self):
        """Fix #3 fails → falls through to Big Fixer."""
        pool = self._make_pool()
        summary = make_swarm_summary(passed=97, failed=3, total=100)
        still_failing = make_swarm_summary(passed=97, failed=3, total=100)
        fixed = make_swarm_summary(passed=100, failed=0, total=100)

        pool._broadcast = AsyncMock()
        pool._run_fix3 = AsyncMock(return_value=still_failing)
        pool._run_big_fixer = AsyncMock(return_value=(fixed, []))

        asyncio.run(pool._post_giant_brain_router(
            swarm_summary=summary,
            export_dir=Path("/tmp/fake"),
            repair_history=[],
            project_context={"project_name": "test"},
            session_id="test-session",
        ))

        pool._run_fix3.assert_called_once()
        pool._run_big_fixer.assert_called_once()

    def test_collection_error_bypasses_threshold(self):
        """0 tests collected → Big Fixer directly, regardless of pass rate."""
        pool = self._make_pool()
        summary = make_swarm_summary(passed=0, failed=0, total=0)
        fixed = make_swarm_summary(passed=10, failed=0, total=10)

        pool._broadcast = AsyncMock()
        pool._run_big_fixer = AsyncMock(return_value=(fixed, []))
        pool._run_fix3 = AsyncMock()

        asyncio.run(pool._post_giant_brain_router(
            swarm_summary=summary,
            export_dir=Path("/tmp/fake"),
            repair_history=[],
            project_context={"project_name": "test"},
            session_id="test-session",
        ))

        pool._run_big_fixer.assert_called_once()
        pool._run_fix3.assert_not_called()

    def test_collection_error_single_test_bypasses_threshold(self):
        """1 test collected (partial collection anomaly) → Big Fixer directly, not DUMP."""
        pool = self._make_pool()
        summary = make_swarm_summary(passed=0, failed=1, total=1)
        fixed = make_swarm_summary(passed=10, failed=0, total=10)

        pool._broadcast = AsyncMock()
        pool._run_big_fixer = AsyncMock(return_value=(fixed, []))
        pool._run_fix3 = AsyncMock()

        asyncio.run(pool._post_giant_brain_router(
            swarm_summary=summary,
            export_dir=Path("/tmp/fake"),
            repair_history=[],
            project_context={"project_name": "test"},
            session_id="test-session",
        ))

        pool._run_big_fixer.assert_called_once()
        pool._run_fix3.assert_not_called()


class TestBigFixerSettings:
    """ADR-030: Settings constants are present and readable."""

    def test_big_fixer_settings_present(self):
        from config.settings import Settings
        s = Settings()
        assert hasattr(s, "BIG_FIXER_LOW_THRESHOLD")
        assert hasattr(s, "BIG_FIXER_HIGH_THRESHOLD")
        assert hasattr(s, "BIG_FIXER_MAX_ATTEMPTS")
        assert s.BIG_FIXER_LOW_THRESHOLD == 40
        assert s.BIG_FIXER_HIGH_THRESHOLD == 95
        assert s.BIG_FIXER_MAX_ATTEMPTS == 2
