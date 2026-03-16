"""
Canopy Seed Agent Pool
---------------------
Manages the registry of AI agents available to the DevHub.

Agents register themselves via POST /api/devhub/agents/register and
are then available for task dispatch.
"""

import asyncio
import logging
import time
import os
import json
import re
from functools import partial
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from core.orchestrator import ProjectOrchestrator, OrchestratorSubTask
from core.complexity_judge import estimate_cost
from core.exceptions import ToolExhaustionError  # Finding 2: distinct from transient failures (avoids circular import)
from core.sdk_reference import render_sdk_guidance  # centralized SDK/model reference for all prompts

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Pre-rendered SDK guidance strings — computed once at import time.
# Using module-level constants avoids repeated function calls inside hot loops.
_sdk_ref_swarm    = render_sdk_guidance("swarm")
_sdk_ref_fixer    = render_sdk_guidance("fixer")
_sdk_ref_contract = render_sdk_guidance("contract")

GIANT_BRAIN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "escalate", "fixes"],
    "properties": {
        "summary": {"type": "string"},
        "escalate": {"type": "boolean"},
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "complexity", "description", "action"],
                "properties": {
                    "file": {"type": "string"},
                    "complexity": {
                        "type": "string",
                        "enum": ["mechanical", "architectural"],
                    },
                    "description": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": [
                            "rename_function", "add_import", "fix_signature",
                            "rewrite_exports", "add_feature", "fix_content",
                            "delete_file", "other",
                        ],
                    },
                },
            },
        },
    },
}

logger = logging.getLogger(__name__)

# ADR-039 / Session 57: JSON schema for Phase 0 contract generation.
# Passed as responseJsonSchema to constrain Gemini to return exactly
# a {test_file_name, test_file_content, public_api_summary, routes, env_vars} object.
# Uses standard JSON Schema lowercase types — compatible with responseJsonSchema key.
# Both responseMimeType AND responseJsonSchema must be set for schema enforcement to activate.
# ADR-042: Added routes (machine-readable route manifest) and env_vars to eliminate
# interface mismatches between swarm agents. Per Opus architecture review: highest-leverage
# single change for reducing repair cycles (40-60% reduction estimate).
CONTRACT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "test_file_name": {"type": "string"},
        "test_file_content": {"type": "string"},
        "public_api_summary": {"type": "string"},
        "routes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "method": {"type": "string"},
                    "path": {"type": "string"},
                    "request_schema": {"type": "object"},
                    "response_schema": {"type": "object"},
                    "description": {"type": "string"},
                },
                "required": ["method", "path", "description"],
            },
        },
        "env_vars": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["test_file_name", "test_file_content", "public_api_summary"],
}


class RepoMap:
    @staticmethod
    def build(export_dir: str, language: str, max_tokens: int = 2000) -> str:
        root = Path(export_dir)
        if not root.exists() or not root.is_dir():
            return ""

        language_norm = str(language or "python").strip().lower()

        skip_dirs = {"node_modules", "dist", "__pycache__", ".git", ".venv", "venv", ".pytest_cache"}
        skip_names = {"package.json", "tsconfig.json"}
        skip_suffixes = (".toml", ".cfg")

        entries: List[tuple[float, str, str]] = []

        for current_root, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            current_path = Path(current_root)

            for file_name in files:
                file_path = current_path / file_name
                rel_path = file_path.relative_to(root).as_posix()
                lowered_rel = rel_path.lower()
                lowered_name = file_name.lower()

                if lowered_name in skip_names:
                    continue
                if lowered_name.endswith(skip_suffixes):
                    continue
                if "/tests/" in f"/{lowered_rel}" or lowered_name.startswith("test_") or lowered_name.endswith("_test.py"):
                    continue
                if ".test." in lowered_name or ".spec." in lowered_name:
                    continue

                if language_norm in {"typescript", "javascript"}:
                    if not lowered_name.endswith((".ts", ".tsx", ".js", ".jsx")):
                        continue
                    summary_lines = RepoMap._extract_ts_exports(file_path)
                else:
                    if not lowered_name.endswith(".py"):
                        continue
                    summary_lines = RepoMap._extract_python_signatures(file_path)

                if not summary_lines:
                    continue

                capped = summary_lines[:40]
                section = "## " + rel_path + "\n" + "\n".join(capped)
                try:
                    mtime = file_path.stat().st_mtime
                except Exception:
                    mtime = 0.0
                entries.append((mtime, rel_path, section))

        if not entries:
            return ""

        entries.sort(key=lambda item: item[0])
        sections = [item[2] for item in entries]

        header = "# Repo Map — files already written"
        max_chars = max(200, int(max_tokens) * 4)

        while sections:
            combined = header + "\n" + "\n".join(sections)
            if len(combined) <= max_chars:
                return combined
            sections.pop(0)

        return ""

    @staticmethod
    def _extract_ts_exports(file_path: Path) -> List[str]:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        lines: List[str] = []
        pattern = re.compile(r"^\s*export\s+(?:interface|type|class|function|const)\s+.+", flags=re.MULTILINE)
        for match in pattern.findall(content):
            line = match.strip()
            line = re.sub(r"\s*\{\s*$", "", line).strip()
            if line.endswith("{"):
                line = line[:-1].rstrip()
            lines.append(line)
        return lines

    @staticmethod
    def _extract_python_signatures(file_path: Path) -> List[str]:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        lines: List[str] = []
        pattern = re.compile(r"^\s*(?:class\s+[A-Za-z_][A-Za-z0-9_]*\s*(?:\([^)]*\))?|def\s+[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\))\s*:", flags=re.MULTILINE)
        for match in pattern.findall(content):
            lines.append(match.strip())
        return lines


def _strip_markdown_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    fenced = re.search(r"```(?:[a-zA-Z0-9_+\-]+)?\s*\n([\s\S]*?)\n```", cleaned)
    if fenced:
        return fenced.group(1).strip()

    cleaned = re.sub(r"^\s*```(?:[a-zA-Z0-9_+\-]+)?\s*", "", cleaned, count=1)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned, count=1)
    cleaned = re.sub(r"(?m)^\s*```(?:[a-zA-Z0-9_+\-]+)?\s*$", "", cleaned)
    return cleaned.strip()


@dataclass
class ContractArtifact:
    test_file_name: str
    test_file_content: str
    public_api_summary: str
    critic_notes: str = ""
    # ADR-042: Machine-readable route manifest from Phase 0.
    # List of {method, path, request_schema, response_schema, description} dicts.
    # Empty list means non-web project (CLI tool, library, etc.) — not an error.
    routes: list = field(default_factory=list)
    # ADR-042: Required environment variable names (API key names, config vars).
    # Injected into subtask prompts so agents know exactly what os.environ keys to read.
    env_vars: list = field(default_factory=list)


class SwarmMemory:
    """Shared in-memory registry of module manifests across swarm tiers (ADR-020 V1)."""

    def __init__(self):
        self.rules: Dict[str, Any] = {}
        self._manifests: Dict[str, Dict[str, Any]] = {}
        self._frozen_contract: Optional[ContractArtifact] = None

    def seed(self, rules: dict) -> None:
        self.rules = dict(rules or {}) if isinstance(rules, dict) else {}

    def publish(self, module_name: str, manifest: dict) -> None:
        if not module_name:
            return
        if not isinstance(manifest, dict):
            return

        if self.is_contract_frozen() and str(module_name).strip().lower() in {"test_file", "public_api", "contract"}:
            logger.warning("SwarmMemory publish blocked for frozen contract key: %s", module_name)
            return

        exports = manifest.get("exports")
        if not isinstance(exports, list):
            exports = []
        exports = [str(item) for item in exports if str(item).strip()]

        file_path = str(manifest.get("file_path") or "")
        raw_tier = manifest.get("tier", 0)
        try:
            tier = int(raw_tier)
        except (TypeError, ValueError):
            tier = 0

        self._manifests[module_name] = {
            "exports": exports,
            "file_path": file_path,
            "tier": tier,
        }

    def freeze_contract(self, contract: ContractArtifact) -> None:
        self._frozen_contract = contract

    def get_contract(self) -> Optional[ContractArtifact]:
        return self._frozen_contract

    def is_contract_frozen(self) -> bool:
        return self._frozen_contract is not None

    def get_block_for_subtask(self, block_id: str) -> Optional[ContractArtifact]:
        # Stub: always return full contract. Full block partitioning is ADR-028.
        return self._frozen_contract

    def read(self, module_name: str) -> dict:
        value = self._manifests.get(module_name)
        return dict(value) if isinstance(value, dict) else {}

    def context_for_tier(self, tier: int) -> str:
        try:
            current_tier = int(tier)
        except (TypeError, ValueError):
            return ""

        lines: List[str] = []
        for module_name, manifest in sorted(
            self._manifests.items(),
            key=lambda item: (int(item[1].get("tier", 0)), item[0]),
        ):
            manifest_tier = int(manifest.get("tier", 0))
            if manifest_tier >= current_tier:
                continue

            exports = manifest.get("exports") or []
            file_path = manifest.get("file_path") or ""
            exports_text = ", ".join(str(item) for item in exports)
            lines.append(
                f"- {module_name} (tier {manifest_tier}): exports [{exports_text}] at {file_path}"
            )

        if not lines:
            return ""

        return "## Available modules from previous tiers\n" + "\n".join(lines)


class AgentType(Enum):
    CLAUDE   = "claude"
    GEMINI   = "gemini"
    LMSTUDIO = "lmstudio"
    OLLAMA   = "ollama"


class AgentConfig:
    """Describes a single registered agent."""

    def __init__(
        self,
        id: str,
        name: str,
        agent_type: AgentType,
        endpoint: str,
        model_id: str,
        capabilities: List[str],
    ):
        self.id           = id
        self.name         = name
        self.agent_type   = agent_type
        self.endpoint     = endpoint
        self.model_id     = model_id
        self.capabilities = capabilities
        self.status       = "idle"
        self.registered_at = time.time()

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "name":          self.name,
            "type":          self.agent_type.value,
            "endpoint":      self.endpoint,
            "model_id":      self.model_id,
            "capabilities":  self.capabilities,
            "status":        self.status,
            "registered_at": self.registered_at,
        }


class TaskRecord:
    """Tracks a dispatched task."""

    def __init__(self, task_id: str, agent_id: str, description: str):
        self.id          = task_id
        self.agent_id    = agent_id
        self.description = description
        self.status      = "queued"
        self.output      = ""
        self.created_at  = time.time()
        self.finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "agent_id":    self.agent_id,
            "description": self.description,
            "status":      self.status,
            "output":      self.output,
            "created_at":  self.created_at,
            "finished_at": self.finished_at,
        }


class TaskSpec:
    """Payload submitted to /api/devhub/dispatch."""

    def __init__(
        self,
        title: str = "",
        description: str = "",
        system_prompt: str = "",
        context_files: List[str] = None,
        target_files: List[str] = None,
        constraints: str = "",
        context: dict = None,
    ):
        self.title        = title
        self.description  = description
        self.system_prompt = system_prompt
        self.context_files = context_files or []
        self.target_files  = target_files or []
        self.constraints   = constraints
        self.context       = context or {}

    @property
    def task(self) -> str:
        """Human-readable task description used by the pool."""
        parts = []
        if self.title:
            parts.append(self.title)
        if self.description:
            parts.append(self.description)
        if self.constraints:
            parts.append(f"Constraints: {self.constraints}")
        return "\n".join(parts) if parts else "Build project"


class AgentPool:
    """
    In-memory registry of agents + task tracking.

    Agents are registered at runtime via the API or auto-discovered
    from Settings on startup.
    """

    _GEAR_SHIFT_MAP = [
        (frozenset({"thinking-only", "http 5", "http 404", "404", "not found", "timeout", "connection", "quota"}), ["openai-codex", "claude-haiku", "claude"]),
        (frozenset({"truncat", "no content", "json", "parse", "empty"}), "gemini"),
        (frozenset({"syntaxerror", "importerror", "nameerror", "syntax", "invalid code"}), "claude"),
    ]

    _TIER_LEVEL_MAP = {
        "lite": 1,
        "standard": 1,
        "brain": 2,
        "heavy": 3,
        "escalate": 3,
    }

    def __init__(self, ai_backend=None, settings=None, sse_broadcast=None):
        self._agents: Dict[str, AgentConfig] = {}
        self._tasks:  Dict[str, TaskRecord]  = {}
        self.ai            = ai_backend
        self.settings      = settings
        self._sse_broadcast = sse_broadcast  # async callable(event_type, data)

    # ── Registration ──────────────────────────────────────────────────────────

    def register_agent(self, config: AgentConfig) -> None:
        self._agents[config.id] = config
        logger.info(f"Agent registered: {config.name} ({config.id}) [{config.agent_type.value}]")

    def remove_agent(self, agent_id: str) -> bool:
        if agent_id in self._agents:
            del self._agents[agent_id]
            logger.info(f"Agent removed: {agent_id}")
            return True
        return False

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_agents(self) -> List[dict]:
        return [a.to_dict() for a in self._agents.values()]

    def get_agent(self, agent_id: str) -> Optional[AgentConfig]:
        return self._agents.get(agent_id)

    def get_pool_summary(self) -> dict:
        agents = self.get_agents()
        return {
            "total": len(agents),
            "idle":  sum(1 for a in agents if a["status"] == "idle"),
            "busy":  sum(1 for a in agents if a["status"] == "busy"),
            "agents": agents,
        }

    async def refresh_agent_status(self) -> None:
        """Ping each agent endpoint to check liveness (best-effort)."""
        import aiohttp
        for agent in self._agents.values():
            if not agent.endpoint:
                continue
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(agent.endpoint + "/health", timeout=aiohttp.ClientTimeout(total=2)) as r:
                        agent.status = "idle" if r.status < 400 else "error"
            except Exception:
                agent.status = "unreachable"

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def dispatch_task(self, task: Any, agent_id: str) -> TaskRecord:
        """
        Dispatch a task to a specific agent.
        `task` may be a TaskSpec object or a plain dict/string.
        """
        agent = self._agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent '{agent_id}' not found in pool")

        description = getattr(task, "task", None) or (
            task if isinstance(task, str) else str(task)
        )

        task_id = f"task-{int(time.time() * 1000)}"
        record  = TaskRecord(task_id, agent_id, description)
        self._tasks[task_id] = record

        agent.status = "busy"

        # Run in background so the HTTP response can return immediately
        async def _safe_run():
            try:
                await self._run_task(record, agent, task)
            except Exception as exc:
                logger.error(f"Unhandled error in _run_task background task: {exc}", exc_info=True)

        asyncio.create_task(_safe_run())

        return record

    async def _broadcast(self, event_type: str, data: dict) -> None:
        """Fire-and-forget SSE broadcast (swallows errors)."""
        if self._sse_broadcast is None:
            return
        try:
            await self._sse_broadcast(event_type, data)
        except Exception as exc:
            logger.debug(f"SSE broadcast failed: {exc}")

    def _select_backend_for_tier(self, tier: str) -> str:
        """Return the primary AI backend for a complexity tier (ADR-034/035/036).

        ADR-036 maps tier names to role chains so swarm workers share the same
        benchmark-driven defaults as all other roles in the system.

        Tier → Role chain mapping:
            t1 / lite / simple / trivial / low  → ROLE_DEV_SWARM_T1_CHAIN
            t2 / standard / elevated / high     → ROLE_DEV_SWARM_T2_CHAIN
            t3 / brain / heavy                  → ROLE_DEV_SWARM_T3_CHAIN
            escalate                            → ROLE_DEV_SWARM_T3_CHAIN

        When settings=None (unit tests without settings), falls back to the
        legacy openai-codex / claude hardcoded defaults so pre-ADR-036 tests pass.
        """
        s = self.settings

        def _cfg(attr: str, fallback: str) -> str:
            return (getattr(s, attr, None) or fallback) if s is not None else fallback

        def _role_primary(role_chain_attr: str, legacy_fallback: str) -> str:
            """Read first element of a role chain setting, or legacy fallback.

            Uses isinstance check so MagicMock settings in tests (which return
            Mock objects rather than None for unknown attributes) fall through
            to the legacy_fallback correctly.
            """
            if s is None:
                return legacy_fallback
            raw = getattr(s, role_chain_attr, None)
            if not isinstance(raw, str) or not raw.strip():
                return legacy_fallback
            return raw.split(",")[0].strip()

        normalized_tier = (tier or "").strip().lower()

        if normalized_tier in {"t1", "lite", "simple", "trivial", "low"}:
            return _role_primary("ROLE_DEV_SWARM_T1_CHAIN",
                                 _cfg("SWARM_BACKEND_LITE", "openai-codex"))

        if normalized_tier in {"t2", "standard", "elevated", "high"}:
            return _role_primary("ROLE_DEV_SWARM_T2_CHAIN",
                                 _cfg("SWARM_BACKEND_STANDARD", "openai-codex"))

        if normalized_tier in {"t3", "brain", "heavy"}:
            return _role_primary("ROLE_DEV_SWARM_T3_CHAIN",
                                 _cfg("SWARM_BACKEND_BRAIN", "claude"))

        if normalized_tier == "escalate":
            logger.warning("Task escalated to 'escalate' tier — human confirmation would be needed")
            return _role_primary("ROLE_DEV_SWARM_T3_CHAIN",
                                 _cfg("SWARM_BACKEND_HEAVY", "claude"))

        # Unknown tier → T3 quality (safest choice)
        return _role_primary("ROLE_DEV_SWARM_T3_CHAIN",
                             _cfg("SWARM_BACKEND_BRAIN", "claude"))

    def _classify_error_gear(self, exc: Exception) -> Optional[str]:
        """Classify an execution error and return an escalation backend, if any."""
        chain = self._classify_error_gear_chain(exc)
        return chain[0] if chain else None

    def _classify_error_gear_chain(self, exc: Exception) -> List[str]:
        """Classify an execution error and return ordered escalation backends."""
        message = str(exc).lower()
        for signals, gear in self._GEAR_SHIFT_MAP:
            if any(signal in message for signal in signals):
                if isinstance(gear, (list, tuple)):
                    return [str(item) for item in gear if str(item).strip()]
                if isinstance(gear, str) and gear.strip():
                    return [gear]
        return []

    def _collect_source_tree(self, export_dir: Path) -> Dict[str, str]:
        """Read all files in export_dir and return {relative_path: content}."""
        tree: Dict[str, str] = {}
        skip_dirs = {
            ".git",
            "__pycache__",
            "node_modules",
            ".venv",
            "venv",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tox",
            "dist",
            "build",
            ".next",
            "coverage",
        }
        root = Path(export_dir)

        for current_root, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip_dirs]

            current_path = Path(current_root)
            for file_name in files:
                file_path = current_path / file_name
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    rel = file_path.relative_to(root).as_posix()
                    tree[rel] = content
                except Exception as exc:
                    logger.debug(f"Source tree skipped unreadable file {file_path}: {exc}")
        return tree

    def _tier_level_for_subtask(self, subtask: OrchestratorSubTask) -> int:
        if not subtask or not getattr(subtask, "judge_result", None):
            return 1

        tier_name = str(getattr(subtask.judge_result, "tier", "") or "").strip().lower()
        return self._TIER_LEVEL_MAP.get(tier_name, 1)

    def _extract_exports_from_source(self, source: str) -> List[str]:
        exports: List[str] = []
        seen = set()

        patterns = [
            re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", flags=re.MULTILINE),
            re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b", flags=re.MULTILINE),
            re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", flags=re.MULTILINE),
        ]

        for pattern in patterns:
            for match in pattern.findall(source or ""):
                name = str(match).strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                exports.append(name)

        return exports

    def _publish_subtask_manifests(
        self,
        memory: SwarmMemory,
        subtask: OrchestratorSubTask,
        execution_result: dict,
        export_dir: Path,
        tier_level: int,
    ) -> None:
        if not isinstance(execution_result, dict):
            return

        written_files = execution_result.get("files")
        if not isinstance(written_files, list):
            return

        export_path = Path(export_dir)
        for written in written_files:
            file_path = Path(str(written))
            if not file_path.is_absolute():
                file_path = export_path / file_path

            if not file_path.exists() or not file_path.is_file():
                continue

            try:
                relative_file_path = file_path.resolve().relative_to(export_path.resolve()).as_posix()
            except Exception:
                relative_file_path = file_path.name

            module_name = Path(relative_file_path).stem or Path(relative_file_path).name
            module_name = re.sub(r"[^A-Za-z0-9_]+", "_", module_name).strip("_") or "module"

            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                source = ""

            manifest = {
                "exports": self._extract_exports_from_source(source),
                "file_path": relative_file_path,
                "tier": tier_level,
            }
            memory.publish(module_name=module_name, manifest=manifest)

    def _orchestrator_scaffolding_rules(self, project_context: dict) -> Dict[str, Any]:
        if not isinstance(project_context, dict):
            return {}

        explicit = project_context.get("scaffolding_rules")
        if isinstance(explicit, dict):
            return explicit

        rules: Dict[str, Any] = {}
        for key in ("constraints", "naming", "naming_constraints", "conventions", "tech_preferences", "goals"):
            value = project_context.get(key)
            if value:
                rules[key] = value
        return rules

    def _with_frozen_contract_instruction(self, project_context: dict) -> Dict[str, Any]:
        context = dict(project_context or {})
        instruction = (
            "IMPORTANT: A test file has already been generated and written to the tests/ directory as the "
            "frozen contract for this project. Do NOT include any subtask that writes or modifies test files. "
            "All implementation subtasks must build source code that satisfies the existing tests."
        )

        constraints = context.get("constraints")
        if isinstance(constraints, list):
            if instruction not in [str(item) for item in constraints]:
                context["constraints"] = constraints + [instruction]
            return context

        if isinstance(constraints, str) and constraints.strip():
            if instruction not in constraints:
                context["constraints"] = [constraints, instruction]
            else:
                context["constraints"] = [constraints]
            return context

        context["constraints"] = [instruction]
        return context

    def _project_seed_text(self, project_context: dict) -> str:
        context = project_context if isinstance(project_context, dict) else {}
        parts: List[str] = []

        for key in ("project_name", "name", "title", "description", "summary"):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())

        goals = context.get("goals")
        if isinstance(goals, list):
            parts.extend(str(item).strip() for item in goals if str(item).strip())

        constraints = context.get("constraints")
        if isinstance(constraints, list):
            parts.extend(str(item).strip() for item in constraints if str(item).strip())
        elif isinstance(constraints, str) and constraints.strip():
            parts.append(constraints.strip())

        return "\n".join(parts)

    def _detect_target_language(self, seed: str) -> str:
        text = str(seed or "")
        checks = [
            ("TypeScript", (r"\btypescript\b", r"\bts\b")),
            ("JavaScript", (r"\bjavascript\b", r"\bjs\b", r"\bnode(?:\.js)?\b")),
            ("Rust", (r"\brust\b",)),
            ("Go", (r"\bgolang\b", r"\bgo\b")),
            ("Java", (r"\bjava\b",)),
            ("C#", (r"\bc#\b", r"\bcsharp\b", r"\bdotnet\b", r"\.net\b")),
            ("Ruby", (r"\bruby\b",)),
            ("Swift", (r"\bswift\b",)),
            ("Kotlin", (r"\bkotlin\b",)),
            ("C++", (r"\bc\+\+\b", r"\bcpp\b")),
            ("Python", (r"\bpython\b", r"\bpy\b")),
        ]

        for canonical, patterns in checks:
            if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
                return canonical
        return "Python"

    def _contract_language_context(self, target_language: str, project_context: dict) -> Dict[str, str]:
        language = str(target_language or "Python")
        context = project_context if isinstance(project_context, dict) else {}
        project_name = str(context.get("project_name") or context.get("name") or "project").strip()
        slug = re.sub(r"[^a-z0-9]+", "_", project_name.lower()).strip("_") or "project"

        defaults = {
            "test_file_ext": "_test.py",
            "test_file_example_name": f"{slug}_test.py",
            "test_framework": "pytest",
            "import_rule": (
                "All imports in test_file_content must use the fully-qualified package path. "
                "Never use bare imports like `from units import` — always use "
                "`from {package_name}.units import` or `import {package_name}.converter`."
            ),
            "float_rule": (
                "- RULE 12 — FLOAT ASSERTIONS: Any test assertion comparing a float result MUST use "
                "`pytest.approx()` with an explicit `abs` or `rel` tolerance. Never use bare `==` for "
                "float comparisons. This includes: unit conversions (length, weight, volume, speed), "
                "temperature conversions (Kelvin↔Celsius offsets produce IEEE 754 rounding errors), "
                "currency, and any arithmetic result. "
                "Use `abs=1e-4` for unit conversions and `abs=1e-2` for temperatures. "
                "Example: `assert convert(300, 'kelvin', 'celsius') == pytest.approx(26.85, abs=1e-2)`. "
                "If a test imports `convert`, it must also import `pytest`."
            ),
        }

        if language in {"TypeScript", "JavaScript"}:
            defaults.update({
                "test_file_ext": ".spec.ts",
                "test_file_example_name": f"test_{slug}.spec.ts",
                "test_framework": "Vitest",
                "import_rule": (
                    "All imports must use relative paths or package name (e.g. "
                    "`import { CellRegistry } from '../src/registry'`). Never use Python-style imports."
                ),
                "float_rule": (
                    "- RULE 12 — FLOAT ASSERTIONS: Any floating-point comparison must use Vitest/Jest closeness "
                    "assertions (e.g. `expect(value).toBeCloseTo(expected, 4)`) instead of strict equality."
                ),
            })
        elif language == "Go":
            defaults.update({
                "test_file_ext": "_test.go",
                "test_file_example_name": f"{slug}_test.go",
                "test_framework": "go test",
                "import_rule": "Use valid Go import paths. Never use Python-style imports.",
                "float_rule": (
                    "- RULE 12 — FLOAT ASSERTIONS: Use tolerant float comparisons (epsilon checks) in Go tests; "
                    "never rely on strict equality for float arithmetic."
                ),
            })
        elif language == "Rust":
            defaults.update({
                "test_file_ext": "_test.rs",
                "test_file_example_name": f"{slug}_test.rs",
                "test_framework": "cargo test",
                "import_rule": "Use Rust crate/module paths (`crate::`, `super::`) as appropriate. Never use Python-style imports.",
                "float_rule": (
                    "- RULE 12 — FLOAT ASSERTIONS: Use tolerant float comparisons in Rust tests (difference/epsilon checks), "
                    "not exact equality."
                ),
            })
        elif language == "Java":
            defaults.update({
                "test_file_ext": ".java",
                "test_file_example_name": f"{slug}Test.java",
                "test_framework": "JUnit 5",
                "import_rule": "Use valid Java package imports and class references. Never use Python-style imports.",
                "float_rule": (
                    "- RULE 12 — FLOAT ASSERTIONS: Use JUnit tolerant assertions for float/double values with explicit delta "
                    "(e.g. `assertEquals(expected, actual, 1e-4)`)."
                ),
            })

        return defaults

    def _is_valid_contract_test_file_name(self, target_language: str, test_file_name: str) -> bool:
        file_name = str(test_file_name or "").strip()
        language = str(target_language or "Python")
        if not file_name:
            return False

        lowered = file_name.lower()
        if language in {"TypeScript", "JavaScript"}:
            return lowered.endswith(".spec.ts") or lowered.endswith(".test.ts")
        if language == "Go":
            return lowered.endswith("_test.go")
        if language == "Rust":
            return lowered.endswith("_test.rs")
        if language == "Java":
            return lowered.endswith(".java")
        return lowered.endswith(".py") and (lowered.startswith("test_") or lowered.endswith("_test.py"))

    def _estimate_contract_token_budget(self, seed: str) -> int:
        seed_text = str(seed or "")
        lowered = seed_text.lower()
        tier = 2

        high_overhead_languages = {"TypeScript", "JavaScript", "Rust", "Go", "Java", "C#"}
        detected_language = self._detect_target_language(seed_text)
        if detected_language in high_overhead_languages:
            tier += 1
        if detected_language in {"TypeScript", "JavaScript"}:
            tier += 1

        named_entities = set()
        for match in re.findall(
            r"\b(?:module|modules|component|components|class|classes|subsystem|subsystems)\s+(?:named\s+)?([A-Za-z][A-Za-z0-9_-]*)",
            seed_text,
            flags=re.IGNORECASE,
        ):
            named_entities.add(match.lower())

        for list_block in re.findall(
            r"\b(?:modules|components|classes|subsystems)\s*:\s*([^\n.;]+)",
            seed_text,
            flags=re.IGNORECASE,
        ):
            for item in re.split(r",|/|\band\b", list_block):
                token = re.sub(r"[^A-Za-z0-9_-]", "", item.strip()).lower()
                if token:
                    named_entities.add(token)

        for phrase in re.findall(
            r"\b(?:a|an)\s+([a-z][a-z0-9_-]{2,}(?:\s+[a-z][a-z0-9_-]{2,})?)",
            lowered,
            flags=re.IGNORECASE,
        ):
            clean_phrase = re.sub(r"[^a-z0-9_\s-]", "", phrase).strip()
            if not clean_phrase:
                continue
            parts = [part for part in re.split(r"\s+", clean_phrase) if part]
            if parts:
                named_entities.add(parts[-1])

        stop_words = {
            "and", "the", "for", "with", "that", "this", "from", "into", "over", "under",
            "suite", "tests", "test", "module", "modules", "component", "components", "class", "classes",
            "subsystem", "subsystems", "full", "complete", "comprehensive", "simple", "basic", "minimal",
            "project", "platform", "framework", "api", "service", "system", "library",
        }
        for list_block in re.findall(
            r"\b(?:define|include|implement|provide|build|create|with)\s+([^\n.;]+)",
            lowered,
            flags=re.IGNORECASE,
        ):
            for item in re.split(r",|/|\band\b", list_block):
                for token in re.findall(r"[a-z][a-z0-9_-]{2,}", item):
                    if token not in stop_words:
                        named_entities.add(token)

        if len(named_entities) >= 4:
            tier += 1

        breadth_terms = (
            "full suite",
            "complete",
            "comprehensive",
            "end-to-end",
            "platform",
            "framework",
            "with tests",
            "test suite",
            "vitest",
            "jest",
            "pytest",
            "coverage",
        )
        if any(term in lowered for term in breadth_terms):
            tier += 1

        architecture_terms = (
            "rest api",
            "database",
            "authentication",
            "microservice",
            "cli",
            "plugin system",
            "event-driven",
        )
        architecture_hits = sum(1 for term in architecture_terms if term in lowered)
        if architecture_hits > 0:
            tier += 1
        if architecture_hits >= 3:
            tier += 1

        downsizing_terms = (
            "simple",
            "basic",
            "minimal",
            "single file",
            "tiny",
            "helper",
        )
        if any(term in lowered for term in downsizing_terms):
            tier -= 1

        tier = max(1, min(8, tier))

        # Session 57: raised T3-T8 — a full pytest file + JSON wrapper easily hits 6k+
        # tokens for medium projects; 8192 is a safer floor for the T3 (normal Python) tier.
        defaults = {
            1: 2048,
            2: 4096,
            3: 8192,   # was 6144 — "Battery Spec Scanner Hub" tier; 6144 caused truncation
            4: 12288,  # was 8192
            5: 16384,  # was 12288
            6: 24576,  # was 16384
            7: 32768,  # was 24576
            8: 65536,  # was 32768
        }
        setting_attrs = {
            1: "CONTRACT_TOKEN_BUDGET_T1",
            2: "CONTRACT_TOKEN_BUDGET_T2",
            3: "CONTRACT_TOKEN_BUDGET_T3",
            4: "CONTRACT_TOKEN_BUDGET_T4",
            5: "CONTRACT_TOKEN_BUDGET_T5",
            6: "CONTRACT_TOKEN_BUDGET_T6",
            7: "CONTRACT_TOKEN_BUDGET_T7",
            8: "CONTRACT_TOKEN_BUDGET_T8",
        }

        if self.settings is not None:
            attr = setting_attrs[tier]
            try:
                return int(getattr(self.settings, attr))
            except Exception:
                pass
        return defaults[tier]

    async def _generate_contract(self, project_context: dict) -> ContractArtifact:
        if self.ai is None:
            raise RuntimeError("AI backend not available for contract generation")

        # Solution 3: contract_override — if the user supplies a pre-built test file
        # and API summary, skip Phase 0 entirely.  This eliminates contract variability
        # across builds and lets power users pin their own reference test.
        # Expected shape: project_context["contract_override"] = {
        #   "test_file_name": "battery_datasheet_scanner_test.py",
        #   "test_file_content": "import pytest\n...",
        #   "public_api_summary": "...",
        #   "routes": [...],       # optional
        #   "env_vars": [...]      # optional
        # }
        _override = project_context.get("contract_override")
        if _override and isinstance(_override, dict):
            _tfn = _override.get("test_file_name", "test_override.py")
            _tfc = _override.get("test_file_content", "")
            _api = _override.get("public_api_summary", "")
            if _tfc.strip():
                logger.info(
                    "Phase 0: using contract_override (test=%s, %d chars API summary)",
                    _tfn, len(_api),
                )
                return ContractArtifact(
                    test_file_name=_tfn,
                    test_file_content=_tfc,
                    public_api_summary=_api,
                    routes=_override.get("routes", []),
                    env_vars=_override.get("env_vars", []),
                )

        seed_text = self._project_seed_text(project_context)
        target_language = self._detect_target_language(seed_text)
        contract_token_budget = self._estimate_contract_token_budget(seed_text)

        logger.info(
            "Phase 0 contract planning: target_language=%s, token_budget=%s",
            target_language,
            contract_token_budget,
        )

        async def _generate_initial_contract(revision: int, extra_feedback: str = "") -> ContractArtifact:
            context_payload = json.dumps(project_context or {}, ensure_ascii=False, indent=2)
            lang_ctx = self._contract_language_context(target_language, project_context)
            base_system_prompt = (
                "You are a contract generator for an automated software build pipeline. "
                "Return strictly valid JSON only — no markdown fences, no prose, no filler. "
                "The JSON you produce is the authoritative frozen contract that all downstream agents must implement exactly. "
                "Be precise: test assertions must be deterministic, field names must be canonical, types must be explicit."
            )
            if int(revision) == 0:
                system_prompt = f"Target language: {target_language}\n{base_system_prompt}"
            else:
                system_prompt = base_system_prompt
            user_prompt = (
                "Generate a complete, runnable test file that will be the authoritative contract for this project. "
                "Implementation agents will be required to pass these tests. Also produce a public API summary "
                "listing every public class name, method signature, and exception type.\n\n"
                f"TARGET_LANGUAGE: {target_language}\n"
                f"TARGET_TEST_FRAMEWORK: {lang_ctx['test_framework']}\n\n"
                "PROJECT_CONTEXT:\n"
                f"{context_payload}\n\n"
                "Return JSON in this exact shape:\n"
                "{\n"
                f"  \"test_file_name\": \"{lang_ctx['test_file_example_name']}\",\n"
                f"  \"test_file_content\": \"<complete {lang_ctx['test_framework']} test file content>\",\n"
                "  \"public_api_summary\": \"<plain text API contract>\",\n"
                "  \"routes\": [\n"
                "    {\n"
                "      \"method\": \"POST\",\n"
                "      \"path\": \"/api/example\",\n"
                "      \"request_schema\": {\"type\": \"object\", \"properties\": {\"input\": {\"type\": \"string\"}}, \"required\": [\"input\"]},\n"
                "      \"response_schema\": {\"type\": \"object\", \"properties\": {\"result\": {\"type\": \"string\"}}},\n"
                "      \"description\": \"One-line description of what this route does\"\n"
                "    }\n"
                "  ],\n"
                "  \"env_vars\": [\"GOOGLE_API_KEY\", \"OTHER_KEY\"]\n"
                "}\n"
                "Rules:\n"
                f"- test_file_name must be a single {lang_ctx['test_framework']} test filename appropriate for {target_language} and end with `{lang_ctx['test_file_ext']}`.\n"
                f"- test_file_content must be complete runnable {lang_ctx['test_framework']} test content.\n"
                "- public_api_summary must be plain text only.\n"
                "- ROUTE MANIFEST (routes field): For web apps (FastAPI, Flask, Starlette, Django), "
                "populate `routes` with one entry per HTTP endpoint. Each entry MUST have: "
                "method (GET/POST/PUT/DELETE/PATCH), path (exact URL path string, e.g. '/api/scan'), "
                "description (one sentence), and SHOULD have request_schema and response_schema as JSON Schema objects. "
                "For non-web projects (CLI tools, libraries, pure Python packages), set `routes` to an empty array [].\n"
                "- ENV VARS (env_vars field): List every environment variable name the app reads at runtime "
                "(e.g. API keys, database URLs). These are the os.environ keys the implementation will call. "
                "Example: [\"GOOGLE_API_KEY\", \"DATABASE_URL\"]. If no env vars are needed, set to [].\n"
                "- For any module-level data structure (e.g. UNITS, CONFIG, REGISTRY), "
                "public_api_summary must include its exact type annotation and a one-line "
                "schema example, e.g.: "
                "UNITS: dict[str, list[dict]] — "
                "{'length': [{'name': 'meter', 'symbol': 'm', 'factor': 1.0}]}\n"
                f"- {lang_ctx['import_rule']}\n"
                "- CRITICAL: Ensure all test assertions for formatting and naming are standardized and explicit. Do "
                "not mix singular and plural keys (e.g. if testing 'kilometer', use 'kilometer' consistently, not "
                "'kilometers'). For symbols, use plain ASCII if possible (e.g. 'C' instead of '°C'). Explicitly "
                "document any required spelling, symbol, or formatting conventions in the public_api_summary so "
                "implementation agents match the test assertions exactly.\n"
                "- Output JSON only, no markdown fences.\n"
                f"{lang_ctx['float_rule']}\n"
                "- WEB APP RULE: If this is a Python web application (FastAPI, Flask, Starlette, Django), "
                "the test file MUST include ALL of the following:\n"
                "  1. An import smoke test: `from <pkg>.main import app` — this catches missing "
                "dependencies (fastapi, uvicorn, etc.) before any other test runs. If this import "
                "fails, every other test fails too. It must be the first test or at the top of the file.\n"
                "  2. At least one TestClient route test:\n"
                "     `from starlette.testclient import TestClient; client = TestClient(app)`\n"
                "     Test at least one real POST/GET endpoint to verify the route exists and returns "
                "an expected status code (e.g. 200 or 422 for missing input).\n"
                "  3. If the project serves a frontend (HTML/CSS/JS in a static/ folder), include: "
                "`assert client.get('/').status_code == 200`.\n"
                "  These three tests exist to catch import/routing/serving failures that pure unit "
                "tests with mocks cannot catch.\n"
                "- CRITICAL — ASCII SYMBOLS IN CONTRACT AND TESTS: All symbols in `test_file_content` and "
                "`public_api_summary` must be ASCII only (0–127). Temperature units must use the plain letter: "
                "'C' for Celsius, 'F' for Fahrenheit, 'K' for Kelvin. Never write '°C', '℃', or any Unicode "
                "degree character anywhere in the contract output. If a unit symbol requires a non-ASCII "
                "character, substitute the closest ASCII equivalent and document it explicitly in "
                "`public_api_summary`.\n"
                "- CRITICAL — MOCK DATA COMPLETENESS: When test assertions mock an API response "
                "(e.g. mock_response.text = '{...}'), the mock JSON MUST include ALL required fields "
                "of the Pydantic model it will be validated against. If the response schema has "
                "`model_name: str` (required), the mock JSON must include `\"model_name\": \"Test Model\"`. "
                "If ANY required field is missing, Pydantic raises ValidationError and the test fails — "
                "this is the #1 cause of unfixable test failures that waste Big Fixer rounds. "
                "Cross-check every mock response against the response model's required fields.\n"
                "- CRITICAL — MOCK PATCH TARGETS: When writing `@patch()` decorators in tests, "
                "ALWAYS patch the function where it is USED (the importing module), NOT where it "
                "is DEFINED. Python's `from X import Y` binds Y into the importing module's "
                "namespace — patching X.Y after that import does NOT affect the already-bound "
                "reference. Example: if `main.py` does `from .extractor import extract_fn`, the "
                "test MUST use `@patch('pkg.main.extract_fn')`, NOT `@patch('pkg.extractor.extract_fn')`. "
                "If `main.py` instead does `from . import extractor` and calls `extractor.extract_fn()`, "
                "THEN `@patch('pkg.extractor.extract_fn')` is correct. Check each patch target against "
                "the actual import statement in the module being tested.\n"
                "- CRITICAL — NO MODULE-LEVEL SIDE EFFECTS: Do NOT call initialization functions "
                "at module scope in the main app file. This means: NO `load_keys_into_env()` at the "
                "top of main.py, NO `init_db()` at module scope, NO `connect_database()` at import time. "
                "Module-level code executes at import time — BEFORE test fixtures can set up mocks. "
                "WRONG: `from canopy_keys import load_keys_into_env\\nload_keys_into_env()\\napp = FastAPI()` "
                "RIGHT: `app = FastAPI()\\n@app.on_event('startup')\\nasync def startup():\\n    load_keys_into_env()\\n    init_db(...)` "
                "The ONLY things allowed at module scope are: imports, `app = FastAPI()`, model/schema "
                "definitions, and function definitions. ALL initialization calls go in startup events.\n"
            )

            # Fix 3: inject SDK guidance when project is AI/Gemini-related so the frozen
            # contract carries the correct package name + model strings into every subtask.
            _ai_keywords = {"google", "gemini", "genai", "llm", "ai", "openai", "anthropic",
                            "claude", "gpt", "vertex", "generative"}
            _proj_desc = str(project_context.get("description", "") or project_context.get("name", "")).lower()
            if any(kw in _proj_desc for kw in _ai_keywords):
                user_prompt += f"\n\n{_sdk_ref_contract}"

            if extra_feedback.strip():
                # Include the previous contract so the generator can make TARGETED
                # fixes instead of regenerating blind (which causes whack-a-mole:
                # fixing one issue while introducing another).
                _prev = getattr(self, "_last_contract_json", None)
                if _prev:
                    user_prompt += (
                        "\n\n## YOUR PREVIOUS OUTPUT (Revision that was rejected)\n"
                        "Make minimal, targeted changes to fix ONLY the Critic's "
                        "specific objections below. Do NOT rewrite from scratch — "
                        "preserve everything that was NOT flagged.\n"
                        f"```json\n{_prev}\n```\n"
                    )
                user_prompt += (
                    "\n\n## CRITIC FEEDBACK (fix these specific issues)\n"
                    f"{extra_feedback.strip()}"
                )

            # ADR-039: Walk contract_generator role chain (gemini-flash primary, qwen-122b fallback — Session 51).
            # Both HTTP failures AND payload validation failures advance the chain —
            # so if Qwen returns HTTP 200 but missing required fields, openai-codex is tried next.
            _contract_chain = self.ai.get_role_chain("contract_generator")
            _contract_artifact: Optional[ContractArtifact] = None
            _contract_exc: Optional[Exception] = None

            for _contract_backend in _contract_chain:
                try:
                    logger.debug("Contract Generator: trying backend '%s'", _contract_backend)
                    _raw = await self.ai.complete(
                        backend=_contract_backend,
                        system=system_prompt,
                        message=user_prompt,
                        max_tokens=contract_token_budget,
                        json_mode=True,
                        # Session 57: constrain Gemini to return a JSON object (not array/string).
                        # response_schema is Gemini-only (responseJsonSchema); Claude ignores it.
                        response_schema=CONTRACT_RESPONSE_SCHEMA,
                    )

                    # Parse JSON — handle fenced or embedded JSON from verbose models
                    _payload = None
                    _stripped = (_raw or "").strip()
                    # Strip markdown fences if present
                    if _stripped.startswith("```"):
                        _lines = _stripped.splitlines()
                        if len(_lines) >= 3 and _lines[-1].strip() == "```":
                            _stripped = "\n".join(_lines[1:-1]).strip()
                    try:
                        _payload = json.loads(_stripped)
                    except Exception:
                        _match = re.search(r"\{[\s\S]*\}", _stripped)
                        if _match:
                            try:
                                _payload = json.loads(_match.group(0))
                            except Exception:
                                pass

                    if not isinstance(_payload, dict):
                        raise RuntimeError(
                            f"Contract Generator '{_contract_backend}': response is not a JSON object"
                        )

                    _test_file_name = str(_payload.get("test_file_name") or "").strip()
                    _test_file_content = str(_payload.get("test_file_content") or "").strip()
                    _public_api_summary = str(_payload.get("public_api_summary") or "").strip()

                    if not _test_file_name or not _test_file_content or not _public_api_summary:
                        missing = [
                            k for k, v in {
                                "test_file_name": _test_file_name,
                                "test_file_content": _test_file_content,
                                "public_api_summary": _public_api_summary,
                            }.items()
                            if not v
                        ]
                        raise RuntimeError(
                            f"Contract Generator '{_contract_backend}': response missing required field(s): {missing}"
                        )

                    if not self._is_valid_contract_test_file_name(target_language, _test_file_name):
                        raise RuntimeError(
                            f"Contract Generator '{_contract_backend}': invalid test_file_name '{_test_file_name}'"
                        )

                    # ADR-042: Extract route manifest and env_vars (optional — empty list is valid for non-web projects)
                    _routes = _payload.get("routes")
                    if not isinstance(_routes, list):
                        _routes = []
                    # Validate each route entry — silently drop malformed ones rather than failing the contract
                    _valid_routes = []
                    for _r in _routes:
                        if isinstance(_r, dict) and _r.get("method") and _r.get("path"):
                            _valid_routes.append({
                                "method": str(_r.get("method", "")).upper(),
                                "path": str(_r.get("path", "")),
                                "description": str(_r.get("description", "")),
                                "request_schema": _r.get("request_schema") if isinstance(_r.get("request_schema"), dict) else {},
                                "response_schema": _r.get("response_schema") if isinstance(_r.get("response_schema"), dict) else {},
                            })

                    _env_vars = _payload.get("env_vars")
                    if not isinstance(_env_vars, list):
                        _env_vars = []
                    _env_vars = [str(v) for v in _env_vars if str(v).strip()]

                    # Stash the raw JSON so revision N+1 can see what it produced
                    # (enables targeted fixes instead of blind regeneration).
                    self._last_contract_json = json.dumps(_payload, indent=2, ensure_ascii=False)

                    logger.info(
                        "Contract Generator '%s': extracted %d routes, %d env_vars",
                        _contract_backend, len(_valid_routes), len(_env_vars),
                    )

                    _contract_artifact = ContractArtifact(
                        test_file_name=_test_file_name,
                        test_file_content=_test_file_content,
                        public_api_summary=_public_api_summary,
                        critic_notes="",
                        routes=_valid_routes,
                        env_vars=_env_vars,
                    )
                    _contract_exc = None
                    break  # success — exit chain

                except Exception as _exc:
                    logger.warning(
                        "Contract Generator: backend '%s' failed (%s) — trying next",
                        _contract_backend, _exc,
                    )
                    _contract_exc = _exc

            if _contract_artifact is None:
                raise RuntimeError(
                    f"Contract Generator: all backends failed. Last error: {_contract_exc}"
                )

            return _contract_artifact

        async def _critic_call(contract: ContractArtifact) -> str:
            context_payload = json.dumps(project_context or {}, ensure_ascii=False, indent=2)
            # ADR-042: Include routes and env_vars in critic payload so critic can verify
            # route manifest consistency against test assertions.
            contract_dict: dict = {
                "test_file_name": contract.test_file_name,
                "test_file_content": contract.test_file_content,
                "public_api_summary": contract.public_api_summary,
            }
            if contract.routes:
                contract_dict["routes"] = contract.routes
            if contract.env_vars:
                contract_dict["env_vars"] = contract.env_vars
            contract_payload = json.dumps(contract_dict, ensure_ascii=False, indent=2)
            system_prompt = (
                "You are a Senior QA Engineer. Review this test contract for: "
                "(1) logical consistency — does the test assert what the API summary says it returns? "
                "(2) missing dependencies — does it require libraries not in the project brief? "
                "(3) structural completeness — does it cover the primary core workflows of the brief? "
                "(4) route manifest consistency — if `routes` is present, do the test assertions exercise "
                "the same paths and methods listed there? Flag any path mismatches (e.g. test hits /upload but routes says /api/upload). "
                "Reply with only APPROVED, or a numbered list of specific flaws. Do NOT write code. "
                "Only reject for CRITICAL flaws: incorrect return types, mathematically wrong assertions, "
                "unrunnable test code (syntax errors, broken imports, wrong class names), or mock patches targeting "
                "the wrong module (patch where function is DEFINED instead of where it is USED). "
                "Do NOT reject for:\n"
                "  - Missing coverage of CRUD endpoints (GET/POST/DELETE for data management) when the core "
                "workflow (the primary value-add feature like extraction, analysis, scanning) IS tested.\n"
                "  - Missing coverage of secondary features, edge cases, batch processing, pagination, or UI interactions.\n"
                "  - Minor string literal style differences (capitalisation, punctuation, word choice in error messages).\n"
                "  - Testing database methods directly vs through HTTP routes — both are valid contract patterns.\n"
                "  - Route manifest having more endpoints than the tests cover — the contract tests the CORE workflow, "
                "not every CRUD helper.\n"
                "The Phase 0 contract is a STRUCTURAL contract for the primary workflow shape. It must be passable "
                "by a competent implementation. A contract that tests the core feature + import smoke + basic route "
                "is SUFFICIENT — approve it."
            )
            user_prompt = (
                "PROJECT_CONTEXT:\n"
                f"{context_payload}\n\n"
                "CONTRACT_TO_REVIEW:\n"
                f"{contract_payload}"
            )

            try:
                # Session 58: auditor chain — primary is gemini-customtools (reliable text output,
                # no JSON schema needed). Flash 3.0 HIGH thinking is still the fallback but
                # was hardcoded here; using the chain honours env overrides (e.g. credit outage).
                _critic_chain = self.ai.get_role_chain("auditor")
                _critic_backend = _critic_chain[0] if _critic_chain else "gemini-customtools"
                verdict = await self.ai.complete(
                    backend=_critic_backend,
                    system=system_prompt,
                    message=user_prompt,
                    max_tokens=2048,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Contract Critic: Pro 3.1 service error (%s) — degrading to force-approve",
                    exc,
                )
                return f"CRITIC_UNAVAILABLE: {exc}"
            return str(verdict or "").strip()

        last_contract: Optional[ContractArtifact] = None
        last_verdict = ""
        critic_feedback = ""

        _MAX_CONTRACT_REVISIONS = 5
        for revision in range(_MAX_CONTRACT_REVISIONS):
            logger.info("Phase 0: Generating contract draft (Revision %s)...", revision)
            try:
                last_contract = await _generate_initial_contract(revision, critic_feedback)
            except RuntimeError as gen_exc:
                if last_contract is not None:
                    # Revision 1+ failed to parse — revert to the last valid draft
                    last_contract.critic_notes = (
                        f"Revision parse failed, reverting to Revision 0 draft, "
                        f"which had Critic flaws: {critic_feedback.strip() or '(none recorded)'}"
                    )
                    logger.warning(
                        "Contract Generator: revision %s parse failed (%s) — "
                        "reverting to last valid draft, see critic_notes",
                        revision,
                        gen_exc,
                    )
                    return last_contract
                # Revision 0 failed and we have nothing to salvage — re-raise
                raise
            logger.info("Phase 0: Submitting Revision %s to Review Council (Critic)...", revision)
            last_verdict = await _critic_call(last_contract)
            if last_verdict.startswith("CRITIC_UNAVAILABLE:"):
                svc_error = last_verdict[len("CRITIC_UNAVAILABLE: "):]
                last_contract.critic_notes = (
                    f"Critic unavailable — service error: {svc_error}. "
                    "Proceeding with unreviewed draft."
                )
                logger.warning(
                    "Contract Generator: force-approved due to critic service outage — "
                    "see critic_notes"
                )
                return last_contract
            if last_verdict.strip().upper().startswith("APPROVED"):
                logger.info("Contract Generator: approved after %s revision(s)", revision + 1)
                return last_contract
            logger.info(
                "Phase 0: Critic rejected Revision %s. Flaws: %s",
                revision,
                str(last_verdict)[:100],
            )
            critic_feedback = last_verdict

        if last_contract is None:
            raise RuntimeError("Contract generation failed before critic cap")

        last_contract.critic_notes = f"Force-approved after {_MAX_CONTRACT_REVISIONS} revisions. Issues: {last_verdict}"
        logger.warning("Contract Generator: force-approved at cap — see critic_notes")
        return last_contract

    def _wrap_swarm_memory_context(self, raw_context: str) -> str:
        """Wrap SwarmMemory context with explicit interpretation instructions for the LLM."""
        if not raw_context or not str(raw_context).strip():
            return ""

        lines = [line for line in str(raw_context).splitlines() if line.strip()]
        if lines and lines[0].strip().startswith("## "):
            lines = lines[1:]

        body = "\n".join(lines).strip()
        if not body:
            return ""

        return (
            "## Modules already written by earlier agents\n"
            "The following exports exist in real, already-written files. When your module\n"
            "needs them, import using these exact names. Do not redefine or reimplement them.\n\n"
            f"{body}\n\n"
            "## Your task"
        )

    def _compose_subtask_description_with_memory_context(self, description: str, raw_context: str) -> str:
        """Compose final subtask description with optional wrapped SwarmMemory context."""
        wrapped = self._wrap_swarm_memory_context(raw_context)
        if not wrapped:
            return description or ""
        return f"{wrapped}\n\n{description or ''}".strip()

    def _is_test_file_target(self, target_file: str, proposed_content: str = "") -> bool:
        normalized = str(target_file or "").replace("\\", "/").strip().lower()
        if not normalized:
            return False

        file_name = Path(normalized).name
        if file_name.startswith("test_") and file_name.endswith(".py"):
            return True

        # ADR-026 extension: also block __init__.py writes inside a tests/ directory
        # component (e.g. tests/__init__.py). This prevents swarm agents from
        # overwriting boilerplate into the test package and triggering ImportErrors
        # on pytest collection (Smoke Test 24 Bug 1).
        in_tests_dir = normalized.startswith("tests/") or "/tests/" in f"/{normalized}"
        if in_tests_dir and file_name == "__init__.py":
            logger.warning(
                "Phase 1 dispatch: blocking __init__.py write inside tests/ directory: %s",
                target_file,
            )
            return True

        if file_name == "__init__.py":
            proposed = str(proposed_content or "").lower()
            toml_markers = ("[build-system]", "[project]", "build-backend =")
            if any(marker in proposed for marker in toml_markers):
                logger.warning(
                    "Phase 1 dispatch: blocking TOML contamination write into __init__.py: %s",
                    target_file,
                )
                return True

        return in_tests_dir

    def _frozen_contract_block(self, memory: Optional[SwarmMemory]) -> str:
        if memory is None or not memory.is_contract_frozen():
            return ""

        contract = memory.get_contract()
        if contract is None:
            return ""

        # ADR-042: Build route manifest section if contract has routes.
        # This is the key output of the contract-driven route manifest feature.
        # Injecting structured JSON eliminates agents inferring different route shapes from prose.
        route_manifest_section = ""
        if contract.routes:
            try:
                routes_json = json.dumps(contract.routes, indent=2, ensure_ascii=False)
                route_manifest_section = (
                    "\n\n### Route Contract (machine-readable — MUST match exactly)\n"
                    "These are the ONLY valid HTTP routes for this project. "
                    "Use these exact method strings, path strings, field names, and types.\n"
                    "Do NOT invent new routes or rename existing ones.\n\n"
                    f"```json\n{routes_json}\n```"
                )
            except Exception:
                pass  # non-fatal — degrade gracefully to no route section

        # ADR-042: Build env_vars section if contract declares required environment variables.
        env_vars_section = ""
        if contract.env_vars:
            env_list = ", ".join(f"`{v}`" for v in contract.env_vars)
            env_vars_section = (
                f"\n\n### Required Environment Variables\n"
                f"Read these keys from os.environ (already loaded by `load_keys_into_env()`): {env_list}."
            )

        # Include the actual test file content so the swarm sees exact import
        # paths, mock targets, expected status codes, field names, and assertions.
        # Without this, agents invent their own module names (e.g. ai_service.py
        # instead of extractor.py) because they only see the public_api_summary.
        # Cap at 6000 chars to avoid blowing up the prompt for very large tests.
        test_content_section = ""
        if contract.test_file_content:
            _tc = contract.test_file_content
            if len(_tc) > 6000:
                _tc = _tc[:6000] + "\n# ... (truncated)"
            test_content_section = (
                f"\n\n### Test file source (READ ONLY — this is the code your implementation must pass):\n"
                f"```python\n{_tc}\n```\n"
                "Study the import paths, mock targets, Pydantic model fields, expected HTTP status "
                "codes, and content-type assertions above. Your source modules MUST match these exactly."
            )

        return (
            "## Frozen Test Contract (READ ONLY — do not modify)\n"
            "The following test file is the authoritative contract for this project.\n"
            "Your implementation MUST pass these tests. Do not alter the test file.\n\n"
            "### Public API you must implement:\n"
            f"{contract.public_api_summary}"
            f"{route_manifest_section}"
            f"{env_vars_section}"
            f"{test_content_section}\n\n"
            f"### Test file location: tests/{contract.test_file_name}\n"
            "(Already written to disk. Build your module to satisfy it.)"
        )

    def _compose_subtask_description(
        self,
        description: str,
        raw_context: str,
        memory: Optional[SwarmMemory] = None,
        deps_text: str = "",
    ) -> str:
        parts: List[str] = []

        frozen_contract = self._frozen_contract_block(memory)
        if frozen_contract:
            parts.append(frozen_contract)

        wrapped_memory = self._wrap_swarm_memory_context(raw_context)
        if wrapped_memory:
            parts.append(wrapped_memory)

        if deps_text:
            parts.append(deps_text)

        if description:
            parts.append(description)

        return "\n\n".join(parts).strip()

    async def _naming_triage(
        self,
        swarm_summary,
        export_dir: Path,
        project_context: dict,
    ) -> List[OrchestratorSubTask]:
        """
        ADR-016 round-0 fast triage for naming/import failures.

        Builds a constrained repair subtask that only touches files implicated by
        import/module/symbol naming failures before entering the full repair loop.
        """
        if not hasattr(swarm_summary, "results"):
            return []

        naming_signals = (
            "modulenotfounderror",
            "importerror",
            "cannot import name",
            "no module named",
            "nameerror",
            "is not defined",
        )

        export_path = Path(export_dir)
        implicated_files: set[str] = set()
        saw_naming_signal = False

        for result in (swarm_summary.results or []):
            result_passed = bool(getattr(result, "passed", False))
            if result_passed:
                continue

            chunks: List[str] = []
            for attr in ("failure_output", "failure_summary", "output", "stdout"):
                value = getattr(result, attr, "")
                if value:
                    chunks.append(str(value))
            failure_text = "\n".join(chunks)
            lowered = failure_text.lower()

            if any(signal in lowered for signal in naming_signals):
                saw_naming_signal = True

            for attr in ("test_file", "source_file", "file"):
                value = getattr(result, attr, "")
                if not value:
                    continue
                candidate = Path(str(value))
                if not candidate.is_absolute():
                    candidate = export_path / candidate
                try:
                    candidate.resolve().relative_to(export_path.resolve())
                except Exception:
                    continue
                if candidate.exists() and candidate.is_file():
                    implicated_files.add(candidate.relative_to(export_path).as_posix())

            for match in re.findall(r'File\s+"([^"]+)"', failure_text):
                candidate = Path(match)
                if not candidate.is_absolute():
                    candidate = export_path / candidate
                try:
                    candidate.resolve().relative_to(export_path.resolve())
                except Exception:
                    continue
                if candidate.exists() and candidate.is_file():
                    implicated_files.add(candidate.relative_to(export_path).as_posix())

        if not saw_naming_signal:
            return []

        if not implicated_files:
            for py_file in sorted(export_path.rglob("test_*.py")):
                try:
                    implicated_files.add(py_file.relative_to(export_path).as_posix())
                except Exception:
                    continue

        if not implicated_files:
            return []

        target_files = sorted(implicated_files)
        domain_layer = "tests" if all("/test" in f or f.startswith("test_") for f in target_files) else "backend"

        naming_subtasks = [
            OrchestratorSubTask(
                title="Round 0 naming triage",
                description=(
                    "Fix naming/import failures only: unresolved module paths, incorrect symbol imports, "
                    "and casing/path mismatches. Keep edits minimal and strictly scoped to target files."
                ),
                target_files=target_files,
                domain_layer=domain_layer,
                ambiguity_score=0,
                dependency_score=0,
                notes=(
                    "MICRO-FIX ONLY. Do NOT rewrite this file. "
                    "Change ONLY the import statement: replace '{wrong}' with '{correct}'. "
                    "Every other line must remain byte-for-byte identical. Touch nothing else."
                ),
            )
        ]

        naming_subtasks = [
            s for s in naming_subtasks
            if not any(
                "test_" in Path(f).name or "/tests/" in f or "\\tests\\" in f
                for f in (s.target_files or [])
            )
        ]

        return naming_subtasks

    async def _execute_subtask(
        self,
        subtask: OrchestratorSubTask,
        export_dir: str,
        semaphore: asyncio.Semaphore,
        max_tokens: int = 16384,
        target_language: str = "python",
        board=None,  # Optional[OrchestrationBoard] — injected by _run_orchestrated_build
    ) -> dict:
        """
        Execute a single orchestrated subtask.

        Args:
            subtask: The OrchestratorSubTask to execute
            export_dir: Base directory for writing output files
            semaphore: Async semaphore to limit concurrency
            max_tokens: Token budget for each AI file-generation call (default 16384)
            board: Optional OrchestrationBoard for swarm coordination (ADR-032)

        Returns:
            Dictionary with status, title, files written, etc.
        """
        async with semaphore:
            try:
                # Log start
                await self._broadcast("subtask_started", {
                    "title": subtask.title,
                    "description": subtask.description,
                    "tier": subtask.judge_result.tier if subtask.judge_result else "unknown",
                    "recommended_model": subtask.judge_result.recommended_model if subtask.judge_result else None,
                    "target_files": subtask.target_files,
                })
                logger.info(f"Starting subtask: {subtask.title}")

                # Determine backend
                tier = subtask.judge_result.tier if subtask.judge_result else "standard"
                backend = self._select_backend_for_tier(tier)

                if self.ai is None:
                    raise RuntimeError("AI backend not available")

                # Write output files
                written_files = []
                export_path = Path(export_dir)
                snapshot_id = None

                deps_text = ""
                if getattr(subtask, "depends_on", None):
                    dep_blocks: List[str] = []
                    for dep in subtask.depends_on:
                        dep_rel = str(dep or "").strip().replace("\\", "/")
                        if not dep_rel:
                            continue
                        dep_path = export_path / dep_rel
                        if dep_path.exists() and dep_path.is_file():
                            content = dep_path.read_text(encoding="utf-8", errors="replace")
                            dep_blocks.append(f"### {dep_rel}\n```\n{content}\n```")
                        else:
                            dep_blocks.append(f"### {dep_rel}\n(File not yet created)")
                    if dep_blocks:
                        deps_text = "## Required Dependencies Already Implemented\n" + "\n\n".join(dep_blocks)

                # Best-effort snapshot of existing files before writing
                try:
                    from core.snapshot import SnapshotManager
                    snapshot_mgr = SnapshotManager(getattr(self, "settings", None))

                    existing = [
                        str(export_path / rel_path)
                        for rel_path in subtask.target_files
                        if (export_path / rel_path).exists()
                    ]
                    if existing:
                        snapshot_id = await snapshot_mgr.create_snapshot(include_paths=existing)
                except Exception as snapshot_exc:
                    logger.warning(f"Snapshot failed before writing subtask files: {snapshot_exc}")

                repo_map_block = RepoMap.build(
                    export_dir=str(export_path),
                    language=target_language,
                    max_tokens=int(getattr(self.settings, "REPO_MAP_MAX_TOKENS", 2000)) if self.settings is not None else 2000,
                )
                
                # ── ADR-032: board registration + nudge check ──────────────────
                if board is not None:
                    # Mark task as in_progress on the board so siblings know it's active
                    primary_task_id = subtask.target_files[0] if subtask.target_files else subtask.title
                    await board.update_task(primary_task_id, status="in_progress")

                # ── ADR-033: swarm tools availability ──────────────────────────
                swarm_tools_enabled = (
                    getattr(self.settings, "SWARM_TOOLS_ENABLED", True)
                    if self.settings is not None else True
                )
                from core.swarm_tools import SwarmToolContext, dispatch_tool

                for target_file in subtask.target_files:
                    file_path = export_path / target_file

                    resolved = file_path.resolve()
                    export_resolved = export_path.resolve()
                    try:
                        resolved.relative_to(export_resolved)
                    except ValueError:
                        logger.warning(f"Blocked write outside export_dir: {resolved}")
                        await self._broadcast("subtask_warning", {
                            "title": subtask.title,
                            "message": f"Blocked write outside export_dir: {target_file}",
                        })
                        continue

                    # ── ADR-032: inject board context for this agent ───────────
                    board_context = ""
                    if board is not None:
                        try:
                            board_context = await board.format_context_for_agent(target_file)
                        except Exception as board_exc:
                            logger.warning(f"Board context retrieval failed: {board_exc}")

                    # Build focused coding prompt per file
                    # System prompt per Flash's own pipeline recommendation:
                    # temp=0.0, no filler, strict syntax, error handling, concise comments only.
                    _is_entrypoint = any(
                        Path(tf).name in {"main.py", "app.py", "server.py"}
                        for tf in subtask.target_files
                    )
                    base_system_prompt = (
                        "You are an expert software engineer in an automated build pipeline. "
                        "Write complete, syntactically correct, idiomatic code for the target file below.\n\n"
                        "Rules:\n"
                        "1. OUTPUT ONLY CODE — no markdown fences (```), no prose, no 'Here is your code' filler.\n"
                        "2. STRICT SYNTAX — code must be valid and runnable; follow PEP8 for Python, standard conventions for other languages.\n"
                        "3. ERROR HANDLING — include robust error handling and edge-case guards.\n"
                        "4. COMMENTS — concise docstrings on public functions/classes only; inline comments for non-obvious logic only.\n"
                        "5. IMPORTS — only import modules that are explicitly listed as dependencies or are stdlib/builtins.\n"
                        f"5a. {_sdk_ref_swarm}"
                        "5b. FILE VALIDATION — when validating uploaded files, check BOTH the file extension AND "
                        "content_type (don't reject on content_type alone; browsers send different MIME types). "
                        "Example: `if not (filename.endswith('.pdf') or content_type == 'application/pdf'): raise ...`\n"
                        "6. API KEYS — NEVER implement your own key storage, vault, or config.json for API keys. "
                        "Always obtain API keys via the bundled canopy_keys module: "
                        "`from canopy_keys import get_key, require_key`. "
                        "Example: `api_key = require_key('GOOGLE_API_KEY')`. "
                        "The canopy_keys module fetches keys from the Canopy vault automatically; "
                        "do not read from files, environment prompts, or hardcoded strings.\n"
                        + (
                        "7. ENTRY POINT STARTUP — You are writing the app entry point. "
                        "The FIRST executable line after imports must be:\n"
                        "   `from canopy_keys import load_keys_into_env; load_keys_into_env()`\n"
                        "This loads all API keys from the Canopy vault into os.environ before any "
                        "API client (Gemini, OpenAI, etc.) is initialized. After this call, read "
                        "API keys with `os.environ.get('GOOGLE_API_KEY')` — do NOT use require_key() "
                        "after load_keys_into_env() since they are already in os.environ.\n"
                        "8. STATIC FILES — If the project has HTML/CSS/JS frontend files, mount "
                        "the static directory so they are served:\n"
                        "   `from fastapi.staticfiles import StaticFiles`\n"
                        "   `app.mount('/', StaticFiles(directory='static', html=True), name='static')`\n"
                        "Mount AFTER all API routes are declared, so route definitions take priority.\n"
                        if _is_entrypoint else ""
                        ) +
                        "CROSS-FILE CONSISTENCY — other files in this project define routes, schemas, "
                        "and function names that yours must match exactly. Use only the route paths, "
                        "field names, and class names stated in the Frozen Test Contract above. "
                        "Do not invent new endpoint paths or rename existing ones."
                    )
                    # Compose system prompt: repo map → board context → tools → base instructions
                    system_parts = []
                    if repo_map_block:
                        system_parts.append(repo_map_block)
                    if board_context:
                        system_parts.append(board_context)
                    system_parts.append(SwarmToolContext.render(enabled=swarm_tools_enabled))
                    system_parts.append(base_system_prompt)
                    system_prompt = "\n\n".join(p for p in system_parts if p)

                    user_prompt = (
                        f"Task: {subtask.title}\n"
                        f"Description: {self._compose_subtask_description(description=subtask.description, raw_context='', deps_text=deps_text)}\n"
                        f"Target file: {target_file}\n"
                        f"All target files in this subtask: {', '.join(subtask.target_files)}\n"
                        f"Notes: {subtask.notes}"
                    )

                    # ── Primary AI call with gear-shift fallback ──────────────
                    try:
                        ai_output = await self.ai.complete(
                            backend=backend,
                            system=system_prompt,
                            message=user_prompt,
                            max_tokens=max_tokens,
                        )
                    except Exception as flash_exc:
                        escalation_chain: List[str] = []
                        qwen_backends = {"qwen-flash", "qwen-plus", "qwen-coder", "qwen-local"}
                        flash_backends = (
                            {"gemini-flash-lite", "gemini-flash", "gemini-flash-med", "openai-codex"}
                            | qwen_backends
                        )
                        if backend in flash_backends or (backend or "").startswith("gemini-flash-"):
                            escalation_chain = self._classify_error_gear_chain(flash_exc)

                        if not escalation_chain:
                            raise

                        ai_output = None
                        last_exc: Optional[Exception] = None
                        for escalation_backend in escalation_chain:
                            logger.warning(
                                f"Subtask '{subtask.title}' failed on backend '{backend}' with '{flash_exc}'; "
                                f"gear-shifting to {escalation_backend}"
                            )
                            try:
                                ai_output = await self.ai.complete(
                                    backend=escalation_backend,
                                    system=system_prompt,
                                    message=user_prompt,
                                    max_tokens=max_tokens,
                                )
                                logger.info(f"Gear-shift to {escalation_backend} succeeded for '{subtask.title}'")
                                break
                            except Exception as escalation_exc:
                                last_exc = escalation_exc
                                logger.error(f"Gear-shift to {escalation_backend} failed: {escalation_exc}")

                        if ai_output is None and last_exc is not None:
                            raise last_exc

                    # ── ADR-033: process any TOOL_CALL: lines in the response ──
                    # Dispatch tool calls and re-run up to 3 rounds so the agent
                    # can verify its output before committing to disk.
                    if swarm_tools_enabled and ai_output:
                        tool_calls = SwarmToolContext.extract_tool_calls(ai_output)
                        tool_rounds = 0
                        while tool_calls and tool_rounds < 3:
                            tool_rounds += 1
                            tool_results_text = []
                            for tc in tool_calls:
                                result = await dispatch_tool(
                                    tc["tool"], tc["input"], export_dir=str(export_path)
                                )
                                result_str = str(result)[:1000]
                                tool_results_text.append(
                                    f'TOOL_RESULT: {{"tool": "{tc["tool"]}", "result": {result_str}}}'
                                )
                                logger.debug(
                                    f"SwarmTool {tc['tool']!r} dispatched for {target_file!r}"
                                )
                            # Re-call with tool results so agent can refine
                            tool_followup = (
                                f"{ai_output}\n\n"
                                + "\n".join(tool_results_text)
                                + f"\n\nNow write the final complete file for {target_file}. "
                                + "Output ONLY the code — no TOOL_CALL lines."
                            )
                            try:
                                ai_output = await self.ai.complete(
                                    backend=backend,
                                    system=system_prompt,
                                    message=tool_followup,
                                    max_tokens=max_tokens,
                                )
                                tool_calls = SwarmToolContext.extract_tool_calls(ai_output)
                            except Exception as tool_exc:
                                logger.warning(
                                    f"Tool follow-up call failed for {target_file!r}: {tool_exc}"
                                )
                                break

                    ai_output = _strip_markdown_fences(ai_output)

                    # Strip any leftover TOOL_CALL / TOOL_RESULT lines from the
                    # final output.  This happens when flash-lite keeps emitting
                    # TOOL_CALLs across all 3 tool-loop rounds — the loop exits
                    # with ai_output still containing TOOL_CALL: text, which would
                    # be written as garbage Python/JS code.  Remove those lines
                    # unconditionally; they should never appear in a source file.
                    if swarm_tools_enabled and ai_output:
                        _clean_lines = [
                            _l for _l in ai_output.splitlines()
                            if not _l.strip().startswith("TOOL_CALL:")
                            and not _l.strip().startswith("TOOL_RESULT:")
                        ]
                        _cleaned = "\n".join(_clean_lines).strip()
                        if _cleaned != ai_output.strip():
                            _n_stripped = ai_output.strip().count("\n") + 1 - (_cleaned.count("\n") + 1)
                            logger.warning(
                                "Stripped %d TOOL_CALL/TOOL_RESULT line(s) from final output for %s",
                                _n_stripped, target_file,
                            )
                        ai_output = _cleaned

                    if self._is_test_file_target(target_file, ai_output):
                        await self._broadcast("subtask_warning", {
                            "title": subtask.title,
                            "message": f"Blocked write to protected target: {target_file}",
                        })
                        continue
                    
                    # Create parent directories
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Write generated file content
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(ai_output)
                    
                    written_files.append(str(file_path))
                    logger.info(f"Wrote: {file_path}")

                    # ── ADR-032: write exports to blackboard ──────────────────
                    if board is not None:
                        try:
                            # Extract top-level exports from the written output for siblings
                            import re as _re
                            if target_file.endswith(".py"):
                                # Python: capture class / def / __all__ top-level symbols
                                exports = _re.findall(r"^(?:class|def)\s+(\w+)", ai_output, _re.MULTILINE)
                                all_match = _re.search(r"__all__\s*=\s*\[([^\]]+)\]", ai_output)
                                if all_match:
                                    extras = _re.findall(r"['\"](\w+)['\"]", all_match.group(1))
                                    exports = list(dict.fromkeys(exports + extras))
                            elif target_file.endswith((".ts", ".tsx", ".js", ".jsx")):
                                exports = _re.findall(r"^export\s+(?:default\s+)?(?:class|function|const|interface|type)\s+(\w+)", ai_output, _re.MULTILINE)
                            else:
                                exports = []
                            if exports:
                                await board.write(
                                    f"{target_file}::exports",
                                    exports,
                                    author=target_file,
                                )

                            # ADR-032+: write INTERFACE SIGNATURES to board so sibling agents
                            # can see actual function params and class fields, not just names.
                            # This is the key missing piece for swarm communication — without it,
                            # agents importing from siblings guess at interfaces and generate
                            # mismatched code.
                            if target_file.endswith(".py"):
                                try:
                                    import ast as _board_ast
                                    _tree = _board_ast.parse(ai_output)
                                    _sigs: list = []
                                    for _node in _board_ast.iter_child_nodes(_tree):
                                        if isinstance(_node, _board_ast.ClassDef):
                                            # Extract class fields from __init__ or class-level annotations
                                            _fields = []
                                            for item in _node.body:
                                                if isinstance(item, _board_ast.AnnAssign) and item.target:
                                                    _fname = _board_ast.unparse(item.target)
                                                    _ftype = _board_ast.unparse(item.annotation) if item.annotation else "Any"
                                                    _fields.append(f"{_fname}: {_ftype}")
                                            if _fields:
                                                _sigs.append(f"class {_node.name}: {', '.join(_fields[:12])}")
                                            else:
                                                _bases = [_board_ast.unparse(b) for b in _node.bases[:3]]
                                                _sigs.append(f"class {_node.name}({', '.join(_bases)})")
                                        elif isinstance(_node, _board_ast.FunctionDef) or isinstance(_node, _board_ast.AsyncFunctionDef):
                                            # Extract function signature
                                            _args = []
                                            for arg in _node.args.args:
                                                _aname = arg.arg
                                                _atype = _board_ast.unparse(arg.annotation) if arg.annotation else ""
                                                _args.append(f"{_aname}: {_atype}" if _atype else _aname)
                                            _ret = f" -> {_board_ast.unparse(_node.returns)}" if _node.returns else ""
                                            _prefix = "async def" if isinstance(_node, _board_ast.AsyncFunctionDef) else "def"
                                            _sigs.append(f"{_prefix} {_node.name}({', '.join(_args[:8])}){_ret}")
                                    if _sigs:
                                        await board.write(
                                            f"{target_file}::interfaces",
                                            "\n".join(_sigs[:20]),
                                            author=target_file,
                                        )
                                except (SyntaxError, Exception):
                                    pass  # AST parse failed — fall back to export names only
                        except Exception as board_write_exc:
                            logger.warning(f"Board export write failed: {board_write_exc}")

                # ── ADR-032: mark task complete on the board ──────────────────
                if board is not None:
                    try:
                        primary_task_id = subtask.target_files[0] if subtask.target_files else subtask.title
                        await board.update_task(
                            primary_task_id,
                            status="complete",
                            exports=[str(Path(f).name) for f in written_files],
                        )
                    except Exception as board_complete_exc:
                        logger.warning(f"Board task completion update failed: {board_complete_exc}")

                # Broadcast completion
                await self._broadcast("subtask_done", {
                    "title": subtask.title,
                    "files_written": written_files,
                    "tier": tier,
                    "model": backend,
                    "snapshot_id": snapshot_id,
                })

                return {
                    "title": subtask.title,
                    "status": "done",
                    "files": written_files,
                }

            except Exception as exc:
                logger.error(f"Subtask '{subtask.title}' failed: {exc}", exc_info=True)
                await self._broadcast("subtask_error", {
                    "title": subtask.title,
                    "error": str(exc),
                })
                return {
                    "title": subtask.title,
                    "status": "error",
                    "error": str(exc),
                }

    # ------------------------------------------------------------------
    # Giant Brain helpers (ADR-023)
    # ------------------------------------------------------------------

    def _collect_test_output(self, swarm_summary) -> str:
        """Extract test failure output from a SwarmSummary as a formatted string."""
        install_hard_fail = bool(getattr(swarm_summary, "install_hard_fail", False))
        install_error = str(getattr(swarm_summary, "install_error", "") or "")

        if install_hard_fail:
            install_status = "## Install Status: HARD FAIL — pytest did not run. Fix packaging before diagnosing test failures.\n"
        elif install_error:
            install_status = "## Install Status: FAILED (pytest ran in degraded environment — results may be unreliable)\n"
        else:
            install_status = "## Install Status: SUCCESS\n"

        test_output_parts = []
        for result in (swarm_summary.results if hasattr(swarm_summary, "results") else []):
            if hasattr(result, "passed") and not result.passed:
                parts = []
                # TestResult fields: source_file, test_file, failure_output, failure_summary
                src = getattr(result, "source_file", None) or getattr(result, "file", None)
                tst = getattr(result, "test_file", None) or getattr(result, "test_name", None)
                out = (
                    getattr(result, "failure_output", None)
                    or getattr(result, "output", None)
                    or getattr(result, "stdout", None)
                )
                if src:
                    parts.append(f"FILE: {src}")
                if tst:
                    parts.append(f"TEST: {tst}")
                if out:
                    parts.append(out)
                if parts:  # only append if we have actual content
                    test_output_parts.append("\n".join(parts))

        if install_error:
            install_section = (
                "## Package Install Error (pip install -e . failed)\n"
                f"{install_error}\n\n"
                "NOTE: This install error may be causing import failures in the tests above. "
                "If __init__.py lacks a __version__ attribute, add __version__ = \"0.1.0\" as the first fix."
            )
            if test_output_parts:
                return install_status + "\n" + install_section + "\n\n---\n\n" + "\n\n---\n\n".join(test_output_parts)
            return install_status + "\n" + install_section

        if test_output_parts:
            return install_status + "\n" + "\n\n---\n\n".join(test_output_parts)
        return install_status + "\n" + str(swarm_summary)

    async def _giant_brain_audit(
        self,
        export_dir: Path,
        test_output: str,
        canonical_test_file: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Giant Brain post-build audit (ADR-023).

        Reads every source file in export_dir plus the full pytest failure output
        and sends them to Claude Sonnet.  Returns a FixManifest dict or None on
        failure.  If Giant Brain sets ``"escalate": true``, the caller must emit
        user_guided_debug_state instead of running a fixer pass.

        canonical_test_file: the frozen contract test filename (e.g. "habit_tracker_test.py").
          If provided, Giant Brain uses this as ground truth when detecting stale test files.
        """
        source_tree = self._collect_source_tree(export_dir)

        source_parts = []
        for file_path, content in source_tree.items():
            source_parts.append(f"### {file_path}\n```\n{content}\n```")
        source_block = "\n\n".join(source_parts) if source_parts else "(no source files found)"

        # Build canonical test context line for the prompt
        canonical_clause = (
            f"\n\nCANONICAL TEST FILE: The one true test file for this project is "
            f"\"tests/{canonical_test_file}\". Any other test file discovered in the "
            f"project is a stale artifact from a previous pipeline run and MUST be "
            f"deleted via action=\"delete_file\". Do NOT treat stale test files as "
            f"evidence of conflicting implementations — they are leftover noise."
            if canonical_test_file
            else ""
        )

        system_prompt = (
            "You are a senior software engineer performing a deep post-build audit.\n"
            "You will receive all source and test files from a project and the full pytest failure output.\n"
            "Your job: produce a precise, actionable FixManifest in JSON.\n\n"
            "═══ PHASE 1 — INVENTORY (do this mentally before emitting any JSON) ═══\n"
            "1a. List every test file present (files named test_*.py or *_test.py).\n"
            "1b. Identify the CANONICAL test file (see CANONICAL TEST FILE below if provided).\n"
            "1c. Flag any non-canonical test files as stale — they must be deleted, not fixed.\n"
            "1d. List every source module imported by the canonical test.\n"
            "1e. For each failing assertion: trace the call path from the test assertion\n"
            "    → through all intermediate function calls → to the actual source that\n"
            "    produces the wrong value. Record the root cause file and line.\n\n"
            "═══ PHASE 2 — ROOT CAUSE ANALYSIS ═══\n"
            "2a. For each test failure, ask:\n"
            "    - Is the import path correct? (ModuleNotFoundError → wrong package name)\n"
            "    - Is the class/function name correct? (AttributeError → rename mismatch)\n"
            "    - Is the function signature correct? (TypeError → wrong args/kwargs)\n"
            "    - Is the return value/type correct? (AssertionError → wrong logic/value)\n"
            "    - Is a required feature missing entirely? (NotImplementedError → add_feature)\n"
            "2b. Group failures by root cause file — multiple tests often share one root cause.\n"
            "2c. A stale test file that imports a non-existent package is NOT a source bug;\n"
            "    delete the stale file first, then re-evaluate.\n"
            "2d. MOCK PATCH AUDIT: For every `@patch('A.B.func')` in the test, verify the patch\n"
            "    target matches the actual import. If `main.py` does `from .extractor import func`,\n"
            "    then the mock MUST patch `pkg.main.func` (where it's used), NOT `pkg.extractor.func`\n"
            "    (where it's defined). A wrong patch target causes the real function to execute,\n"
            "    producing errors like 'API key not set' or '500 Internal Server Error' even though\n"
            "    the mock is configured correctly. If you find a wrong patch target, emit a fix_content\n"
            "    action on the test file to correct ONLY the @patch() path — do NOT rewrite the test.\n"
            "    EXCEPTION: The test file is read-only ONLY IF it is the canonical frozen contract.\n"
            "    If the test was generated by the contract and the patch target is wrong, you may fix\n"
            "    the patch target string inside the test file as a mechanical fix.\n"
            "2e. MODULE-LEVEL SIDE EFFECTS: If a source file calls init functions (load_keys, init_db,\n"
            "    connect, etc.) at module scope (outside any function/class), these run at import time\n"
            "    before test fixtures. Move them into the lifespan handler or a lazy-init function.\n\n"
            "═══ PHASE 3 — FIX MANIFEST RULES ═══\n"
            "- Emit ONLY valid JSON — no narrative text, no markdown, no code fences.\n"
            "- NEVER rewrite or regenerate the canonical test file — it is read-only input.\n"
            "  EXCEPTION: You MAY fix `@patch()` target paths in the canonical test if the\n"
            "  patch target is provably wrong (patches where the function is defined instead\n"
            "  of where it's used). This is a mechanical string replacement, not a rewrite.\n"
            "  You may also move module-level `TestClient(app)` into a fixture if needed.\n"
            "- Stale (non-canonical) test files: action=\"delete_file\", complexity=\"mechanical\".\n"
            "- complexity=\"mechanical\": rename, add import, fix signature, wire export, add missing\n"
            "  class, fix string literal or error message, fix return value in a single function,\n"
            "  add/fix a missing symbol, delete a stale file.\n"
            "- complexity=\"architectural\": changes spanning 2+ files, redesign of a data contract\n"
            "  or class hierarchy, add missing feature logic, fix a fundamental algorithmic error.\n"
            "  Single-file string or value changes are NEVER architectural.\n"
            "- Any change to a function's return TYPE (str → float, float → str) is architectural.\n"
            "- Error message text fixes (add period, change wording) are mechanical.\n"
            "- If a mechanical fix has been attempted 2+ times on the same file and is still\n"
            "  failing, reclassify it as architectural on the next pass.\n"
            "- FILE RENAME: to rename a file (e.g. store.py → tracker.py), emit TWO fix items:\n"
            "  1. action=\"delete_file\", file=\"old/path/store.py\" (deletes the old file)\n"
            "  2. action=\"fix_content\", file=\"new/path/tracker.py\" (creates and writes the new file)\n"
            "  Never emit a single fix that 'renames' by writing to the old filename — the test\n"
            "  will still fail because the import still points to the new name.\n"
            "- Sequence: stale-file deletions first, then mechanical fixes, then architectural.\n"
            "- Do not emit more than 3 fixes per file. Consolidate if needed.\n"
            "- When root cause is ambiguous, pick the most conservative fix.\n"
            "- escalate=true ONLY as absolute last resort — when the canonical test file\n"
            "  asserts behaviour that is logically impossible to implement given the project\n"
            "  description. Conflicting test files, import errors, missing symbols, wrong\n"
            "  package names, and stale artifacts are ALL fixable — do NOT escalate for these.\n\n"
            f"{canonical_clause}\n\n"
            "Output ONLY this JSON structure (no preamble, no explanation):\n"
            "{\n"
            "  \"summary\": \"one-sentence root cause summary\",\n"
            "  \"escalate\": false,\n"
            "  \"fixes\": [\n"
            "    {\n"
            "      \"file\": \"path/to/file.py\",\n"
            "      \"complexity\": \"mechanical\",\n"
            "      \"description\": \"Precise description of what to change and why\",\n"
            "      \"action\": \"rename_function|add_import|fix_signature|rewrite_exports|add_feature|fix_content|delete_file|other\"\n"
            "    }\n"
            "  ]\n"
            "}"
        )

        user_prompt = (
            f"## Source Files\n\n{source_block}\n\n"
            f"## Test Failure Output\n\n{test_output}\n\n"
            "Work through Phase 1 (inventory) and Phase 2 (root cause) silently, "
            "then emit ONLY the fix manifest JSON. No preamble, no markdown, no explanation."
        )

        # ADR-039: Walk giant_brain role chain with increasing token budget on truncation.
        # Base budget 16 384; if a backend returns truncated JSON ("Unterminated string"),
        # retry that same backend with 2x budget before moving to the next chain member.
        _GB_BASE_TOKENS = 16384
        _gb_chain = self.ai.get_role_chain("giant_brain")
        raw = None

        def _strip_fences(text: str) -> str:
            text = text.strip()
            if text.startswith("```") and text.endswith("```"):
                lines = text.splitlines()
                if len(lines) >= 3:
                    return "\n".join(lines[1:-1]).strip()
            return text

        for _gb_backend in _gb_chain:
            for _gb_tokens in (_GB_BASE_TOKENS, _GB_BASE_TOKENS * 2):
                try:
                    logger.debug(
                        "Giant Brain audit: trying backend '%s' max_tokens=%d",
                        _gb_backend, _gb_tokens,
                    )
                    raw = await self.ai.complete(
                        system=system_prompt,
                        message=user_prompt,
                        backend=_gb_backend,
                        json_mode=True,
                        max_tokens=_gb_tokens,
                    )
                    raw = _strip_fences(raw or "")
                    # Validate JSON — if truncated, retry with larger budget;
                    # if the model returned a list instead of a dict, treat it
                    # as a format failure and try the next backend.
                    _parsed_check = json.loads(raw)
                    if not isinstance(_parsed_check, dict):
                        logger.warning(
                            "Giant Brain '%s': response is %s not a dict — trying next backend",
                            _gb_backend, type(_parsed_check).__name__,
                        )
                        raw = None
                        break  # move to next backend in chain
                    break  # valid JSON dict — exit token-retry loop
                except json.JSONDecodeError as _json_exc:
                    _is_truncation = any(
                        kw in str(_json_exc)
                        for kw in ("Unterminated string", "Expecting", "Extra data")
                    )
                    if _is_truncation and _gb_tokens == _GB_BASE_TOKENS:
                        logger.warning(
                            "Giant Brain '%s': JSON truncated at %d tokens — retrying at %d",
                            _gb_backend, _gb_tokens, _gb_tokens * 2,
                        )
                        continue  # retry same backend with 2x budget
                    logger.warning(
                        "Giant Brain '%s': unparseable JSON (%s) — trying next backend",
                        _gb_backend, _json_exc,
                    )
                    raw = None
                    break  # move to next backend in chain
                except Exception as _exc:
                    logger.warning(
                        "Giant Brain audit: backend '%s' failed (%s) — trying next",
                        _gb_backend, _exc,
                    )
                    raw = None
                    break  # move to next backend in chain
            if raw:
                break  # successfully got valid JSON from this backend

        if not raw:
            logger.error("Giant Brain audit: all backends exhausted — no valid manifest")
            return None

        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(f"Giant Brain returned unparseable JSON: {exc}\nRaw: {raw[:500]}")
            return None

        if not isinstance(manifest, dict):
            logger.error(f"Giant Brain manifest is not a dict: {type(manifest)}")
            return None

        # Normalise escalation flag
        if manifest.get("escalate"):
            logger.warning(f"Giant Brain requested escalation: {manifest.get('summary', '')}")
            return {"escalate": True, "summary": manifest.get("summary", ""), "fixes": []}

        fixes = manifest.get("fixes")
        if not isinstance(fixes, list):
            manifest["fixes"] = []

        # Ensure every fix has a complexity field
        for fix in manifest["fixes"]:
            if "complexity" not in fix:
                fix["complexity"] = "mechanical"

        logger.info(
            f"Giant Brain audit complete — {len(manifest['fixes'])} fix(es), "
            f"summary: {manifest.get('summary', '')[:100]}"
        )
        return manifest

    async def _run_fixer_pass(
        self,
        manifest: dict,
        export_dir: Path,
        source_tree: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """
        2-tier fixer pass (ADR-023).

        Reads the FixManifest and routes each fix by complexity:
        - mechanical  → openai-nano  (cheap, fast, run first)
        - architectural → gemini      (full context, run after mechanical)

        Returns a list of file paths that were touched.
        """
        fixes = manifest.get("fixes", [])
        if not fixes:
            return []

        mechanical = [f for f in fixes if f.get("complexity") == "mechanical"]
        architectural = [f for f in fixes if f.get("complexity") != "mechanical"]

        files_touched: List[str] = []
        live_source_tree: Dict[str, str] = source_tree or self._collect_source_tree(export_dir)

        async def _apply_single_fix(fix: dict, backend: str) -> None:
            file_rel = fix.get("file", "")
            if not file_rel:
                logger.warning(f"Fix item missing 'file' field: {fix}")
                return

            action = fix.get("action", "")

            # ── delete_file: remove the file, no AI call needed ───────────────
            if action == "delete_file":
                target_path = export_dir / file_rel
                if not target_path.exists():
                    # Try rglob fallback
                    matches = list(export_dir.rglob(Path(file_rel).name))
                    target_path = matches[0] if matches else target_path
                if target_path.exists():
                    try:
                        target_path.unlink()
                        files_touched.append(file_rel)
                        logger.info(f"Fix (delete_file): removed stale file {file_rel}")
                    except Exception as exc:
                        logger.warning(f"Fix (delete_file): could not remove {file_rel}: {exc}")
                else:
                    logger.info(f"Fix (delete_file): file already absent: {file_rel}")
                return

            target_path = export_dir / file_rel
            if not target_path.exists():
                # Fallback 1: search by filename only (file exists but under different path)
                matches = list(export_dir.rglob(Path(file_rel).name))
                if matches:
                    target_path = matches[0]
                else:
                    # Fallback 2: package alias resolution — same logic as Big Fixer.
                    # The model may request "habit_tracker/X.py" when the real package
                    # dir is "python_cli_habit_tracker/".  Remap to the real package.
                    file_parts = Path(file_rel).parts
                    req_pkg = file_parts[0] if len(file_parts) > 1 else None
                    remapped = False
                    if req_pkg and not (export_dir / req_pkg).exists():
                        pkg_dirs = [
                            d for d in export_dir.iterdir()
                            if d.is_dir() and (d / "__init__.py").exists()
                        ]
                        if pkg_dirs:
                            real_pkg = pkg_dirs[0]
                            remapped_path = real_pkg / Path(*file_parts[1:])
                            logger.info(
                                f"Fix: remapping '{file_rel}' → "
                                f"'{remapped_path.relative_to(export_dir)}' "
                                f"(alias {req_pkg!r} → {real_pkg.name!r})"
                            )
                            remapped_path.parent.mkdir(parents=True, exist_ok=True)
                            target_path = remapped_path
                            remapped = True
                    if not target_path.exists():
                        # Fallback 3: create the file at the resolved path.
                        # Runs whether or not alias remapping fired — a remapped
                        # path that still doesn't exist on disk needs to be seeded
                        # so the AI repair call has a target to write into.
                        _label = "remapped" if remapped else "requested"
                        logger.info(
                            f"Fix: creating new file at {_label} path: "
                            f"{target_path.relative_to(export_dir)}"
                        )
                        try:
                            target_path.parent.mkdir(parents=True, exist_ok=True)
                            target_path.write_text("", encoding="utf-8")
                        except Exception as _create_exc:
                            logger.warning(
                                f"Fix: could not create {_label} file "
                                f"{target_path.relative_to(export_dir)}: {_create_exc} — skipping"
                            )
                            return

            try:
                current_content = target_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                logger.warning(f"Could not read fix target {target_path}: {exc}")
                return

            description = fix.get("description", "")
            action = fix.get("action", "")
            complexity = fix.get("complexity", "mechanical")

            if complexity == "mechanical":
                system_prompt = (
                    "You are a strict code repair bot. Apply the exact fix described below. "
                    "Return ONLY the raw updated file content. "
                    "CRITICAL: Do NOT wrap your output in markdown code blocks (```python). Do NOT write any explanations or preamble. Raw source code only."
                )
                user_prompt = (
                    f"## Fix to Apply\n"
                    f"File: {file_rel}\n"
                    f"Description: {description}\n"
                    f"Action: {action}\n\n"
                    f"## Current File Content\n"
                    f"{current_content}\n\n"
                    "If the file contains a __version__ attribute (e.g. __version__ = \"0.1.0\"),\n"
                    "you MUST preserve it exactly as-is in the output. Do not alter its value or format.\n\n"
                    "Apply the fix and return the complete updated file. Raw source code only."
                )
            else:
                # Architectural — include related source files for full context
                context_parts = []
                for src_path, src_content in live_source_tree.items():
                    if (
                        src_path != file_rel
                        and not src_path.endswith("_test.py")
                        and not Path(src_path).name.startswith("test_")
                    ):
                        context_parts.append(f"### {src_path}\n```\n{src_content}\n```")
                related_block = "\n\n".join(context_parts[:5]) if context_parts else "(none)"

                system_prompt = (
                    "You are a strict senior software engineer making a targeted architectural fix. "
                    "Apply the described change precisely, taking all related files into account. "
                    "Return ONLY the raw updated file content. "
                    "CRITICAL: Do NOT wrap your output in markdown code blocks (```). Do NOT write any explanations. Raw source code only."
                )
                user_prompt = (
                    f"## Fix to Apply\n"
                    f"File: {file_rel}\n"
                    f"Description: {description}\n\n"
                    f"## Current File Content\n"
                    f"{current_content}\n\n"
                    f"## Related Files (context only — do not rewrite these)\n"
                    f"{related_block}\n\n"
                    "Apply the fix and return the complete updated file. Raw source code only."
                )

            try:
                updated = await self.ai.complete(
                    system=system_prompt,
                    message=user_prompt,
                    backend=backend,
                    max_tokens=16384,
                )
            except Exception as exc:
                logger.error(f"Fixer failed for {file_rel} (complexity={complexity}, backend={backend}): {exc}")
                return

            updated = _strip_markdown_fences(updated).strip()

            if not updated:
                logger.warning(f"Fixer returned empty content for {file_rel} — skipping write")
                return

            try:
                target_path.write_text(updated, encoding="utf-8")
                files_touched.append(str(target_path))
                actual_backend = backend
                if backend == "gemini" and hasattr(self, "ai") and hasattr(self.ai, "_gemini_pro_degraded") and self.ai._gemini_pro_degraded:
                    actual_backend = "claude (circuit breaker)"
                logger.info(f"Applied {complexity} fix to {file_rel} via {actual_backend}")
            except Exception as exc:
                logger.error(f"Could not write fix to {target_path}: {exc}")

        # Mechanical fixes first — sequential (cheap + fast)
        # Session 52: gemini-flash replaces openai-codex (direct completion, no tool calls)
        for fix in mechanical:
            try:
                await _apply_single_fix(fix, "gemini-flash")
            except Exception as _mech_exc:
                logger.warning(f"Mechanical fix via gemini-flash failed ({_mech_exc}) — retrying via gemini-customtools")
                await _apply_single_fix(fix, "gemini-customtools")

        # Refresh source tree so architectural fixes see mechanical changes
        if mechanical:
            live_source_tree = self._collect_source_tree(export_dir)

        # Architectural fixes — sequential, refresh tree after each
        for fix in architectural:
            await _apply_single_fix(fix, "gemini-customtools")
            live_source_tree = self._collect_source_tree(export_dir)

        return files_touched

    async def _rerun_tests(self, export_dir: Path):
        """Re-run TesterSwarm and return the fresh SwarmSummary."""
        from core.tester_swarm import TesterSwarm
        # Fresh instance per rerun: _editable_install_done cache is per-TesterSwarm,
        # so editable install is attempted again after Giant Brain modifies files.
        swarm = TesterSwarm(ai_backend=self.ai, sse_broadcast=self._sse_broadcast)
        return await swarm.run(export_dir=str(export_dir))

    def _auto_fix_missing_deps(self, export_dir: Path) -> bool:
        """Scan all generated .py files for third-party imports not declared in pyproject.toml.

        Returns True if pyproject.toml was modified (caller should reinstall deps).
        Catches the common case where a subtask imports fastapi/uvicorn/etc. but the
        orchestrator decompose step forgot to list them as dependencies.
        """
        import sys as _sys
        import ast as _ast
        import sysconfig as _sysconfig

        pyproject_path = export_dir / "pyproject.toml"
        if not pyproject_path.exists():
            return False

        try:
            pyproject_text = pyproject_path.read_text(encoding="utf-8")
        except Exception:
            return False

        # Collect all top-level import names from generated .py files
        imported_names: set = set()
        pkg_dirs = [d for d in export_dir.iterdir() if d.is_dir() and (d / "__init__.py").exists()]
        search_dirs = pkg_dirs if pkg_dirs else [export_dir]
        for search_dir in search_dirs:
            for py_file in search_dir.rglob("*.py"):
                try:
                    source = py_file.read_text(encoding="utf-8", errors="replace")
                    tree = _ast.parse(source)
                except Exception:
                    continue
                for node in _ast.walk(tree):
                    if isinstance(node, _ast.Import):
                        for alias in node.names:
                            imported_names.add(alias.name.split(".")[0])
                    elif isinstance(node, _ast.ImportFrom):
                        if node.module and node.level == 0:
                            imported_names.add(node.module.split(".")[0])

        import sys as _sys
        stdlib_names: set = set(_sys.stdlib_module_names) if hasattr(_sys, "stdlib_module_names") else set()
        # Additional well-known stdlib / always-available names
        stdlib_names.update({
            "os", "sys", "re", "json", "time", "datetime", "pathlib", "typing",
            "collections", "itertools", "functools", "logging", "asyncio",
            "abc", "io", "math", "random", "hashlib", "uuid", "copy",
            "traceback", "inspect", "warnings", "enum", "dataclasses",
            "contextlib", "threading", "subprocess", "shutil", "tempfile",
            "urllib", "http", "email", "socket", "struct", "base64",
            "canopy_keys",  # injected by pipeline — not a pypi package
            "tests",  # local test package
        })

        # Local package names (don't need to be in pyproject.toml)
        local_pkg_names = {d.name for d in pkg_dirs}
        # Also include any top-level .py file names
        for py_file in export_dir.glob("*.py"):
            local_pkg_names.add(py_file.stem)

        # Identify third-party names not already in pyproject.toml
        pyproject_lower = pyproject_text.lower()
        missing: list = []
        for name in sorted(imported_names):
            if name in stdlib_names or name in local_pkg_names:
                continue
            # Normalize: starlette is part of fastapi, pydantic comes with fastapi, etc.
            check_name = name.replace("_", "-").lower()
            if check_name not in pyproject_lower and name.lower() not in pyproject_lower:
                missing.append(name)

        if not missing:
            return False

        # Map common import names to their pypi package names
        _pypi_map = {
            "fastapi": "fastapi[standard]",
            "uvicorn": "uvicorn[standard]",
            "starlette": "starlette",
            "pydantic": "pydantic",
            "sqlalchemy": "sqlalchemy",
            "aiofiles": "aiofiles",
            "httpx": "httpx",
            "requests": "requests",
            "PIL": "Pillow",
            "cv2": "opencv-python",
            "numpy": "numpy",
            "pandas": "pandas",
            "google": "google-generativeai",
            "anthropic": "anthropic",
            "openai": "openai",
            "dotenv": "python-dotenv",
            "yaml": "pyyaml",
            "toml": "tomli",
            "pypdf2": "PyPDF2",
            "pymupdf": "PyMuPDF",
            "fitz": "PyMuPDF",
        }
        deps_to_add = [_pypi_map.get(n, n) for n in missing]

        logger.info(
            f"Import scan: adding {len(deps_to_add)} missing deps to pyproject.toml: "
            f"{', '.join(deps_to_add)}"
        )

        # Insert into [project] dependencies list in pyproject.toml
        import re as _re
        deps_str = "\n".join(f'    "{d}",' for d in deps_to_add)
        # Find the dependencies = [ block and append before closing ]
        def _insert_deps(text: str, new_deps: str) -> str:
            pattern = r'(dependencies\s*=\s*\[)(.*?)(\])'
            match = _re.search(pattern, text, _re.DOTALL)
            if match:
                existing = match.group(2).rstrip()
                sep = ",\n" if existing and not existing.endswith(",") else "\n"
                return text[:match.start(2)] + existing + sep + new_deps + "\n" + text[match.end(2):]
            # No dependencies block — add one under [project]
            return text + f'\n# Auto-added by import scan\ndependencies = [\n{new_deps}\n]\n'

        new_text = _insert_deps(pyproject_text, deps_str)
        try:
            pyproject_path.write_text(new_text, encoding="utf-8")
            return True
        except Exception as exc:
            logger.warning(f"Import scan: could not update pyproject.toml: {exc}")
            return False

    async def _reinstall_with_deps(self, export_dir: Path) -> None:
        """Run pip install -e . WITH dependencies (no --no-deps flag).

        Called after a fixer pass touches pyproject.toml so newly-added
        dependencies (e.g. sqlalchemy) are actually installed before the
        next test run.  Uses --break-system-packages for the sandbox env.
        """
        import sys as _sys
        cmd = [
            _sys.executable, "-m", "pip", "install", "-e", ".",
            "--break-system-packages", "-q",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(export_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                logger.warning(
                    f"_reinstall_with_deps failed in {export_dir}: "
                    f"{(stdout_b + stderr_b).decode(errors='replace')[:300]}"
                )
            else:
                logger.info(f"_reinstall_with_deps OK in {export_dir}")
        except Exception as exc:
            logger.warning(f"_reinstall_with_deps exception in {export_dir}: {exc}")

    # ------------------------------------------------------------------
    # Giant Brain repair orchestration (ADR-023) — replaces _run_repair_loop
    # ------------------------------------------------------------------

    async def _run_giant_brain_repair(
        self,
        swarm_summary,
        export_dir: Path,
        project_context: dict,
        session_id: str,
        memory: Optional["SwarmMemory"] = None,
    ) -> tuple:
        """
        Giant Brain repair flow (ADR-023).

        Round 0 — naming/import triage (Flash, fast, cheap) — preserved from ADR-016.
        Passes 1-2 — Giant Brain audit (Sonnet) → structured FixManifest →
                      2-tier fixer (mechanical→openai-nano, architectural→gemini) →
                      re-run tests.
        After two passes with failures remaining the caller emits
        user_guided_debug_state.

        memory: optional SwarmMemory; used to extract the canonical test file name
          so that stale test files from prior runs can be purged before auditing.

        Returns (final_swarm_summary, repair_history).
        """
        from core.orchestrator import ProjectOrchestrator
        from core.tester_swarm import TesterSwarm

        if bool(getattr(swarm_summary, "install_hard_fail", False)):
            await self._broadcast("user_guided_debug_state", {
                "project_name": project_context.get("project_name") or project_context.get("name") or "project",
                "tests_still_failing": swarm_summary.failed,
                "tests_passing": swarm_summary.passed,
                "repair_rounds": 0,
                "repair_history": [],
                "message": "Install hard fail — packaging must be fixed before test diagnostics are meaningful.",
                "export_dir": str(export_dir),
                "session_id": session_id,
            })
            logger.warning("Install hard fail detected — skipping Giant Brain repair passes")
            return swarm_summary, []

        # ── Stale test file purge (ADR-039) ───────────────────────────────────
        # If the frozen contract names a canonical test file, delete any other test
        # files that were left behind by a previous pipeline run.  Two competing test
        # suites cause Giant Brain to see contradictory failures and mis-escalate.
        canonical_test_file: Optional[str] = None
        if memory is not None and memory.is_contract_frozen():
            contract = memory.get_contract()
            if contract and contract.test_file_name:
                canonical_test_file = contract.test_file_name
                tests_dir = export_dir / "tests"
                if tests_dir.is_dir():
                    for stale in tests_dir.iterdir():
                        if not stale.is_file():
                            continue
                        name = stale.name
                        is_test_file = name.startswith("test_") or name.endswith("_test.py")
                        if is_test_file and name != canonical_test_file:
                            try:
                                stale.unlink()
                                logger.warning(
                                    "Stale test file purged before Giant Brain audit: %s "
                                    "(canonical: %s)",
                                    name, canonical_test_file,
                                )
                            except Exception as _purge_exc:
                                logger.warning(
                                    "Could not purge stale test file %s: %s",
                                    name, _purge_exc,
                                )

        orchestrator = ProjectOrchestrator(self.ai)
        repair_history: List[dict] = []
        current_summary = swarm_summary

        # ADR-016 naming triage removed (Session 51) — Giant Brain 3-phase audit
        # (Phase 1b root-cause analysis) is a strict superset: it sees all files
        # including test files, uses the full model chain, and produces a
        # structured FixManifest. Triage ran flash-lite at 1024 tokens and was
        # unable to touch test files, making it ineffective for package-name
        # mismatches between source modules and test imports.

        # ── Giant Brain passes — maximum 2 ────────────────────────────────────
        for pass_num in range(1, 3):
            if current_summary.failed == 0:
                break

            # Finding 5: snapshot failure count before the pass so we can detect zero-progress.
            _failing_before_pass = current_summary.failed

            logger.info(f"Giant Brain audit pass {pass_num}/2 — {current_summary.failed} failing tests")
            await self._broadcast("repair_round_started", {
                "round": pass_num,
                "failed_tests": current_summary.failed,
                "session_id": session_id,
                "phase": "giant_brain_audit",
            })

            test_output = self._collect_test_output(current_summary)

            # Guard: if failure count > 0 but test output has no FAILED lines,
            # Giant Brain would incorrectly conclude all tests pass — skip it
            has_failure_lines = "FAILED" in test_output or "ERROR" in test_output
            if current_summary.failed > 0 and not has_failure_lines:
                logger.warning(
                    "Giant Brain skipped — test output missing FAILED lines but "
                    f"{current_summary.failed} failures reported. Going straight to Big Fixer."
                )
                manifest = None
            else:
                manifest = await self._giant_brain_audit(
                    export_dir, test_output, canonical_test_file=canonical_test_file
                )

            if manifest is None:
                logger.warning(f"Giant Brain pass {pass_num}: audit returned no manifest — stopping")
                repair_history.append({
                    "round": pass_num,
                    "phase": "giant_brain_audit",
                    "note": "Giant Brain audit failed — no manifest returned",
                    "tests_failed_after": current_summary.failed,
                })
                break

            if manifest.get("escalate"):
                summary_msg = manifest.get("summary", "")
                logger.warning(f"Giant Brain pass {pass_num}: escalation requested — {summary_msg}")
                repair_history.append({
                    "round": pass_num,
                    "phase": "giant_brain_audit",
                    "note": f"Giant Brain escalated: {summary_msg}",
                    "tests_failed_after": current_summary.failed,
                })
                await self._broadcast("giant_brain_escalation", {
                    "pass": pass_num,
                    "summary": summary_msg,
                    "session_id": session_id,
                })
                break

            fixes = manifest.get("fixes", [])
            logger.info(
                f"Giant Brain pass {pass_num}: {len(fixes)} fix(es), "
                f"summary: {manifest.get('summary', '')[:100]}"
            )

            files_touched = await self._run_fixer_pass(manifest, export_dir)
            # If pyproject.toml was among the touched files, a dependency was added/changed.
            # The normal tester_swarm install uses --no-deps, so new deps would never be
            # installed.  Run a full dep install here before the rerun so the fix takes effect.
            if any("pyproject.toml" in f for f in files_touched):
                logger.info("Giant Brain touched pyproject.toml — running full dep install")
                await self._reinstall_with_deps(export_dir)
            current_summary = await self._rerun_tests(export_dir)

            repair_history.append({
                "round": pass_num,
                "phase": "giant_brain_audit",
                "fix_count": len(fixes),
                "files_touched": files_touched,
                "giant_brain_summary": manifest.get("summary", ""),
                "tests_failed_after": current_summary.failed,
                "tests_passed_after": current_summary.passed,
            })
            await self._broadcast("repair_round_complete", {
                "round": pass_num,
                "phase": "giant_brain_audit",
                "fix_count": len(fixes),
                "files_touched": files_touched,
                "tests_failed": current_summary.failed,
                "tests_passed": current_summary.passed,
                "session_id": session_id,
            })
            logger.info(f"Giant Brain pass {pass_num} complete — {current_summary.failed} still failing")

            # Finding 5: if this pass made zero progress (same or more failures than before),
            # stop immediately — a second identical Giant Brain pass would produce the same
            # manifest and waste another audit + fixer cycle before reaching Big Fixer.
            if current_summary.failed >= _failing_before_pass and pass_num < 2:
                logger.warning(
                    f"Giant Brain pass {pass_num}: no improvement "
                    f"({_failing_before_pass} → {current_summary.failed} failures) — "
                    f"skipping pass 2, proceeding to Big Fixer"
                )
                repair_history.append({
                    "round": f"{pass_num}_no_progress",
                    "phase": "giant_brain_audit",
                    "note": (
                        f"Giant Brain pass {pass_num} made no progress "
                        f"({_failing_before_pass} → {current_summary.failed} failures) — "
                        f"early exit to Big Fixer"
                    ),
                    "tests_failed_after": current_summary.failed,
                })
                break

        return current_summary, repair_history

    async def _post_giant_brain_router(
        self,
        swarm_summary,
        export_dir: Path,
        repair_history: list,
        project_context: dict,
        session_id: str,
        escalate_to_contract_fired: bool = False,
    ):
        """
        ADR-030 — Three-tier threshold router.

        After Giant Brain pass 2 (or a collection error), compute pass rate and route:
        - Collection error (0 collected) → Big Fixer directly
        - pass_rate < LOW_THRESHOLD (70%)  → user_guided_debug_state (DUMP)
        - LOW_THRESHOLD ≤ pass_rate < HIGH_THRESHOLD (70–95%) → Big Fixer
        - pass_rate ≥ HIGH_THRESHOLD (95%) → Fix #3 geared up, then Big Fixer if still failing

        Returns the final swarm_summary. If user_guided_debug_state was emitted, returns
        a swarm_summary with failed > 0 (caller must check and return).
        """
        low = getattr(self.settings, "BIG_FIXER_LOW_THRESHOLD", 70)
        high = getattr(self.settings, "BIG_FIXER_HIGH_THRESHOLD", 95)
        project_name = project_context.get("project_name") or project_context.get("name") or "project"

        total = swarm_summary.total if hasattr(swarm_summary, "total") else (swarm_summary.passed + swarm_summary.failed)
        passed = swarm_summary.passed
        failed = swarm_summary.failed

        # Collection error detection: total <= 1 or install_hard_fail
        # total == 0: pytest collected nothing (import error, syntax error, etc.)
        # total == 1: pytest collected only 1 test — anomaly indicating partial collection
        collection_error = (total <= 1) or bool(getattr(swarm_summary, "install_hard_fail", False))

        if collection_error:
            logger.info("ADR-030 router: collection error detected — routing directly to Big Fixer")
            swarm_summary, repair_history = await self._run_big_fixer(
                swarm_summary=swarm_summary,
                export_dir=export_dir,
                repair_history=repair_history,
                project_context=project_context,
                session_id=session_id,
                escalate_to_contract_fired=escalate_to_contract_fired,
            )
            return swarm_summary

        pass_rate = (passed / total * 100) if total > 0 else 0.0
        logger.info(f"ADR-030 router: pass_rate={pass_rate:.1f}% ({passed}/{total}), low={low}%, high={high}%")

        if pass_rate < low:
            logger.warning(f"ADR-030 router: pass_rate {pass_rate:.1f}% < LOW_THRESHOLD {low}% — emitting user_guided_debug_state (DUMP)")
            await self._broadcast("user_guided_debug_state", {
                "project_name": project_name,
                "tests_still_failing": failed,
                "tests_passing": passed,
                "repair_rounds": len(repair_history),
                "repair_history": repair_history,
                "message": (
                    f"Pass rate {pass_rate:.1f}% is below the Big Fixer threshold ({low}%). "
                    f"Failures likely indicate contract-level design flaws. See repair_history for Giant Brain attempts."
                ),
                "export_dir": str(export_dir),
                "session_id": session_id,
            })
            return swarm_summary

        if pass_rate >= high:
            logger.info(f"ADR-030 router: pass_rate {pass_rate:.1f}% ≥ HIGH_THRESHOLD {high}% — running Fix #3 geared up")
            swarm_summary = await self._run_fix3(
                swarm_summary=swarm_summary,
                export_dir=export_dir,
                repair_history=repair_history,
                project_context=project_context,
                session_id=session_id,
            )
            if swarm_summary.failed == 0:
                logger.info("ADR-030 router: Fix #3 resolved all failures")
                return swarm_summary
            logger.info("ADR-030 router: Fix #3 did not resolve all failures — routing to Big Fixer")

        swarm_summary, repair_history = await self._run_big_fixer(
            swarm_summary=swarm_summary,
            export_dir=export_dir,
            repair_history=repair_history,
            project_context=project_context,
            session_id=session_id,
            escalate_to_contract_fired=escalate_to_contract_fired,
        )
        return swarm_summary

    async def _run_fix3(
        self,
        swarm_summary,
        export_dir: Path,
        repair_history: list,
        project_context: dict,
        session_id: str,
    ):
        """
        ADR-030 Fix #3 — Giant Brain pass 3 with history injection and per-file cap lifted.

        Reuses existing Giant Brain infrastructure with two modifications:
        1. Previous attempt summaries injected into system prompt (## Previous Attempts section).
        2. Per-file fix cap removed from system prompt.
        """
        logger.info("ADR-030 Fix #3: running Giant Brain pass 3 (geared up)")
        await self._broadcast("repair_round_started", {
            "round": 3,
            "failed_tests": swarm_summary.failed,
            "session_id": session_id,
            "phase": "giant_brain_fix3",
        })

        test_output = self._collect_test_output(swarm_summary)

        # Build previous attempts summary for injection
        prev_attempts_lines = []
        for entry in repair_history:
            r = entry.get("round", "?")
            summary = entry.get("giant_brain_summary", entry.get("note", ""))
            fixes = entry.get("fix_count", 0)
            still_failing = entry.get("tests_failed_after", "?")
            prev_attempts_lines.append(
                f"  Pass {r}: {fixes} fix(es) attempted — outcome: {still_failing} still failing. Summary: {summary}"
            )
        prev_attempts_block = "\n".join(prev_attempts_lines) if prev_attempts_lines else "  (no prior passes recorded)"

        # Collect source tree
        source_tree = self._collect_source_tree(export_dir)
        source_parts = []
        for file_path, content in source_tree.items():
            source_parts.append(f"### {file_path}\n```\n{content}\n```")
        source_block = "\n\n".join(source_parts) if source_parts else "(no source files found)"

        system_prompt = (
            "You are a senior software engineer performing a post-build audit (pass 3 of 3).\n"
            "You will receive all source files, the full pytest failure output, and a history of prior fix attempts.\n"
            "Your job: identify root causes and emit a structured fix manifest in JSON.\n\n"
            "IMPORTANT — Previous fixes have already been attempted and failed. Do NOT repeat them.\n"
            "Look for a different root cause or a more complete fix.\n\n"
            "Rules:\n"
            "- Read ALL source files before forming any opinion.\n"
            "- Read the FULL test failure output, not just the first error.\n"
            "- Emit ONLY valid JSON — no narrative text, no markdown, no code fences.\n"
            "- NEVER suggest rewriting or regenerating test files — tests are read-only input.\n"
            "- If tests appear to have unrealistic expectations, set \"escalate\": true and explain in summary.\n"
            "- Every fix must have \"complexity\": \"mechanical\" or \"architectural\".\n"
            "  mechanical = rename, add import, fix signature, wire export, add missing class, fix file content type,\n"
            "    change a string literal or error message text, fix a return value in a single function, add/fix a missing symbol.\n"
            "  architectural = changes spanning 2+ files, redesign of a data contract or class hierarchy, add missing feature logic,\n"
            "    fix a fundamental algorithmic error. Single-file string or value changes are NEVER architectural.\n"
            "- Any change to a function's return TYPE (e.g. str → float, float → str) is architectural.\n"
            "- Error message text fixes (add period, change wording to match test contract) are mechanical.\n"
            "- Sequence mechanical fixes before architectural ones.\n"
            "- When root cause is ambiguous, pick the most conservative fix.\n\n"
            f"## Previous Attempts (do not repeat these — try a different approach)\n{prev_attempts_block}\n\n"
            "Output ONLY this JSON structure (no preamble, no explanation):\n"
            "{\n"
            "  \"summary\": \"brief description of root cause\",\n"
            "  \"escalate\": false,\n"
            "  \"fixes\": [\n"
            "    {\n"
            "      \"file\": \"path/to/file.py\",\n"
            "      \"complexity\": \"mechanical\",\n"
            "      \"description\": \"Human-readable description of what to change\",\n"
            "      \"action\": \"rename_function|add_import|fix_signature|rewrite_exports|add_feature|fix_content|other\"\n"
            "    }\n"
            "  ]\n"
            "}"
        )

        user_prompt = (
            f"## Source Files\n\n{source_block}\n\n"
            f"## Test Failure Output\n\n{test_output}\n\n"
            "Emit the fix manifest JSON now. Output JSON only — no preamble, no markdown, no explanation."
        )

        try:
            raw = await self.ai.complete(
                system=system_prompt,
                message=user_prompt,
                backend="claude-audit",
                json_mode=True,
                thinking_budget=2048,
                max_tokens=8192,
            )
        except Exception as exc:
            logger.error(f"ADR-030 Fix #3 audit LLM call failed: {exc}")
            return swarm_summary

        raw = raw.strip()
        if raw.startswith("```") and raw.endswith("```"):
            lines = raw.splitlines()
            if len(lines) >= 3:
                raw = "\n".join(lines[1:-1]).strip()

        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(f"ADR-030 Fix #3 returned unparseable JSON: {exc}\nRaw: {raw[:500]}")
            return swarm_summary

        if not isinstance(manifest, dict):
            return swarm_summary

        if manifest.get("escalate"):
            logger.warning(f"ADR-030 Fix #3 escalation requested: {manifest.get('summary', '')}")
            return swarm_summary

        fixes = manifest.get("fixes", [])
        logger.info(f"ADR-030 Fix #3: {len(fixes)} fix(es), summary: {manifest.get('summary', '')[:100]}")

        if fixes:
            touched = await self._run_fixer_pass(manifest, export_dir)
            if any("pyproject.toml" in f for f in touched):
                logger.info("Fix #3 touched pyproject.toml — running full dep install")
                await self._reinstall_with_deps(export_dir)
            swarm_summary = await self._rerun_tests(export_dir)

        logger.info(f"ADR-030 Fix #3 complete — {swarm_summary.failed} still failing")
        await self._broadcast("repair_round_complete", {
            "round": 3,
            "phase": "giant_brain_fix3",
            "fix_count": len(fixes),
            "tests_failed": swarm_summary.failed,
            "tests_passed": swarm_summary.passed,
            "session_id": session_id,
        })
        return swarm_summary

    async def _run_big_fixer(
        self,
        swarm_summary,
        export_dir: Path,
        repair_history: list,
        project_context: dict,
        session_id: str,
        escalate_to_contract_fired: bool = False,
    ):
        """
        ADR-030 Big Fixer — last-resort single-model fallback agent (Session 56 rewrite).

        Uses gemini-customtools with native function calling. The model reads files
        via read_export_file, verifies via lint_code / run_code, and writes via
        write_export_file — iteratively, one file at a time. No monolithic JSON blob.

        Tool loop managed inside _gemini_complete (Option A). Big Fixer just passes
        tools= and tool_dispatcher= and gets back final text when done.
        """
        from core.swarm_tools import TOOL_DEFINITIONS, dispatch_tool

        max_attempts = getattr(self.settings, "BIG_FIXER_MAX_ATTEMPTS", 2)
        project_name = (
            project_context.get("project_name")
            or project_context.get("name")
            or "project"
        )
        contract = project_context.get(
            "test_contract", project_context.get("api_summary", "")
        )

        base_dispatch = partial(dispatch_tool, export_dir=str(export_dir))

        # Build a dispatcher bound to this export_dir
        async def _dispatcher(tool_name: str, tool_input: dict) -> dict:
            return await base_dispatch(tool_name, tool_input)

        for attempt in range(1, max_attempts + 1):
            logger.info(
                f"ADR-030 Big Fixer attempt {attempt}/{max_attempts} — "
                f"{swarm_summary.failed} failing (native tools)"
            )
            await self._broadcast("repair_round_started", {
                "round": f"big_fixer_{attempt}",
                "failed_tests": swarm_summary.failed,
                "session_id": session_id,
                "phase": "big_fixer",
            })

            test_output = self._collect_test_output(swarm_summary)

            # History block from previous Giant Brain passes
            history_lines = []
            for entry in repair_history:
                r = entry.get("round", "?")
                summary = entry.get("giant_brain_summary", entry.get("note", ""))
                fixes = entry.get("fix_count", 0)
                still_failing = entry.get("tests_failed_after", "?")
                history_lines.append(
                    f"  Pass {r}: {fixes} fix(es) — {still_failing} still failing. "
                    f"Summary: {summary}"
                )
            history_block = "\n".join(history_lines) if history_lines else "  (none)"

            # Collect full source tree for context
            full_source_tree = self._collect_source_tree(export_dir)
            all_paths = sorted(full_source_tree.keys())
            file_tree_block = "\n".join(f"  {p}" for p in all_paths)

            system_prompt = (
                "You are a last-resort senior software engineer with live tool access.\n\n"
                "Giant Brain has already audited and attempted fixes — the build is still failing.\n"
                "Your job: use your tools to read, diagnose, fix, and verify the remaining failures.\n\n"
                "## Workflow\n"
                "FIRST — check if the failure is a ModuleNotFoundError or ImportError for a "
                "third-party library (e.g. 'No module named sqlalchemy'). If so:\n"
                "  a. Call write_export_file to add the missing package to pyproject.toml dependencies.\n"
                "  b. Immediately call install_package_deps to install it.\n"
                "  c. Call run_test to verify — if tests pass, call declare_done.\n"
                "  This entire flow is 3-4 tool calls. Do NOT read dozens of files first.\n\n"
                "For all other failures:\n"
                "1. Use read_export_files (batch) to read multiple relevant files in ONE call — "
                "e.g. the failing module, its imports, and the test file. This saves rounds.\n"
                "2. Use search_code to find all usages of any class or function whose interface "
                "you plan to change — prevents missed call sites.\n"
                "3. Reason about the root cause using the test failure output and file contents.\n"
                "   - If the error involves an unfamiliar API, deprecated method, or version "
                "mismatch (e.g. 'AttributeError', 'TypeError', 'unexpected keyword argument'), "
                "call web_search BEFORE guessing — search for the exact error message or "
                "'library-name correct-api-name example 2024'. Then call fetch_url on the most "
                "relevant result to read the actual documentation.\n"
                "   - If the error is about a specific library's API (e.g. SQLAlchemy 2.0, "
                "Pydantic v2, FastAPI lifespan), search for its migration guide or current docs.\n"
                "4. Use edit_export_file for targeted fixes (variable names, imports, function "
                "signatures, one-line changes). MUCH more efficient than write_export_file — "
                "you only specify the exact text to find and its replacement.\n"
                "   Only use write_export_file when you need to rewrite an entire file.\n"
                "5. After each edit/write, call run_test on the specific failing test file to "
                "verify your fix works before moving on.\n"
                "6. EVERY TOOL ROUND IS PRECIOUS — batch reads with read_export_files, use "
                "edit_export_file instead of full rewrites, and don't re-read files you've "
                "already seen.\n\n"
                "## Research Tools\n"
                "- web_search(query) — searches the internet for current docs and error solutions\n"
                "- fetch_url(url) — reads a documentation page or GitHub README in full\n"
                "Use these proactively when you are unsure about an API's current signature "
                "or behaviour. A single well-targeted search + fetch is almost always faster "
                "than guessing and iterating.\n"
                "EXCEPTION — Google AI SDK: do NOT web_search for Google AI SDK patterns. "
                "Use the authoritative reference below instead.\n\n"
                f"{_sdk_ref_fixer}\n"
                "## Rules\n"
                "- Do NOT edit test files. They are read-only.\n"
                "- Use ONLY the exact directory paths shown in the File Tree below.\n"
                "- Fix the actual failing logic — do not rename or restructure unless "
                "the failure is specifically an import/naming error.\n"
                "- Call declare_done IMMEDIATELY after a run_test call confirms 0 failures — do not\n"
                "continue reading or writing files after tests pass. You MUST also call\n"
                "declare_done if you have attempted fixes on all identified failing files and\n"
                "cannot make further progress. Specifically: if you have completed 3 or more\n"
                "write_export_file → run_test cycles and tests are still failing, call\n"
                "declare_done(summary='partial fix — N tests remain failing') rather than\n"
                "looping further. Never exhaust tool rounds without calling declare_done.\n\n"
                "## File Tree (use ONLY these paths)\n"
                f"{file_tree_block}"
            )

            user_prompt = (
                f"## Previous Fix Attempts (Giant Brain)\n{history_block}\n\n"
                f"## Test Failure Output\n{test_output}\n\n"
                f"## Test Contract (Phase 0)\n{contract}\n\n"
                "Read the files you need, then fix the failures. "
                "When all fixes are written and tests pass, call declare_done(summary='...')."
            )

            raw = None
            # Session 58: use role chain instead of hardcoded tuple so ROLE_BIG_FIXER_CHAIN
            # setting is actually honoured (was always "gemini-customtools,gemini" before).
            #
            # tool_mode="ANY": function_calling_config mode=ANY forces the model to emit a
            # function call on every single turn — thinking-only is physically impossible at
            # the API level.  declare_done() replaces the old "BIG_FIXER_DONE" text signal.
            # thinkingConfig is omitted entirely when tool_mode="ANY" to avoid the
            # MINIMAL+functionDeclarations HTTP 400 seen on customtools in the S58 prod log.
            _bf_chain = self.ai.get_role_chain("big_fixer")
            for _bf_backend in _bf_chain:
                try:
                    raw = await self.ai.complete(
                        system=system_prompt,
                        message=user_prompt,
                        backend=_bf_backend,
                        max_tokens=65536,
                        tools=TOOL_DEFINITIONS,
                        tool_dispatcher=_dispatcher,
                        tool_mode="ANY",
                        max_tool_rounds=20,  # restored: efficient tools mean more productive rounds
                    )
                    if raw and raw.strip():
                        # Reject LM Studio fallback responses — they are markdown analysis
                        # produced when Claude key isn't configured, never actual code fixes.
                        # The warning prefix is injected by the claude backend fallback path.
                        if raw.startswith("⚠️ *Claude failed"):
                            logger.warning(
                                f"ADR-030 Big Fixer attempt {attempt}: "
                                f"{_bf_backend} fell back to LM Studio (no Claude key) — "
                                f"skipping non-functional response"
                            )
                            raw = None
                            continue
                        logger.info(
                            f"ADR-030 Big Fixer attempt {attempt}: "
                            f"completed via {_bf_backend}"
                        )
                        break
                except ToolExhaustionError:
                    # Finding 2: tool exhaustion ≠ backend failure.
                    # The model DID work (wrote files, ran tests) across all 14 rounds
                    # but never called declare_done.  The right move is to accept partial
                    # work and rerun tests — NOT to retry on the next backend (which would
                    # re-exhaust identically and double the stall time).
                    logger.warning(
                        f"ADR-030 Big Fixer attempt {attempt}: "
                        f"{_bf_backend} exhausted tool rounds — accepting partial work, "
                        f"skipping remaining backends, proceeding to test rerun"
                    )
                    raw = "partial — tool rounds exhausted without declare_done"
                    break  # exit backend chain; proceed to test rerun below
                except Exception as exc:
                    logger.warning(
                        f"ADR-030 Big Fixer attempt {attempt}: "
                        f"{_bf_backend} failed ({exc}) — trying next backend"
                    )

            if not raw:
                logger.error(
                    f"ADR-030 Big Fixer attempt {attempt}: all backends failed"
                )
                repair_history.append({
                    "round": f"big_fixer_{attempt}",
                    "phase": "big_fixer",
                    "note": "all backends failed",
                    "tests_failed_after": swarm_summary.failed,
                })
                continue

            # Log the summary from the model's final response
            logger.info(
                f"ADR-030 Big Fixer attempt {attempt} complete — "
                f"model response: {raw[:200]}"
            )

            # Rerun tests to see if the writes fixed things
            current_summary = await self._rerun_tests(export_dir)
            still_failing = current_summary.failed

            repair_history.append({
                "round": f"big_fixer_{attempt}",
                "phase": "big_fixer",
                "note": raw[:300],
                "tests_failed_after": still_failing,
            })

            await self._broadcast("repair_round_complete", {
                "round": f"big_fixer_{attempt}",
                "tests_failed": still_failing,
                "session_id": session_id,
                "phase": "big_fixer",
            })

            if still_failing == 0:
                logger.info(
                    f"ADR-030 Big Fixer attempt {attempt} — all tests passing ✓"
                )
                return current_summary, repair_history

            logger.info(
                f"ADR-030 Big Fixer attempt {attempt} complete — "
                f"{still_failing} still failing"
            )
            swarm_summary = current_summary

        logger.warning(
            f"ADR-030 Big Fixer exhausted ({max_attempts} attempts) — "
            f"{swarm_summary.failed} still failing"
        )
        return swarm_summary, repair_history

    # ------------------------------------------------------------------
    # Legacy repair loop (ADR-014) — preserved for backward compat
    # Superseded by _run_giant_brain_repair (ADR-023)
    # ------------------------------------------------------------------

    async def _run_repair_loop(
        self,
        swarm_summary,          # SwarmSummary from TesterSwarm — contains the initial failures
        export_dir: Path,
        project_context: dict,
        session_id: str,
    ) -> tuple:
        """
        Gem Pro repair loop (ADR-014).
        Runs up to 3 rounds: audit → repair subtasks → re-test.
        Returns (final_swarm_summary, repair_history).
        repair_history: list of {round, subtask_count, files_touched, test_result_summary}
        """
        from core.orchestrator import ProjectOrchestrator
        from core.tester_swarm import TesterSwarm

        orchestrator = ProjectOrchestrator(self.ai)
        repair_history = []
        current_summary = swarm_summary

        # ADR-016 naming triage removed (Session 51) — see _run_giant_brain_repair
        # for rationale. Go straight to audit rounds.

        for round_num in range(1, 4):  # up to 3 rounds
            if current_summary.failed == 0:
                break  # clean — exit loop early

            await self._broadcast("repair_round_started", {
                "round": round_num,
                "failed_tests": current_summary.failed,
                "session_id": session_id,
            })
            logger.info(f"Repair round {round_num}/3 — {current_summary.failed} failing tests")

            # Collect full source tree
            source_tree = self._collect_source_tree(export_dir)

            # Build test output string from swarm results
            test_output_parts = []
            for result in (current_summary.results if hasattr(current_summary, "results") else []):
                if hasattr(result, "passed") and not result.passed:
                    parts = []
                    if hasattr(result, "file"):
                        parts.append(f"FILE: {result.file}")
                    if hasattr(result, "test_name"):
                        parts.append(f"TEST: {result.test_name}")
                    if hasattr(result, "output"):
                        parts.append(result.output)
                    elif hasattr(result, "stdout"):
                        parts.append(result.stdout)
                    test_output_parts.append("\n".join(parts))
            test_output = "\n\n---\n\n".join(test_output_parts) if test_output_parts else str(current_summary)

            # Gem Pro audit → repair subtasks
            repair_subtasks = await orchestrator.repair_audit(
                source_tree=source_tree,
                test_output=test_output,
                project_context=project_context,
            )

            if not repair_subtasks:
                logger.warning(f"Repair round {round_num}: audit returned 0 subtasks — stopping loop")
                repair_history.append({
                    "round": round_num,
                    "subtask_count": 0,
                    "files_touched": [],
                    "note": "Audit returned no repair subtasks",
                    "tests_failed_after": current_summary.failed,
                })
                break

            # Score each repair subtask
            for subtask in repair_subtasks:
                orchestrator.score_subtask(subtask, project_context)

            # Execute repair subtasks via swarm
            semaphore = asyncio.Semaphore(1)
            repair_results = await asyncio.gather(
                *[
                    self._execute_subtask(
                        s,
                        str(export_dir),
                        semaphore,
                        max_tokens=32768,
                        target_language=self._detect_target_language(self._project_seed_text(project_context)),
                    )
                    for s in repair_subtasks
                ],
                return_exceptions=True,
            )

            files_touched = []
            for r in repair_results:
                if isinstance(r, dict) and r.get("files"):
                    files_touched.extend(r["files"])

            # Re-run TesterSwarm
            swarm = TesterSwarm(ai_backend=self.ai, sse_broadcast=self._sse_broadcast)
            current_summary = await swarm.run(export_dir=str(export_dir))

            round_record = {
                "round": round_num,
                "subtask_count": len(repair_subtasks),
                "files_touched": files_touched,
                "tests_failed_after": current_summary.failed,
                "tests_passed_after": current_summary.passed,
            }
            repair_history.append(round_record)

            await self._broadcast("repair_round_complete", {
                "round": round_num,
                "subtasks_executed": len(repair_subtasks),
                "files_touched": files_touched,
                "tests_failed": current_summary.failed,
                "tests_passed": current_summary.passed,
                "session_id": session_id,
            })
            logger.info(f"Repair round {round_num} complete — {current_summary.failed} still failing")

        return current_summary, repair_history

    async def run_orchestrated_build(self, project_context: dict, task_spec: Any) -> None:
        """
        Run a full orchestrated project build.
        
        Args:
            project_context: The project context dictionary
            task_spec: The original task specification
        """
        try:
            project_context = dict(project_context or {})
            seed_text = self._project_seed_text(project_context)
            target_language = self._detect_target_language(seed_text)

            tech_preferences = project_context.get("tech_preferences")
            if isinstance(tech_preferences, dict):
                tech_preferences = dict(tech_preferences)
            else:
                tech_preferences = {}
            tech_preferences["target_language"] = target_language

            project_context["target_language"] = target_language
            project_context["tech_preferences"] = tech_preferences

            # Determine export directory
            project_slug = (project_context.get("project_name") or project_context.get("name") or "project").lower().replace(" ", "_")
            export_dir = _PROJECT_ROOT / "exports" / project_slug
            export_dir.mkdir(parents=True, exist_ok=True)
            session_id = f"session-{int(time.time())}"

            # ── ADR-041: inject canopy_keys.py so exported apps share the central vault ──
            # Copy core/canopy_keys.py into the project root so generated code can use
            # `from canopy_keys import get_key, require_key` without implementing its own
            # key storage.  The copy is idempotent — safe to re-run on resume.
            try:
                import shutil as _shutil
                _ck_src = _PROJECT_ROOT / "core" / "canopy_keys.py"
                _ck_dst = export_dir / "canopy_keys.py"
                if _ck_src.exists() and not _ck_dst.exists():
                    _shutil.copy2(_ck_src, _ck_dst)
                    logger.info("Injected canopy_keys.py into %s", export_dir)
            except Exception as _ck_exc:
                logger.warning("Could not inject canopy_keys.py: %s", _ck_exc)

            # Broadcast build start
            await self._broadcast("build_started", {
                "project_name": project_context.get("project_name") or project_context.get("name") or "project",
                "export_dir": str(export_dir),
                "session_id": session_id,
            })
            logger.info(f"Orchestrated build started: {project_context.get('project_name')} -> {export_dir}")

            # Create orchestrator and decompose
            if self.ai is None:
                raise RuntimeError("AI backend not available for orchestration")

            swarm_memory = SwarmMemory()
            swarm_memory.seed(self._orchestrator_scaffolding_rules(project_context))

            # ── ADR-032: create orchestration board for this build ────────
            board = None
            board_enabled = (
                getattr(self.settings, "ORCHESTRATION_BOARD_ENABLED", True)
                if self.settings is not None else True
            )
            if board_enabled:
                try:
                    from core.orchestration_board import OrchestrationBoard
                    max_nudges = (
                        getattr(self.settings, "ORCHESTRATION_BOARD_MAX_NUDGES", 5)
                        if self.settings is not None else 5
                    )
                    board = OrchestrationBoard(build_id=session_id, max_nudges=max_nudges)
                    logger.info("OrchestrationBoard created for build %s", session_id)
                except Exception as board_init_exc:
                    logger.warning(f"OrchestrationBoard init failed (continuing without board): {board_init_exc}")

            # Phase 0 — Contract generator + frozen SwarmMemory

            # ── Stale-artifact cleanup (rebuild guard) ────────────────────────
            # export_dir is keyed on project slug, so a rebuild reuses the same
            # directory.  Without cleanup, pytest discovers *all* test files ever
            # written there (rglob "*.py"), including ones from previous builds
            # whose API shapes no longer exist — causing phantom failures.
            #
            # What we wipe:
            #   1. tests/  — all old test files; the new contract is about to replace them.
            #   2. <pkg>/  — the main source package dir (same name as project slug);
            #      stale renamed/deleted modules would otherwise shadow new ones.
            #   3. .pytest_cache/ and __pycache__/ — stale pyc/cache artifacts.
            # What we KEEP:
            #   - pyproject.toml, canopy_keys.py, static/, any non-Python assets.
            import shutil as _shutil_cleanup

            _tests_dir_pre = export_dir / "tests"
            if _tests_dir_pre.exists():
                # Nuke the entire tests/ directory — not just *.py files.
                # Previous approach left tests/__pycache__/ with stale .pyc
                # bytecode that could shadow the new contract test file when
                # both have the same filename (e.g. battery_datasheet_scanner_test.py).
                # This was a recurring source of phantom failures.
                try:
                    _shutil_cleanup.rmtree(_tests_dir_pre)
                    logger.info("Rebuild cleanup: removed entire stale tests/ directory")
                except Exception as _e:
                    logger.warning("Rebuild cleanup: could not remove tests/: %s", _e)
                    # Fallback: at least remove individual files
                    for _old_tf in list(_tests_dir_pre.glob("*.py")) + list(_tests_dir_pre.glob("*.spec.*")) + list(_tests_dir_pre.glob("*.test.*")):
                        try:
                            _old_tf.unlink()
                        except Exception:
                            pass

            # Wipe source package dir (same name as the project slug) if it exists.
            # Keeps the export_dir root intact (pyproject.toml, canopy_keys.py, etc.)
            _pkg_dir = export_dir / project_slug
            if _pkg_dir.exists() and _pkg_dir.is_dir():
                try:
                    _shutil_cleanup.rmtree(_pkg_dir)
                    logger.info("Rebuild cleanup: removed stale package dir %s/", project_slug)
                except Exception as _e:
                    logger.warning("Rebuild cleanup: could not remove package dir %s: %s", _pkg_dir, _e)

            # Wipe ALL .pytest_cache and __pycache__ dirs recursively.
            # Previously only cleaned top-level; nested caches (tests/__pycache__/,
            # pkg/submod/__pycache__/) survived and caused stale bytecode imports.
            for _cache_dir in export_dir.rglob("__pycache__"):
                try:
                    _shutil_cleanup.rmtree(_cache_dir, ignore_errors=True)
                except Exception:
                    pass
            for _cache_dir in export_dir.rglob(".pytest_cache"):
                try:
                    _shutil_cleanup.rmtree(_cache_dir, ignore_errors=True)
                except Exception:
                    pass
            # ─────────────────────────────────────────────────────────────────

            contract = await self._generate_contract(project_context)
            tests_dir = export_dir / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            contract_path = tests_dir / contract.test_file_name
            contract_path.write_text(contract.test_file_content, encoding="utf-8")
            swarm_memory.freeze_contract(contract)
            logger.info(
                "Phase 0 complete — contract frozen: %s, %s chars API summary, %d routes, %d env_vars",
                contract.test_file_name,
                len(contract.public_api_summary),
                len(contract.routes),
                len(contract.env_vars),
            )

            orchestrator_context = self._with_frozen_contract_instruction(project_context)
            
            orchestrator = ProjectOrchestrator(self.ai)
            subtasks = await orchestrator.decompose_and_score(orchestrator_context)

            filtered_subtasks: List[OrchestratorSubTask] = []
            for subtask in subtasks:
                targets = subtask.target_files or []
                if swarm_memory.is_contract_frozen() and any(self._is_test_file_target(target) for target in targets):
                    logger.warning(
                        "Phase 1 dispatch: skipping test-file subtask '%s' — contract is frozen",
                        subtask.title,
                    )
                    continue
                filtered_subtasks.append(subtask)
            subtasks = filtered_subtasks

            logger.info(f"Decomposed into {len(subtasks)} subtasks")

            # Estimate cost
            judge_results = [s.judge_result for s in subtasks if s.judge_result]
            cost_info = estimate_cost(judge_results) if judge_results else {}

            # Count tiers
            tier_counts = {}
            for subtask in subtasks:
                if subtask.judge_result:
                    tier = subtask.judge_result.tier
                    tier_counts[tier] = tier_counts.get(tier, 0) + 1

            # Broadcast preflight estimate
            await self._broadcast("preflight_estimate", {
                "task_count": len(subtasks),
                "tier_counts": tier_counts,
                "estimated_cost": cost_info.get("total_cost", 0),
                "total_tokens": cost_info.get("total_tokens", 0),
            })
            logger.info(f"Preflight: {len(subtasks)} tasks, cost={cost_info.get('total_cost', 0)}, tokens={cost_info.get('total_tokens', 0)}")

            # Execute subtasks by tier so later tiers can consume shared SwarmMemory
            subtasks_by_tier: Dict[int, List[OrchestratorSubTask]] = {1: [], 2: [], 3: []}
            for subtask in subtasks:
                tier_level = self._tier_level_for_subtask(subtask)
                if tier_level not in subtasks_by_tier:
                    tier_level = 3
                subtasks_by_tier[tier_level].append(subtask)

            semaphore = asyncio.Semaphore(1)
            results: List[dict] = []

            for tier_level in (1, 2, 3):
                tier_subtasks = subtasks_by_tier.get(tier_level, [])
                if not tier_subtasks:
                    continue

                context_prefix = swarm_memory.context_for_tier(tier_level) if tier_level > 1 else ""
                for subtask in tier_subtasks:
                    existing_description = subtask.description or ""
                    subtask.description = self._compose_subtask_description(
                        description=existing_description,
                        raw_context=context_prefix,
                        memory=swarm_memory,
                    )

                for subtask in tier_subtasks:
                    result = await self._execute_subtask(
                        subtask,
                        str(export_dir),
                        semaphore,
                        target_language=target_language,
                        board=board,
                    )
                    results.append(result)

                    if isinstance(result, dict) and result.get("status") == "done":
                        self._publish_subtask_manifests(
                            memory=swarm_memory,
                            subtask=subtask,
                            execution_result=result,
                            export_dir=export_dir,
                            tier_level=tier_level,
                        )

            # Count outcomes
            tasks_done = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "done")
            tasks_error = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "error")

            # Phase 1 — dependency completeness scan before first test run.
            # Catches the common case where subtasks import a package (fastapi, uvicorn,
            # etc.) that the decompose step forgot to list in pyproject.toml dependencies.
            _deps_added = self._auto_fix_missing_deps(export_dir)
            if _deps_added:
                logger.info("Import scan added missing deps — running full dep install before tests")
                await self._reinstall_with_deps(export_dir)

            # Phase 1 — Tester Swarm
            from core.tester_swarm import TesterSwarm

            swarm = TesterSwarm(ai_backend=self.ai, sse_broadcast=self._sse_broadcast)
            swarm_summary = await swarm.run(export_dir=str(export_dir))

            if bool(getattr(swarm_summary, "install_hard_fail", False)):
                await self._broadcast("user_guided_debug_state", {
                    "project_name": project_context.get("project_name") or project_context.get("name") or "project",
                    "tests_still_failing": swarm_summary.failed,
                    "tests_passing": swarm_summary.passed,
                    "repair_rounds": 0,
                    "repair_history": [],
                    "message": "Install hard fail — packaging must be fixed before test diagnostics are meaningful.",
                    "export_dir": str(export_dir),
                    "session_id": session_id,
                })
                logger.warning("Install hard fail after Phase 1 — emitted user_guided_debug_state and stopped build")
                return

            # ADR-023 — Giant Brain Repair (naming triage + up to 2 Giant Brain passes)
            repair_history: List[dict] = []
            if swarm_summary.failed > 0 and self.ai is not None:
                swarm_summary, repair_history = await self._run_giant_brain_repair(
                    swarm_summary=swarm_summary,
                    export_dir=export_dir,
                    project_context=project_context,
                    session_id=session_id,
                    memory=swarm_memory,
                )

                # If still failing after Giant Brain passes, route via ADR-030 three-tier threshold
                if swarm_summary.failed > 0:
                    swarm_summary = await self._post_giant_brain_router(
                        swarm_summary=swarm_summary,
                        export_dir=export_dir,
                        repair_history=repair_history,
                        project_context=project_context,
                        session_id=session_id,
                    )
                    if swarm_summary is None or swarm_summary.failed > 0:
                        # Router emitted user_guided_debug_state internally
                        return

            # Post-smoke cleanup: if smoke is clean, remove duplicate .jsx stubs
            # when an equivalent .tsx file exists in the same directory.
            dedup_removed_files: List[str] = []
            if swarm_summary.failed == 0:
                dedup_removed_files = self._dedup_jsx_when_tsx_exists(export_dir)
                if dedup_removed_files:
                    await self._broadcast("postbuild_dedup_complete", {
                        "removed_files": dedup_removed_files,
                        "reason": "Removed .jsx duplicates when sibling .tsx exists",
                    })

            # Phase 2 — Auto-Fix Loop / Giant Brain review
            from core.auto_fix import AutoFixLoop
            fixer = AutoFixLoop(ai_backend=self.ai, sse_broadcast=self._sse_broadcast)
            fix_summary = await fixer.run(
                swarm_summary=swarm_summary,
                export_dir=str(export_dir),
                session_id=session_id,
            )

            await self._inject_missing_dependencies(export_dir, session_id)

            # Broadcast build complete
            await self._broadcast("build_complete", {
                "project_name": project_context.get("project_name") or project_context.get("name") or "project",
                "tasks_done": tasks_done,
                "tasks_error": tasks_error,
                "export_dir": str(export_dir),
                "tests_total": swarm_summary.total,
                "tests_passed": swarm_summary.passed,
                "tests_failed": swarm_summary.failed,
                "dedup_removed": len(dedup_removed_files),
                "fixes_applied": fix_summary.fixed if fix_summary else 0,
                "fixes_failed": fix_summary.failed if fix_summary else 0,
                "giant_brain_review": fix_summary.giant_brain_review if fix_summary else "",
            })
            logger.info(f"Build complete: {tasks_done} done, {tasks_error} errors, exported to {export_dir}")

            # ── Post-build smoke test (Manager autonomous debug loop) ─────────
            # Trigger only for Python web apps where all tests passed (or nearly).
            # Launches the app, runs the Manager agent in autonomous mode to probe
            # endpoints and catch runtime issues that unit tests can't catch.
            _is_web_app = target_language == "Python" and any(
                (export_dir / project_slug / f).exists()
                for f in ("main.py", "app.py", "server.py")
            )
            _smoke_threshold = 0.70  # run smoke if ≥70% of tests passed
            _pass_rate = (
                swarm_summary.passed / max(swarm_summary.total, 1)
            )
            if _is_web_app and _pass_rate >= _smoke_threshold and self._sse_broadcast:
                asyncio.ensure_future(
                    self._post_build_smoke_test(
                        export_dir=export_dir,
                        project_slug=project_slug,
                        session_id=session_id,
                    )
                )
                logger.info(
                    "Post-build smoke test scheduled for %s (pass_rate=%.0f%%)",
                    project_slug, _pass_rate * 100,
                )

        except Exception as exc:
            logger.error(f"Orchestrated build failed: {exc}", exc_info=True)
            await self._broadcast("build_error", {
                "error": str(exc),
            })
            raise

    async def _post_build_smoke_test(
        self,
        export_dir: Path,
        project_slug: str,
        session_id: str,
    ) -> None:
        """
        Auto-triggered after a successful build: launch the app and run the
        Manager agent in autonomous mode to catch runtime issues before the
        user ever opens the app.

        Covers the most common post-build failure classes:
          - startup crashes (missing deps, import errors)
          - broken routes (404s on /api endpoints)
          - wrong SDK (google.generativeai vs google.genai)
          - JS console errors on page load

        Capped at 10 tool rounds to prevent runaway.  Results are broadcast
        via SSE so the dashboard can surface them.
        """
        logger.info("Post-build smoke test starting for %s", project_slug)
        await self._broadcast("smoke_test_started", {
            "project_slug": project_slug,
            "session_id": session_id,
        })

        try:
            from core.manager_tools import ManagerToolDispatcher, MANAGER_TOOL_DEFINITIONS

            # We need a DashboardAPI reference to reuse launch logic.
            # The SSE broadcast callable is on self — use it to find the server instance.
            # If there's no api_server reference, skip (headless builds without hub).
            api_server = getattr(self, '_api_server_ref', None)
            if api_server is None:
                logger.info("Post-build smoke test: no api_server reference — skipping launch")
                return

            dispatcher = ManagerToolDispatcher(
                app_dir=project_slug,
                app_path=export_dir,
                api_server=api_server,
                exports_dir=export_dir.parent,
            )

            # Build file tree so smoke test knows exact paths
            _smoke_tree_lines = []
            _scount = 0
            for fp in sorted(export_dir.rglob('*')):
                if fp.is_file() and not fp.name.startswith('.') and '__pycache__' not in str(fp):
                    rel = str(fp.relative_to(export_dir)).replace('\\', '/')
                    _smoke_tree_lines.append(rel)
                    _scount += 1
                    if _scount >= 40:
                        _smoke_tree_lines.append('... (truncated)')
                        break
            _smoke_file_tree = ''
            if _smoke_tree_lines:
                _smoke_file_tree = (
                    '\n\n## File tree (use these EXACT paths with read_file / write_file)\n```\n'
                    + '\n'.join(_smoke_tree_lines) + '\n```\n'
                )

            # The smoke test system prompt is more directive than the chat prompt —
            # it has a specific checklist to run, not an open-ended conversation.
            SMOKE_SYSTEM = f"""You are Canopy Manager running an automated post-build smoke test for **{project_slug}**.
{_smoke_file_tree}
Work through this checklist autonomously using your tools:

1. Call restart_app to launch the app. Check startup_logs for ModuleNotFoundError or tracebacks.
2. Call probe_endpoint(GET, /) — should return 200 (static frontend).
3. For each API route you can infer from the route manifest or source code, call probe_endpoint to verify it exists (200 or 422 is fine, 404/500 is a bug).
4. Call screenshot_app("/") to visually verify the frontend loads correctly.
5. Call get_console_logs("/") to check for JavaScript errors.
6. If you find bugs: read_file → write_file → restart_app → re-probe.
7. Call declare_done with a summary of what you found and fixed.

Keep it focused: probe the most critical routes, take one screenshot, check console logs.
Cap yourself at 8 tool calls to stay efficient."""

            _declare_done_args = {}

            async def _smoke_dispatcher(tool_name: str, tool_args: dict):
                result = await dispatcher.dispatch(tool_name, tool_args)
                if tool_name == "declare_done":
                    _declare_done_args.update(tool_args)
                return result

            summary_text = await self.ai.complete(
                backend="gemini-customtools",
                system=SMOKE_SYSTEM,
                message="Run the post-build smoke test now.",
                history=[],
                max_tokens=16384,
                tools=MANAGER_TOOL_DEFINITIONS,
                tool_dispatcher=_smoke_dispatcher,
                tool_mode="AUTO",
                max_tool_rounds=10,  # enforce the "cap at 8 calls" hint
            )

            fix_files = [
                {"path": p, "content": c}
                for p, c in dispatcher._written_files.items()
            ]

            await self._broadcast("smoke_test_complete", {
                "project_slug": project_slug,
                "session_id": session_id,
                "summary": _declare_done_args.get("summary") or summary_text or "Smoke test complete.",
                "needs_human_review": _declare_done_args.get("needs_human_review", False),
                "fix_files": fix_files,
                "fixes_applied": len(fix_files),
            })
            logger.info(
                "Post-build smoke test complete for %s: %d fix(es) applied",
                project_slug, len(fix_files),
            )

        except Exception as exc:
            logger.warning("Post-build smoke test failed for %s: %s", project_slug, exc)
            await self._broadcast("smoke_test_error", {
                "project_slug": project_slug,
                "session_id": session_id,
                "error": str(exc),
            })

    def _dedup_jsx_when_tsx_exists(self, export_dir: Path) -> List[str]:
        """Remove .jsx files if a sibling .tsx file with the same base name exists."""
        removed_files: List[str] = []
        export_path = Path(export_dir)

        for jsx_path in export_path.rglob("*.jsx"):
            tsx_path = jsx_path.with_suffix(".tsx")
            if not tsx_path.exists():
                continue
            try:
                jsx_path.unlink()
                removed_files.append(str(jsx_path))
                logger.info(f"Post-build dedup removed duplicate JSX file: {jsx_path}")
            except Exception as exc:
                logger.warning(f"Post-build dedup failed to remove {jsx_path}: {exc}")

        return removed_files

    async def _inject_missing_dependencies(self, export_dir: Path, session_id: str = "") -> None:
        """Best-effort dependency injection for generated JS/TS projects."""
        package_json_path = Path(export_dir) / "package.json"
        if not package_json_path.exists():
            return

        try:
            package_data = json.loads(package_json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Dependency injection skipped (invalid package.json): {exc}")
            return

        dependencies = package_data.get("dependencies")
        if not isinstance(dependencies, dict):
            dependencies = {}

        dev_dependencies = package_data.get("devDependencies")
        if not isinstance(dev_dependencies, dict):
            dev_dependencies = {}

        package_data["dependencies"] = dependencies
        package_data["devDependencies"] = dev_dependencies

        builtins = {
            "fs", "path", "os", "crypto", "http", "https", "url", "util", "events",
            "stream", "buffer", "child_process", "process", "assert", "querystring", "zlib",
        }

        def normalize_import_spec(import_spec: str) -> Optional[str]:
            spec = (import_spec or "").strip()
            if not spec:
                return None
            if spec.startswith("node:"):
                spec = spec[len("node:"):]
            if spec.startswith(".") or spec.startswith("/"):
                return None
            if spec.startswith("@"):
                if spec.startswith("@/"):
                    return None
                parts = spec.split("/")
                if len(parts) >= 2:
                    return "/".join(parts[:2])
                return spec
            return spec.split("/")[0]

        import_from_pattern = re.compile(r"import\s+(?:[^\n;]*?\s+from\s+)?['\"]([^'\"]+)['\"]")
        require_pattern = re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)")

        found_packages = set()
        for file_path in Path(export_dir).rglob("*"):
            if not file_path.is_file() or file_path.suffix.lower() not in {".js", ".ts", ".jsx", ".tsx"}:
                continue
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                logger.debug(f"Dependency scan skipped unreadable file {file_path}: {exc}")
                continue

            for match in import_from_pattern.finditer(source):
                normalized = normalize_import_spec(match.group(1))
                if normalized and normalized not in builtins:
                    found_packages.add(normalized)

            for match in require_pattern.finditer(source):
                normalized = normalize_import_spec(match.group(1))
                if normalized and normalized not in builtins:
                    found_packages.add(normalized)

        existing_packages = set(dependencies.keys()) | set(dev_dependencies.keys())
        missing_packages = sorted(pkg for pkg in found_packages if pkg not in existing_packages)

        if not missing_packages:
            return

        for package_name in missing_packages:
            dependencies[package_name] = "*"

        try:
            package_json_path.write_text(
                json.dumps(package_data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"Dependency injection failed to write package.json: {exc}")
            return

        logger.info(f"Auto-injected dependencies: {', '.join(missing_packages)}")

        if not session_id:
            return

        try:
            from core.master_changelog import MasterChangelog

            changelog = MasterChangelog()
            await changelog.log_agent_action(
                session_id=session_id,
                agent_name="AgentPool",
                file_path=str(package_json_path),
                action=f"AUTO-INJECTED DEPS: {', '.join(missing_packages)}",
                diff="",
                complexity_score=0,
                risk_flags="",
            )
        except Exception as exc:
            logger.warning(f"Failed to append dependency injection changelog entry: {exc}")

    async def _run_task(self, record: TaskRecord, agent: AgentConfig, task: Any) -> None:
        """Execute the task using the AI backend and update the record."""
        try:
            record.status = "running"
            await self._broadcast("task_started", {
                "task_id":   record.id,
                "agent_id":  agent.id,
                "agent_name": agent.name,
                "description": record.description,
            })

            description   = record.description
            context_str   = ""
            
            # Extract context from task
            context = getattr(task, 'context', None) or {}
            project_context = context.get('project_context') or context

            # Check if this is a full orchestrated build (has project_context with project_name)
            _pname = project_context.get('project_name') or project_context.get('name')
            if project_context and isinstance(project_context, dict) and _pname:
                # Full orchestrated build
                await self.run_orchestrated_build(project_context, task)
                output = f"Orchestrated build complete for: {_pname}"
            else:
                # Fallback: simple single-shot completion (existing behavior)
                if hasattr(task, "context") and task.context:
                    context_str = "\n\nProject context:\n" + json.dumps(task.context, indent=2)

                if self.ai is not None:
                    system = (
                        f"You are a {agent.name} build agent using {agent.model_id}. "
                        "Execute the requested task and report your output clearly."
                    )
                    output = await self.ai.complete(
                        backend="claude",
                        system=system,
                        message=description + context_str,
                    )
                else:
                    output = f"[no AI backend] Task received: {description}"

            record.output      = output
            record.status      = "done"
            record.finished_at = time.time()
            await self._broadcast("task_done", {
                "task_id":  record.id,
                "agent_id": agent.id,
                "agent_name": agent.name,
                "output":   output,
                "finished_at": record.finished_at,
            })

        except Exception as exc:
            logger.error(f"Task {record.id} failed: {exc}", exc_info=True)
            record.output      = f"Error: {exc}"
            record.status      = "error"
            record.finished_at = time.time()
            await self._broadcast("task_error", {
                "task_id":  record.id,
                "agent_id": agent.id,
                "error":    str(exc),
            })
        finally:
            agent.status = "idle"

    # ── Task queries ──────────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> Optional[dict]:
        rec = self._tasks.get(task_id)
        return rec.to_dict() if rec else None

    def get_active_tasks(self) -> List[dict]:
        return [r.to_dict() for r in self._tasks.values()]

    # ── Ollama helpers ────────────────────────────────────────────────────────

    async def list_ollama_models(self) -> List[str]:
        """Return locally available Ollama models (best-effort)."""
        import aiohttp
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get("http://localhost:11434/api/tags", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        data = await r.json()
                        return [m["name"] for m in data.get("models", [])]
        except Exception:
            pass
        return []

    # ── Auto-registration ─────────────────────────────────────────────────────

    def auto_register_from_settings(self) -> None:
        """
        Register agents based on available API keys in Settings.
        Called once at server startup so the pool is never empty.
        """
        if self.settings is None:
            return

        registered = 0

        # Claude
        api_key = getattr(self.settings, "ANTHROPIC_API_KEY", "")
        model   = getattr(self.settings, "CLAUDE_MODEL", "claude-sonnet-4-6")
        if api_key:
            short = model.split("/")[-1].split(":")[0]
            cfg = AgentConfig(
                id=f"claude-{short}-auto",
                name="Claude",
                agent_type=AgentType.CLAUDE,
                endpoint="",
                model_id=model,
                capabilities=["code", "architecture", "reasoning", "chat"],
            )
            self.register_agent(cfg)
            registered += 1

        # Gemini
        api_key = getattr(self.settings, "GOOGLE_API_KEY", "")
        model   = getattr(self.settings, "GOOGLE_GEMINI_MODEL", "gemini-2.0-flash")
        if api_key:
            short = model.split("/")[-1].split(":")[0]
            cfg = AgentConfig(
                id=f"gemini-{short}-auto",
                name="Gemini",
                agent_type=AgentType.GEMINI,
                endpoint="",
                model_id=model,
                capabilities=["code", "reasoning", "chat"],
            )
            self.register_agent(cfg)
            registered += 1

        # LM Studio
        lmstudio_url = getattr(self.settings, "LMSTUDIO_BASE_URL", "")
        lm_model     = getattr(self.settings, "LMSTUDIO_MODEL", "")
        if lmstudio_url and lm_model and lm_model != "your-local-model-name":
            short = lm_model.split("/")[-1].split(":")[0]
            cfg = AgentConfig(
                id=f"lmstudio-{short}-auto",
                name="LM Studio",
                agent_type=AgentType.LMSTUDIO,
                endpoint=lmstudio_url,
                model_id=lm_model,
                capabilities=["code", "chat"],
            )
            self.register_agent(cfg)
            registered += 1

        if registered == 0:
            logger.warning("AgentPool: no API keys configured — pool is empty.")
        else:
            logger.info(f"AgentPool: auto-registered {registered} agent(s) from settings.")
