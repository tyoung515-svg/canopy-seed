"""
Project orchestrator for decomposition + complexity scoring.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from core.complexity_judge import JudgeResult, judge_task_static

logger = logging.getLogger(__name__)

ORCHESTRATOR_SYSTEM = (
    "You are a software architecture orchestrator. Decompose a project spec into "
    "independent engineering tasks. Return strictly valid JSON only. "
    "IMPORTANT: A test file has already been generated and written to the tests/ directory "
    "as the frozen contract for this project. Do NOT include any subtask that writes or "
    "modifies test files. All implementation subtasks must build source code that satisfies "
    "the existing tests."
)

REPAIR_AUDIT_SYSTEM = (
    "You are a software architecture auditor reviewing a project that failed its tests. "
    "Given the full source tree and test output, identify which files need to be fixed or "
    "regenerated and produce a repair plan as structured JSON subtasks."
)

VALID_DOMAIN_LAYERS = {"backend", "frontend", "tests", "config", "docs"}

DECOMPOSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subtasks"],
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "description",
                    "target_files",
                    "domain_layer",
                    "ambiguity_score",
                    "dependency_score",
                    "tier",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "target_files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "domain_layer": {
                        "type": "string",
                        "enum": sorted(VALID_DOMAIN_LAYERS),
                    },
                    "ambiguity_score": {"type": "integer"},
                    "dependency_score": {"type": "integer"},
                    "tier": {"type": "integer"},
                    "notes": {"type": "string"},
                },
            },
        }
    },
}

REPAIR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subtasks"],
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "description",
                    "target_files",
                    "domain_layer",
                    "ambiguity_score",
                    "dependency_score",
                    "tier",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "target_files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "domain_layer": {
                        "type": "string",
                        "enum": sorted(VALID_DOMAIN_LAYERS),
                    },
                    "ambiguity_score": {"type": "integer"},
                    "dependency_score": {"type": "integer"},
                    "tier": {"type": "integer"},
                    "notes": {"type": "string"},
                },
            },
        }
    },
}


@dataclass
class OrchestratorSubTask:
    title: str
    description: str
    target_files: List[str]
    domain_layer: str
    ambiguity_score: int
    dependency_score: int
    tier: int = 1
    depends_on: List[str] = field(default_factory=list)
    judge_result: Optional[JudgeResult] = field(default=None)
    notes: str = ""

    @property
    def ambiguity(self) -> int:
        return self.ambiguity_score

    @property
    def external_deps(self) -> int:
        return self.dependency_score


class ProjectOrchestrator:
    def __init__(self, ai_backend):
        self.ai = ai_backend

    async def decompose(self, project_context: Dict[str, Any]) -> List[OrchestratorSubTask]:
        prompt = self._build_decompose_prompt(project_context)
        chain = self.ai.get_role_chain("orchestrator")
        last_exc: Optional[Exception] = None
        for backend in chain:
            try:
                logger.info(f"Orchestrator decompose: trying backend '{backend}'")
                raw_response = await self.ai.complete(
                    backend=backend,
                    system=ORCHESTRATOR_SYSTEM,
                    message=prompt,
                    max_tokens=16000,
                    json_mode=True,
                    response_schema=DECOMPOSE_SCHEMA,
                )
                logger.debug(f"Orchestrator raw response (first 500 chars): {raw_response[:500]}")
                subtasks = self._parse_subtasks(raw_response)
                if subtasks:
                    if backend != chain[0]:
                        logger.info(f"Orchestrator decompose: succeeded via fallback '{backend}'")
                    return subtasks
                logger.warning(f"Orchestrator decompose: '{backend}' returned 0 tasks — trying next")
            except Exception as exc:
                logger.warning(f"Orchestrator decompose: '{backend}' failed ({exc}) — trying next")
                last_exc = exc
        logger.error(f"Orchestrator decompose: all backends exhausted (last error: {last_exc})")
        return [self._create_fallback_subtask(project_context)]

    def score_subtask(self, subtask: OrchestratorSubTask, project_context: Dict[str, Any]) -> JudgeResult:
        domain_layers = self._resolve_domain_layers(subtask.domain_layer, project_context)
        result = judge_task_static(
            task_description=subtask.description,
            files=subtask.target_files,
            domain_layers=domain_layers,
            ambiguity_questions=subtask.ambiguity_score,
            external_dep_level=subtask.dependency_score,
        )
        subtask.judge_result = result
        return result

    async def decompose_and_score(self, project_context: Dict[str, Any]) -> List[OrchestratorSubTask]:
        subtasks = await self.decompose(project_context)
        for subtask in subtasks:
            self.score_subtask(subtask, project_context)
        return subtasks

    async def repair_audit(
        self,
        source_tree: Dict[str, str],
        test_output: str,
        project_context: Dict[str, Any],
    ) -> List[OrchestratorSubTask]:
        prompt = self._build_repair_prompt(source_tree, test_output, project_context)
        chain = self.ai.get_role_chain("auditor")
        last_exc: Optional[Exception] = None
        for backend in chain:
            try:
                logger.info(f"Repair audit: trying backend '{backend}'")
                raw_response = await self.ai.complete(
                    backend=backend,
                    system=REPAIR_AUDIT_SYSTEM,
                    message=prompt,
                    max_tokens=32000,
                    json_mode=True,
                    response_schema=REPAIR_SCHEMA,
                )
                logger.debug(f"Repair audit raw response (first 500 chars): {raw_response[:500]}")
                subtasks = self._parse_subtasks(raw_response)
                if subtasks:
                    if backend != chain[0]:
                        logger.info(f"Repair audit: succeeded via fallback '{backend}'")
                    return subtasks
                logger.warning(f"Repair audit: '{backend}' returned 0 subtasks — trying next")
            except Exception as exc:
                logger.warning(f"Repair audit: '{backend}' failed ({exc}) — trying next")
                last_exc = exc
        logger.error(f"Repair audit: all backends exhausted (last error: {last_exc})")
        return []

    def _build_decompose_prompt(self, context: Dict[str, Any]) -> str:
        project_name = context.get("project_name", "Unknown Project")
        description = context.get("description", "")
        output_slug = self._derive_output_slug(context)
        goals = context.get("goals", [])
        constraints = context.get("constraints", [])
        tech_preferences = context.get("tech_preferences", {})

        goals_str = "\n".join(f"- {goal}" for goal in goals) if goals else "- none"
        constraints_str = "\n".join(f"- {item}" for item in constraints) if constraints else "- none"
        tech_str = json.dumps(tech_preferences, ensure_ascii=False) if tech_preferences else "{}"
        schema_block = self._subtask_schema_block()

        return f"""
PROJECT_NAME: {project_name}
DESCRIPTION: {description}
OUTPUT_DIRECTORY_SLUG: {output_slug}

GOALS:
{goals_str}

CONSTRAINTS:
{constraints_str}

TECH_PREFERENCES:
{tech_str}

{schema_block}

WEB APP LAUNCH CONTRACT — these rules apply to every web app build and override any conflicting convention:
W1) ENTRY POINT FILE: The FastAPI entry point MUST be named `main.py` and live at `{output_slug}/main.py`. Never use `app.py`, `run.py`, `server.py`, or any other name. This is the filename the Hub uses to launch the app.
W2) APP OBJECT NAME: The FastAPI instance in `main.py` MUST be named `app` (e.g. `app = FastAPI(...)`). The Hub launches with `uvicorn {output_slug}.main:app --port <PORT>` — any other variable name will cause a launch failure.
W3) RELATIVE URLS IN FRONTEND JS: Every `fetch()` call in `static/app.js` MUST use a relative URL path (e.g. `fetch('/api/chores')`, NOT `fetch('http://localhost:8000/api/chores')`). The launch port is assigned dynamically at runtime and is unknown at build time. Hardcoded ports will break the app completely.
W4) ROUTER BEFORE STATICFILES: `app.include_router(...)` MUST always be called BEFORE `app.mount('/', StaticFiles(...))` in `main.py`. Mounting StaticFiles at `/` first will swallow all API requests.
W5) COMPLETE STATIC FRONTEND: Every web app MUST generate all three static files as separate subtasks: `{output_slug}/static/index.html`, `{output_slug}/static/app.js`, and `{output_slug}/static/style.css`. The `app.js` file must handle all user interactions via `fetch()` — no inline `<script>` logic in the HTML.
W6) PRE-INSTALLED DEPS: `fastapi` and `uvicorn` are already installed in the runtime environment. Do NOT list them in `pyproject.toml` dependencies. Only add genuinely extra third-party packages (e.g. `sqlalchemy`, `aiofiles`, `python-multipart`) that the app actually imports.
W7) STARTUP INITIALISATION: If the app requires DB setup or other startup work, use a FastAPI `@app.on_event("startup")` handler — never run setup code at module import time (it will run twice and can cause errors).

Additional critical rules:
1) CRITICAL: Every file that any component imports must appear as a target_file in at least one subtask. Before finalising the subtask list, scan every planned import statement and verify the imported file exists in the target_files list. If it does not, add a subtask to generate it. Do not reference files that are not in the target_files list of at least one subtask.
2) CRITICAL: The package.json subtask must include every npm package that any generated file will import. Scan all planned import statements for third-party packages before writing the package.json target_file contents. Do not omit any package that is not part of the Node.js standard library.
3) CRITICAL (Python projects only): Every `import` and `from ... import ...` statement in generated test files must reference module paths that actually exist in the generated source tree. Do not invent package names. The package name used in tests must exactly match the directory name created in the project.
4) CRITICAL (Python projects only): When generating `pyproject.toml`, the `[project]` `name` field MUST exactly match `OUTPUT_DIRECTORY_SLUG`.
5) CRITICAL (Python projects only): `pyproject.toml` MUST always include this exact section (substitute `OUTPUT_DIRECTORY_SLUG`):
    [tool.hatch.build.targets.wheel]
    packages = ["OUTPUT_DIRECTORY_SLUG"]
    Do not use auto-discovery for wheel packages.
6) CRITICAL (Python projects only): If a license field is included, it must use `license = {{text = "MIT"}}` — never `license = {{file = "LICENSE"}}`. No LICENSE file will be created.
7) CRITICAL (Python projects only): `pyproject.toml` must NOT reference files that are not explicitly generated in this build. Omit the `readme` field entirely. Do not include `readme` in any form — not as a string, not as a table, not as an inline table. Do not use `license = {{file = ...}}`, changelog files, authors files, or any metadata field that points to files on disk. Use inline forms or omit.
8) CRITICAL: Every subtask object must include a `tier` integer field. Never omit this field.
9) Tier assignment rules:
    - Tier 1: source files with no imports from other project modules
    - Tier 2: source files that import from Tier 1 modules
    - Tier 3: source files that import from Tier 2 or both tiers; all test files are always Tier 3 minimum
    - Config files (`pyproject.toml`, `__init__.py`): assign to the highest tier of any module they reference, or Tier 2 if uncertain
10) If dependency is ambiguous, default to Tier 1.
11) CRITICAL: For every subtask that generates implementation code that will be tested, the `notes` field must encode the expected behavior contract explicitly. Include: (a) the exact error message strings that test assertions match — for example, if tests use `pytest.raises(ValueError, match="Invalid unit name: 'x'")`, the notes must state the expected message verbatim; (b) the expected return type of every public function; (c) any domain-specific symbol or formatting convention the tests assert (for example: if tests assert `"°C"` for Celsius, state "use °C not C"; if tests assert unit abbreviations like `"km"` rather than `"kilometer"`, state this). Do not leave these details for the build agent to infer — write them into the notes.
12) CRITICAL — ASCII ONLY IN GENERATED CODE: All string literals in generated source files must use ASCII characters only. Unicode characters are forbidden in implementation code. Specifically:
    - Temperature symbols: use the plain letter 'C' for Celsius, 'F' for Fahrenheit, 'K' for Kelvin. NEVER use '°C', '℃', or any Unicode degree symbol.
    - Currency and math symbols: use ASCII equivalents ('USD', 'GBP', 'EUR', '%', 'x') not Unicode.
    - If the test contract specifies a symbol (e.g. a unit abbreviation), use that symbol verbatim, but if it contains any character above ASCII 127, raise this as a contract inconsistency in your notes field and use the ASCII equivalent.
    Write this as a hard constraint in the 'notes' field of every implementation subtask: "All string literals must be ASCII (0–127). Use 'C' not '°C', 'F' not '°F', etc."
13) CRITICAL — DEPENDS_ON REQUIRED FOR TIER 2+: Any subtask at Tier 2 or higher that imports from another project module MUST populate `depends_on` with the exact target_file paths of the subtasks it imports from. Example: if `converter.py` imports from `units.py`, the converter subtask must have `depends_on: ["unit_converter/units.py"]`. This enables the build agent to receive the actual file content as context, preventing import errors and API guessing. Never leave depends_on empty for a subtask that has any intra-project imports.

Do not include any extra prose.
""".strip()

    def _build_repair_prompt(
        self,
        source_tree: Dict[str, str],
        test_output: str,
        project_context: Dict[str, Any],
    ) -> str:
        project_name = project_context.get("project_name", "Unknown Project")
        description = project_context.get("description", "")
        output_slug = self._derive_output_slug(project_context)
        schema_block = self._subtask_schema_block()

        # Format source tree — cap each file at 200 lines to stay within token budget
        tree_sections = []
        for rel_path, content in sorted(source_tree.items()):
            lines = content.splitlines()
            if len(lines) > 200:
                truncated = "\n".join(lines[:200]) + f"\n... [{len(lines) - 200} lines truncated]"
            else:
                truncated = content
            tree_sections.append(f"### {rel_path}\n```\n{truncated}\n```")
        tree_str = "\n\n".join(tree_sections) if tree_sections else "(no source files found)"

        # Cap test output at 4000 chars
        if len(test_output) > 4000:
            truncation_suffix = "\n... [truncated]"
            max_prefix = 4000 - len(truncation_suffix)
            if max_prefix > 0:
                test_output = test_output[:max_prefix] + truncation_suffix
            else:
                test_output = test_output[:4000]

        return f"""
PROJECT_NAME: {project_name}
DESCRIPTION: {description}
OUTPUT_DIRECTORY_SLUG: {output_slug}

## Source Tree
{tree_str}

## Test Output (failures)
```
{test_output}
```

## Task
The project above has failing tests. Identify every file that needs to be fixed or fully regenerated.
Produce a repair plan as structured JSON subtasks using the same schema as a normal decomposition.

{schema_block}

WEB APP LAUNCH CONTRACT — also applies during repair:
W1) Entry point MUST be `{output_slug}/main.py` — never `app.py`, `run.py`, or `server.py`.
W2) FastAPI instance in `main.py` MUST be named `app` (launched as `uvicorn {output_slug}.main:app`).
W3) All `fetch()` calls in `static/app.js` MUST use relative URLs (e.g. `/api/route`, not `http://localhost:PORT/api/route`).
W4) `app.include_router(...)` MUST come before `app.mount('/', StaticFiles(...))` in `main.py`.
W5) `fastapi` and `uvicorn` must NOT appear in `pyproject.toml` dependencies — they are pre-installed.

Critical rules:
1) CRITICAL: Only include files that are directly implicated in the test failures above. Do not add new features.
2) CRITICAL: target_files must use exact relative paths that already exist in the source tree, OR new files that are missing imports. Do not invent new file paths.
3) CRITICAL (Python projects): Every import in generated test files must resolve to real module paths in the generated source tree. Package name in tests must exactly match the directory name in the project.
4) CRITICAL: If a file in the source tree ends mid-statement, mid-function, or with a SyntaxError (indicating it was truncated by a prior generation hitting its token limit), the repair subtask for that file MUST include this exact text in its notes field: "CONCISE: Previous version was truncated at token limit. Write a focused version under 150 lines. Cover 8-10 critical test cases only — happy path, key error conditions, and boundary values. Do not write exhaustive edge case tests."
5) CRITICAL (Python projects): If repairing or regenerating `pyproject.toml`, set `[project]` `name` exactly to `OUTPUT_DIRECTORY_SLUG`, and always include:
    [tool.hatch.build.targets.wheel]
    packages = ["OUTPUT_DIRECTORY_SLUG"]
    Do not use auto-discovery for wheel packages.
6) CRITICAL (Python projects): If a license field is included in `pyproject.toml`, it must be `license = {{text = "MIT"}}` and must never use `license = {{file = "LICENSE"}}`. No LICENSE file will be created.
7) CRITICAL (Python projects): `pyproject.toml` must not reference files unless those files are explicitly generated in this build. Omit the `readme` field entirely. Do not include `readme` in any form — not as a string, not as a table, not as an inline table. Do not use `license = {{file = ...}}`, changelog files, authors files, or any metadata field that points to files on disk. Use inline forms or omit.

Do not include any extra prose.
""".strip()

    def _subtask_schema_block(self) -> str:
        return f"""
Return JSON in one of these exact shapes (no markdown fences):
1) {{"subtasks": [ ... ]}}
2) [ ... ]

Each subtask object must contain:
- title: string
- description: string
- target_files: string[]
- depends_on: string[] — list the target_files paths (from other subtasks) that this subtask's files will import. If subtask B imports from subtask A, B.depends_on must include A's target_file path. Leave empty only for Tier 1 (no project imports). Correct cross-module dependency injection requires this field — do not omit it for Tier 2+ subtasks.
- domain_layer: one of {sorted(VALID_DOMAIN_LAYERS)}
- ambiguity_score: integer 0-3
- dependency_score: integer 0-3
- tier: integer (required)
- notes: string (optional)
""".strip()

    def _parse_subtasks(self, raw_response: str) -> List[OrchestratorSubTask]:
        payload = self._extract_json_payload(raw_response)
        if payload is None:
            logger.warning("Could not parse orchestrator response as JSON.")
            return []

        if isinstance(payload, dict):
            items = payload.get("subtasks") or payload.get("tasks") or payload.get("items") or []
        elif isinstance(payload, list):
            items = payload
        else:
            logger.warning(f"Unexpected orchestrator payload type: {type(payload).__name__}")
            return []

        subtasks: List[OrchestratorSubTask] = []
        for item in items:
            parsed = self._parse_single_subtask(item)
            if parsed is not None:
                subtasks.append(parsed)

        return subtasks

    def _extract_json_payload(self, raw_response: str) -> Optional[Any]:
        candidates: List[str] = []
        stripped = raw_response.strip()
        candidates.append(stripped)

        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, flags=re.IGNORECASE)
        if fence_match:
            candidates.append(fence_match.group(1).strip())

        array_match = re.search(r"(\[[\s\S]*\])", stripped)
        if array_match:
            candidates.append(array_match.group(1).strip())

        object_match = re.search(r"(\{[\s\S]*\})", stripped)
        if object_match:
            candidates.append(object_match.group(1).strip())

        for candidate in candidates:
            parsed = self._try_json_load(candidate)
            if parsed is not None:
                return parsed

        return None

    def _try_json_load(self, text: str) -> Optional[Any]:
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            repaired = re.sub(r",\s*([\]}])", r"\1", text)
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                return None

    def _parse_single_subtask(self, item: Any) -> Optional[OrchestratorSubTask]:
        if not isinstance(item, dict):
            return None

        title = str(item.get("title", "Untitled Task")).strip() or "Untitled Task"
        description = str(item.get("description", "")).strip()
        target_files = self._normalize_target_files(item.get("target_files", []))
        domain_layer = self._normalize_domain_layer(item.get("domain_layer", "backend"))
        ambiguity_score = self._clamp_score(item.get("ambiguity_score", item.get("ambiguity", 1)))
        dependency_score = self._clamp_score(item.get("dependency_score", item.get("external_deps", 1)))
        tier = self._parse_tier(item.get("tier", 1))
        depends_on = self._normalize_target_files(item.get("depends_on", []))
        notes = str(item.get("notes", "")).strip()

        if not description:
            description = title

        return OrchestratorSubTask(
            title=title,
            description=description,
            target_files=target_files,
            domain_layer=domain_layer,
            ambiguity_score=ambiguity_score,
            dependency_score=dependency_score,
            tier=tier,
            depends_on=depends_on,
            notes=notes,
        )

    def _parse_tier(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1

    def _normalize_target_files(self, raw_files: Any) -> List[str]:
        if isinstance(raw_files, str):
            raw_files = [raw_files]
        if not isinstance(raw_files, Sequence):
            return []

        normalized: List[str] = []
        for entry in raw_files:
            if not isinstance(entry, str):
                continue
            path = entry.strip().replace("\\", "/")
            if path:
                normalized.append(path)
        return normalized

    def _normalize_domain_layer(self, value: Any) -> str:
        domain = str(value or "backend").strip().lower()
        return domain if domain in VALID_DOMAIN_LAYERS else "backend"

    def _clamp_score(self, value: Any) -> int:
        try:
            num = int(value)
        except (TypeError, ValueError):
            return 1
        return max(0, min(3, num))

    def _resolve_domain_layers(self, domain_layer: str, project_context: Dict[str, Any]) -> int:
        if domain_layer in {"frontend", "backend"}:
            return 2
        if domain_layer == "tests":
            return 2 if project_context.get("tech_preferences") else 1
        return 1

    def _derive_output_slug(self, context: Dict[str, Any]) -> str:
        project_name = context.get("project_name") or context.get("name") or "project"
        return str(project_name).strip().lower().replace(" ", "_")

    def _create_fallback_subtask(self, project_context: Dict[str, Any]) -> OrchestratorSubTask:
        project_name = project_context.get("project_name", "Unknown Project")
        description = project_context.get("description", "Build the requested project")
        summary = f"Build {project_name}: {description}"[:300]

        return OrchestratorSubTask(
            title="Full Project Build",
            description=summary,
            target_files=[],
            domain_layer="backend",
            ambiguity_score=2,
            dependency_score=1,
            notes="Fallback task generated after malformed or failed decomposition response.",
        )
