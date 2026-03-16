import asyncio
from pathlib import Path

import aiosqlite

from core.master_changelog import MasterChangelog


def test_log_and_export_agent_action(tmp_path):
    db_path = tmp_path / "master_changelog.db"
    changelog = MasterChangelog(db_path=str(db_path))

    async def _run():
        await changelog.log_agent_action(
            session_id="session-a",
            agent_name="Claude",
            file_path="core/example.py",
            action="edit",
            diff="+line",
            complexity_score=3,
            risk_flags="low",
        )
        return await changelog.export_session("session-a")

    markdown = asyncio.run(_run())
    assert "Claude" in markdown


def test_export_includes_test_results(tmp_path):
    db_path = tmp_path / "master_changelog.db"
    changelog = MasterChangelog(db_path=str(db_path))

    async def _run():
        await changelog.log_agent_action(
            session_id="session-b",
            agent_name="Gemini",
            file_path="core/other.py",
            action="create",
            diff="+x",
            complexity_score=1,
            risk_flags="",
        )
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                INSERT INTO swarm_results (
                    session_id,
                    source_file,
                    test_file,
                    passed,
                    failure_output,
                    failure_summary,
                    duration
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("session-b", "core/other.py", "tests/test_other.py", 0, "assert", "failed test", 0.321),
            )
            await db.commit()
        return await changelog.export_session("session-b")

    markdown = asyncio.run(_run())
    assert "tests/test_other.py" in markdown


def test_list_sessions_returns_distinct(tmp_path):
    db_path = tmp_path / "master_changelog.db"
    changelog = MasterChangelog(db_path=str(db_path))

    async def _run():
        await changelog.log_agent_action(
            session_id="session-1",
            agent_name="A",
            file_path="a.py",
            action="edit",
            diff="+a",
            complexity_score=1,
            risk_flags="",
        )
        await changelog.log_agent_action(
            session_id="session-2",
            agent_name="B",
            file_path="b.py",
            action="edit",
            diff="+b",
            complexity_score=2,
            risk_flags="",
        )
        return await changelog.list_sessions()

    sessions = asyncio.run(_run())
    assert "session-1" in sessions
    assert "session-2" in sessions


def test_write_export_file_creates_file(tmp_path):
    db_path = tmp_path / "master_changelog.db"
    output_dir = tmp_path / "exports"
    changelog = MasterChangelog(db_path=str(db_path))

    async def _run():
        await changelog.log_agent_action(
            session_id="session-file",
            agent_name="Claude",
            file_path="core/file.py",
            action="edit",
            diff="+content",
            complexity_score=2,
            risk_flags="none",
        )
        return await changelog.write_export_file("session-file", output_dir=str(output_dir))

    output_path = asyncio.run(_run())
    assert Path(output_path).exists()
