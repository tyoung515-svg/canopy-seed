"""
Orchestration Board (ADR-032)
─────────────────────────────
Hybrid Shared Blackboard + Task State Board for swarm coordination.

Design (ADR-032 A+C hybrid):
  A — Shared Blackboard: agents write key/value facts as they work
      (exports, contracts, API shapes). Other agents read these to
      avoid guessing about sibling files.
  C — Task State Board: each subtask reports its current status and
      exports list. The orchestrator watches the snapshot and can post
      a nudge (directive) to any in-progress task.

All state is in-process and thread-safe. The board is created once per
build and passed into _execute_subtask by agent_pool. It is not
persisted between builds.

Usage
─────
    board = OrchestrationBoard(build_id="build-123")

    # Agent writes its exported symbols
    board.write("utils.py::exports", ["UtilClass", "helper_fn"], author="utils.py")

    # Agent reads a sibling's exports
    exports = board.read("utils.py::exports")

    # Agent reports its task state
    board.update_task("test_utils.py", status="in_progress", exports=["TestUtils"])

    # Orchestrator checks the board
    snapshot = board.get_snapshot()

    # Orchestrator nudges an in-progress agent
    board.post_nudge("test_utils.py", "Import UtilClass from utils, not util")

    # Agent checks for nudges before writing its next file
    nudges = board.get_nudges("test_utils.py")
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class BlackboardEntry:
    """A single fact written to the shared blackboard."""
    key: str
    value: Any
    author: str          # typically the target_file name that wrote this
    written_at: float = field(default_factory=time.monotonic)


@dataclass
class TaskState:
    """Snapshot of a single subtask's execution state."""
    task_id: str         # typically the target_file path
    status: str          # "pending" | "in_progress" | "complete" | "failed"
    exports: List[str] = field(default_factory=list)   # symbols / APIs exported
    issues: List[str] = field(default_factory=list)    # self-reported problems
    updated_at: float = field(default_factory=time.monotonic)


@dataclass
class Nudge:
    """A directive from the orchestrator to a specific in-progress task."""
    task_id: str
    message: str         # e.g. "Use TokenType enum from lexer.py, not raw strings"
    from_orchestrator: bool = True
    created_at: float = field(default_factory=time.monotonic)
    consumed: bool = False


# ─── Board ────────────────────────────────────────────────────────────────────

class OrchestrationBoard:
    """
    Thread-safe in-process orchestration board for a single build.

    One instance is created per build run and injected into each subtask.
    After the build completes the board is discarded — it is not persisted.
    """

    def __init__(self, build_id: str = "", max_nudges: int = 5):
        self.build_id = build_id
        self.max_nudges = max_nudges
        self._lock = asyncio.Lock()

        # Shared blackboard: key → BlackboardEntry
        self._blackboard: Dict[str, BlackboardEntry] = {}

        # Task state registry: task_id → TaskState
        self._tasks: Dict[str, TaskState] = {}

        # Nudge queue: task_id → List[Nudge]
        self._nudges: Dict[str, List[Nudge]] = {}

        logger.debug(f"OrchestrationBoard created for build '{build_id}'")

    # ─── Blackboard API ───────────────────────────────────────────────────────

    async def write(self, key: str, value: Any, author: str = "") -> None:
        """Write a fact to the shared blackboard (overwrites if key exists)."""
        async with self._lock:
            self._blackboard[key] = BlackboardEntry(key=key, value=value, author=author)
            logger.debug(f"Board.write [{author}] {key!r} = {str(value)[:120]}")

    async def read(self, key: str) -> Optional[Any]:
        """Read a fact from the shared blackboard. Returns None if not set."""
        async with self._lock:
            entry = self._blackboard.get(key)
            return entry.value if entry else None

    async def read_entry(self, key: str) -> Optional[BlackboardEntry]:
        """Read the full BlackboardEntry (includes author + timestamp)."""
        async with self._lock:
            return self._blackboard.get(key)

    async def all_keys(self) -> List[str]:
        """Return all keys currently on the blackboard."""
        async with self._lock:
            return list(self._blackboard.keys())

    # ─── Task State API ───────────────────────────────────────────────────────

    async def update_task(
        self,
        task_id: str,
        status: str,
        exports: Optional[List[str]] = None,
        issues: Optional[List[str]] = None,
    ) -> None:
        """Update (or create) a task's state on the board."""
        async with self._lock:
            existing = self._tasks.get(task_id)
            if existing:
                existing.status = status
                existing.updated_at = time.monotonic()
                if exports is not None:
                    existing.exports = exports
                if issues is not None:
                    existing.issues = issues
            else:
                self._tasks[task_id] = TaskState(
                    task_id=task_id,
                    status=status,
                    exports=exports or [],
                    issues=issues or [],
                )
            logger.debug(f"Board.update_task {task_id!r} → {status}")

    async def get_task(self, task_id: str) -> Optional[TaskState]:
        """Return the current state for a task, or None if unknown."""
        async with self._lock:
            return self._tasks.get(task_id)

    async def get_all_tasks(self) -> List[TaskState]:
        """Return all registered task states."""
        async with self._lock:
            return list(self._tasks.values())

    # ─── Nudge API ────────────────────────────────────────────────────────────

    async def post_nudge(self, task_id: str, message: str) -> bool:
        """
        Post a directive from the orchestrator to a specific task.

        Returns False if the nudge queue for this task is already at capacity
        (max_nudges), in which case the nudge is dropped to avoid overload.
        """
        async with self._lock:
            queue = self._nudges.setdefault(task_id, [])
            unconsumed = [n for n in queue if not n.consumed]
            if len(unconsumed) >= self.max_nudges:
                logger.warning(
                    f"Board.post_nudge: queue full for {task_id!r} "
                    f"(max_nudges={self.max_nudges}) — nudge dropped"
                )
                return False
            queue.append(Nudge(task_id=task_id, message=message))
            logger.info(f"Board.post_nudge → {task_id!r}: {message[:80]}")
            return True

    async def get_nudges(self, task_id: str, consume: bool = True) -> List[str]:
        """
        Retrieve pending nudge messages for a task.

        If consume=True (default), marks them as consumed so they are not
        returned again on subsequent calls.
        """
        async with self._lock:
            queue = self._nudges.get(task_id, [])
            pending = [n for n in queue if not n.consumed]
            if consume:
                for n in pending:
                    n.consumed = True
            return [n.message for n in pending]

    # ─── Snapshot ─────────────────────────────────────────────────────────────

    async def get_snapshot(self) -> dict:
        """
        Return a full read-only snapshot of board state for the orchestrator.

        Returns a plain dict (safe to serialize / log).
        """
        async with self._lock:
            return {
                "build_id": self.build_id,
                "blackboard": {
                    k: {"value": str(v.value)[:500], "author": v.author}
                    for k, v in self._blackboard.items()
                },
                "tasks": {
                    tid: {
                        "status": t.status,
                        "exports": t.exports,
                        "issues": t.issues,
                    }
                    for tid, t in self._tasks.items()
                },
                "pending_nudges": {
                    tid: [n.message for n in nudges if not n.consumed]
                    for tid, nudges in self._nudges.items()
                    if any(not n.consumed for n in nudges)
                },
            }

    # ─── Helpers ──────────────────────────────────────────────────────────────

    async def format_context_for_agent(self, task_id: str) -> str:
        """
        Build a compact context block for injection into a swarm agent's system prompt.

        Includes:
          - Relevant blackboard entries (excluding the agent's own writes)
          - Pending nudges for this agent
          - Status of other tasks (completed only, to show what's available)

        Keeps output ≤ ~1500 tokens to avoid crowding the agent's context.
        """
        async with self._lock:
            lines: List[str] = []

            # Nudges first — highest priority
            pending_nudges = [n.message for n in self._nudges.get(task_id, []) if not n.consumed]
            if pending_nudges:
                lines.append("## ORCHESTRATOR DIRECTIVES (follow these exactly)")
                for msg in pending_nudges:
                    lines.append(f"- {msg}")
                lines.append("")

            # Blackboard entries from other agents — prioritize interface signatures
            # over bare export names for richer inter-agent communication.
            interface_entries = [
                entry for entry in self._blackboard.values()
                if entry.author != task_id and entry.key.endswith("::interfaces")
            ]
            other_entries = [
                entry for entry in self._blackboard.values()
                if entry.author != task_id and not entry.key.endswith("::interfaces")
            ]
            if interface_entries:
                lines.append("## Sibling File Interfaces (use these for imports)")
                for entry in interface_entries[:15]:
                    _file = entry.key.replace("::interfaces", "")
                    val_str = str(entry.value)[:500]
                    lines.append(f"### {_file}")
                    lines.append(f"```\n{val_str}\n```")
                lines.append("")
            if other_entries:
                lines.append("## Shared Contract Board (from sibling agents)")
                for entry in other_entries[:20]:  # cap at 20 entries
                    val_str = str(entry.value)[:200]
                    lines.append(f"- [{entry.author}] {entry.key}: {val_str}")
                lines.append("")

            # Completed sibling tasks and their exports
            complete_tasks = [
                t for tid, t in self._tasks.items()
                if tid != task_id and t.status == "complete" and t.exports
            ]
            if complete_tasks:
                lines.append("## Available Exports from Completed Siblings")
                for t in complete_tasks[:15]:
                    exports_str = ", ".join(t.exports[:10])
                    lines.append(f"- {t.task_id}: {exports_str}")
                lines.append("")

            return "\n".join(lines) if lines else ""
