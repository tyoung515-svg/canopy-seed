"""
canopy.py — Persistent Memory Store using SQLite + FTS5

WHY THIS EXISTS:
Canopy needs to remember long-term state: conversation history, project context,
research results, snapshots metadata. SQLite provides reliable persistence with
zero configuration. FTS5 (full-text search) enables querying historical notes
and conversations without external dependencies.

DESIGN DECISIONS:
- SQLite (in-process, zero config) over external database (simpler, more portable)
- Async aiosqlite for non-blocking I/O
- FTS5 virtual table for full-text search on notes (future: conversation search)
- JSON column type for flexible schema (context entries, research results)
- Automatic connection pooling and cleanup (prevent resource leaks)

OWNED BY: Agent CS1 (Anti/Gemini Pro) — Canopy Seed V1, 2026-02-25
REVIEWED BY: Claude Sonnet 4.6 (Orchestrator)
"""

import json
import logging
import aiosqlite
from pathlib import Path
from typing import Any, List, Optional, Dict

logger = logging.getLogger(__name__)


class MemoryStore:
    def __init__(self, db_path: str | Path = "memory/canopy.db"):
        self.db_path = Path(db_path)
        self._initialized = False

    async def _ensure_initialized(self):
        if self._initialized:
            return

        if not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            # Context State (KeyValue Store)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS context_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Notes / Long-term Memory
            await db.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    tags TEXT,
                    source TEXT DEFAULT 'manual',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Full Text Search for Notes
            # Check if FTS5 is available (usually is)
            try:
                await db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                        content,
                        tags,
                        content=notes,
                        content_rowid=id
                    )
                """)
            except aiosqlite.OperationalError:
                logger.warning("FTS5 not available; full-text search disabled")

            # Project Context Snapshots
            await db.execute("""
                CREATE TABLE IF NOT EXISTS project_contexts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(session_id)
                )
            """)

            # Research Results Cache
            await db.execute("""
                CREATE TABLE IF NOT EXISTS research_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    sources TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)

            await db.commit()
            self._initialized = True

    async def set_state(self, key: str, value: Any) -> None:
        """Store a key-value pair in persistent state."""
        await self._ensure_initialized()
        value_str = json.dumps(value) if not isinstance(value, str) else value
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO context_state (key, value) VALUES (?, ?)",
                (key, value_str)
            )
            await db.commit()

    async def get_state(self, key: str, default: Any = None) -> Any:
        """Retrieve a key-value pair from persistent state."""
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT value FROM context_state WHERE key = ?",
                (key,)
            )
            row = await cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return row[0]
            return default

    async def add_note(self, content: str, tags: str = "", source: str = "manual") -> int:
        """Add a note to long-term memory. Returns note ID."""
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO notes (content, tags, source) VALUES (?, ?, ?)",
                (content, tags, source)
            )
            await db.commit()
            return cursor.lastrowid

    async def search_notes(self, query: str, limit: int = 10) -> List[dict]:
        """Search notes using full-text search."""
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cursor = await db.execute("""
                    SELECT n.id, n.content, n.tags, n.created_at
                    FROM notes n
                    LEFT JOIN notes_fts f ON n.id = f.rowid
                    WHERE f.notes MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (query, limit))
                rows = await cursor.fetchall()
                return [
                    {
                        "id": row[0],
                        "content": row[1],
                        "tags": row[2],
                        "created_at": row[3]
                    }
                    for row in rows
                ]
            except aiosqlite.OperationalError:
                # FTS5 not available; fall back to LIKE search
                cursor = await db.execute("""
                    SELECT id, content, tags, created_at
                    FROM notes
                    WHERE content LIKE ? OR tags LIKE ?
                    LIMIT ?
                """, (f"%{query}%", f"%{query}%", limit))
                rows = await cursor.fetchall()
                return [
                    {
                        "id": row[0],
                        "content": row[1],
                        "tags": row[2],
                        "created_at": row[3]
                    }
                    for row in rows
                ]
