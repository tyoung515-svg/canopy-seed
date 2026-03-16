"""
Brain State Manager
-------------------
Tracks the high-level 'state of the brain' for Canopy Seed.
Maintains a markdown file (docs/brain.md) that serves as the
primary context for the AI, listing active tasks, memory bank locations,
and project status.
"""

import os
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Default locations adjustments for Canopy Seed
DEFAULT_BRAIN_MD = Path("docs/brain.md")
DEFAULT_BRAIN_DB = Path("memory/brain.db") 
DEFAULT_CHANGELOG_DB = Path("memory/master_changelog.db")

class BrainStateManager:
    """
    Manages the 'Brain' — a combination of a persistent Markdown dashboard
    and a structured SQLite database for task tracking.
    """

    def __init__(self, root_dir: str):
        self.root = Path(root_dir)
        self.md_path = self.root / DEFAULT_BRAIN_MD
        self.db_path = self.root / DEFAULT_BRAIN_DB
        self.changelog_path = self.root / DEFAULT_CHANGELOG_DB

        # Ensure directories exist
        self.md_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.changelog_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _init_db(self):
        """Initialize the brain state SQLite tables."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Core state table
        c.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at DATETIME
            )
        """)
        
        # Active tasks
        c.execute("""
            CREATE TABLE IF NOT EXISTS active_tasks (
                task_id TEXT PRIMARY KEY,
                description TEXT,
                status TEXT,  -- 'pending', 'in_progress', 'blocked', 'done'
                priority INTEGER,
                assigned_agent TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
        """)
        
        conn.commit()
        conn.close()

    def update_state(self, key: str, value: str):
        """Update a key-value pair in the brain state."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO state (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, datetime.now()))
        conn.commit()
        conn.close()
        # Trigger markdown update after state change
        self.sync_markdown()

    def get_state(self, key: str) -> Optional[str]:
        """Retrieve a value from the brain state."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT value FROM state WHERE key = ?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def add_task(self, task_id: str, description: str, priority: int = 1):
        """Register a new task in the brain."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO active_tasks 
            (task_id, description, status, priority, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?, ?)
        """, (task_id, description, priority, datetime.now(), datetime.now()))
        conn.commit()
        conn.close()
        self.sync_markdown()

    def complete_task(self, task_id: str):
        """Mark a task as done."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            UPDATE active_tasks 
            SET status = 'done', updated_at = ?
            WHERE task_id = ?
        """, (datetime.now(), task_id))
        conn.commit()
        conn.close()
        self.sync_markdown()

    def sync_markdown(self):
        """
        Regenerates the docs/brain.md file based on current DB state.
        This file is read by the AI to understand 'who am I and what am I doing?'.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Get raw state
        c.execute("SELECT key, value FROM state")
        kv_state = dict(c.fetchall())
        
        # Get pending/active tasks
        c.execute("""
            SELECT task_id, description, status, priority 
            FROM active_tasks 
            WHERE status != 'done' 
            ORDER BY priority DESC, created_at ASC
        """)
        tasks = c.fetchall()
        
        conn.close()

        # Build Markdown Content
        lines = [
            "# Canopy Seed Brain State",
            f"**Last Sync:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 🧠 Context & Identity",
            f"- **Project Phase:** {kv_state.get('phase', 'Initialization')}",
            f"- **Current Focus:** {kv_state.get('focus', 'System Bootstrap')}",
            f"- **Mode:** {kv_state.get('mode', 'listen')}",
            "",
            "## 📋 Active Tasks",
        ]

        if not tasks:
            lines.append("_No active tasks tracked in Brain DB._")
        else:
            for tid, desc, status, prio in tasks:
                icon = "🔴" if prio > 2 else "jq" if status == 'in_progress' else "⚪"
                lines.append(f"- {icon} **[{tid}]** {desc} `({status})`")

        lines.append("")
        lines.append("## 💾 Memory Banks")
        lines.append(f"- **Main DB:** `{DEFAULT_BRAIN_DB}`")
        lines.append(f"- **Changelog:** `{DEFAULT_CHANGELOG_DB}`")
        
        # Write to file
        try:
            with open(self.md_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines))
        except Exception as e:
            logger.error(f"Failed to sync brain markdown: {e}")

    def ingest_context(self, context_text: str):
        """
        Allows the AI to dump summary context into the brain state
        to persist 'thought validation' between sessions.
        """
        self.update_state("recent_thought_dump", context_text[:2000]) # Cap size

