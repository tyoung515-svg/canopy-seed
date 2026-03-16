"""
Agent Context
Manages conversation history and persistent memory
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Max messages to keep in active history (to stay within token limits)
MAX_HISTORY = 20


class AgentContext:
    def __init__(self, settings, memory_store=None):
        self.settings = settings
        self.memory_store = memory_store
        self._history: List[Dict] = []
        self._pending_command: Optional[str] = None
        self._history_file = Path("logs/conversation_history.jsonl")
        self._load_history()

    # --- Conversation History ---

    def get_history(self) -> List[Dict]:
        """Return recent conversation history"""
        return self._history[-MAX_HISTORY:]

    def add_to_history(self, role: str, content: str):
        """Add message to history and persist it"""
        entry = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        }
        self._history.append(entry)

        # Persist to disk
        try:
            if not self._history_file.parent.exists():
                self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception as e:
            logger.warning(f"Failed to persist history: {e}")

    def _load_history(self):
        """Load persisted conversation history from disk on startup."""
        try:
            if self._history_file.exists():
                entries = []
                with open(self._history_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                # Only load the last MAX_HISTORY entries
                self._history = entries[-MAX_HISTORY:]
                logger.info(f"Loaded {len(self._history)} messages from history.")
        except Exception as e:
            logger.warning(f"Failed to load history: {e}")
            self._history = []

    def clear_history(self):
        self._history = []
        # Also wipe the file so it doesn't reload on next restart
        try:
            self._history_file.write_text('', encoding='utf-8')
        except Exception as e:
            logger.warning(f"Failed to clear history file: {e}")

    # --- Pending Command (confirmation flow) ---

    def set_pending_command(self, cmd: str):
        self._pending_command = cmd

    def get_pending_command(self) -> Optional[str]:
        return self._pending_command

    def clear_pending_command(self):
        self._pending_command = None