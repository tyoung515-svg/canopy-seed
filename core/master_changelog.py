import datetime
from pathlib import Path

import aiosqlite


class MasterChangelog:
    def __init__(self, db_path="memory/master_changelog.db"):
        self.db_path = Path(db_path)

    async def _ensure_tables(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    agent_name TEXT,
                    file_path TEXT,
                    action TEXT,
                    diff TEXT,
                    complexity_score INTEGER,
                    risk_flags TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS swarm_results (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT,
                    source_file TEXT,
                    test_file TEXT,
                    passed INTEGER,
                    failure_output TEXT,
                    failure_summary TEXT,
                    duration REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS giant_brain_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    review_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()

    async def log_agent_action(
        self,
        session_id: str,
        agent_name: str,
        file_path: str,
        action: str,
        diff: str,
        complexity_score: int,
        risk_flags: str,
    ) -> None:
        await self._ensure_tables()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO agent_actions (
                    session_id,
                    agent_name,
                    file_path,
                    action,
                    diff,
                    complexity_score,
                    risk_flags
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_name,
                    file_path,
                    action,
                    diff,
                    complexity_score,
                    risk_flags,
                ),
            )
            await db.commit()

    async def export_session(self, session_id: str) -> str:
        await self._ensure_tables()

        async with aiosqlite.connect(self.db_path) as db:
            action_cursor = await db.execute(
                """
                SELECT agent_name, file_path, action, complexity_score, risk_flags
                FROM agent_actions
                WHERE session_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (session_id,),
            )
            action_rows = await action_cursor.fetchall()

            tests_cursor = await db.execute(
                """
                SELECT test_file, passed, duration, failure_summary
                FROM swarm_results
                WHERE session_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (session_id,),
            )
            test_rows = await tests_cursor.fetchall()

            review_cursor = await db.execute(
                """
                SELECT review_text
                FROM giant_brain_reviews
                WHERE session_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (session_id,),
            )
            review_row = await review_cursor.fetchone()

        generated = datetime.datetime.now().isoformat(timespec="seconds")
        lines = [
            "# Canopy Seed — Master Changelog",
            f"Session: {session_id}",
            f"Generated: {generated}",
            "",
            "## Agent Actions",
            "| Agent | File | Action | Complexity | Risk |",
            "|-------|------|--------|------------|------|",
        ]

        if action_rows:
            for row in action_rows:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            self._md(str(row[0] or "")),
                            self._md(str(row[1] or "")),
                            self._md(str(row[2] or "")),
                            self._md(str(row[3] if row[3] is not None else "")),
                            self._md(str(row[4] or "")),
                        ]
                    )
                    + " |"
                )
        else:
            lines.append("| — | — | — | — | — |")

        lines.extend(
            [
                "",
                "## Test Results",
                "| Test File | Passed | Duration | Failure Summary |",
                "|-----------|--------|----------|-----------------|",
            ]
        )

        if test_rows:
            for row in test_rows:
                test_file = self._md(str(row[0] or ""))
                passed = "Yes" if int(row[1] or 0) == 1 else "No"
                duration = f"{float(row[2] or 0.0):.3f}s"
                summary = self._md(str(row[3] or ""))
                lines.append(f"| {test_file} | {passed} | {duration} | {summary} |")
        else:
            lines.append("| — | — | — | — |")

        lines.extend(["", "## Giant Brain Review"])
        if review_row and review_row[0]:
            lines.append(str(review_row[0]))
        else:
            lines.append("No review recorded.")

        return "\n".join(lines) + "\n"

    async def list_sessions(self) -> list[str]:
        await self._ensure_tables()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT session_id
                FROM (
                    SELECT session_id, MAX(created_at) AS last_created_at
                    FROM (
                        SELECT session_id, created_at FROM agent_actions
                        UNION ALL
                        SELECT session_id, created_at FROM swarm_results
                        UNION ALL
                        SELECT session_id, created_at FROM giant_brain_reviews
                    ) all_sessions
                    WHERE session_id IS NOT NULL AND session_id != ''
                    GROUP BY session_id
                )
                ORDER BY last_created_at DESC
                """
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def write_export_file(self, session_id: str, output_dir: str = "exports") -> str:
        markdown = await self.export_session(session_id)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = out_dir / f"MASTER_CHANGELOG_{session_id}.md"
        file_path.write_text(markdown, encoding="utf-8")
        return str(file_path.resolve())

    def _md(self, text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ").strip()
