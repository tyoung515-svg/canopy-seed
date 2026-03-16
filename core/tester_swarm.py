"""
Tester Swarm
Runs discovered pytest files under an export directory, summarizes failures,
and stores per-test outcomes in the master changelog database.
Supports both Python (pytest) and JavaScript/TypeScript (vitest/jest) projects.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import tomllib
except ImportError:
    import tomli as tomllib

import aiosqlite


logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    source_file: str
    test_file: str
    passed: bool
    failure_output: str = ""
    failure_summary: str = ""
    duration_seconds: float = 0.0

    __test__ = False


@dataclass
class SwarmSummary:
    total: int = 0
    passed: int = 0
    failed: int = 0
    duration: float = 0.0
    results: List[TestResult] = field(default_factory=list)
    install_error: str = ""
    install_hard_fail: bool = False

    __test__ = False


class TesterSwarm:
    __test__ = False

    def __init__(self, ai_backend, sse_broadcast=None):
        self.ai = ai_backend
        self._sse_broadcast = sse_broadcast
        self._db_path = Path("memory/master_changelog.db")
        self._editable_install_done: Dict[str, bool] = {}
        self._install_error_by_export: Dict[str, str] = {}
        self._install_hard_fail_by_export: Dict[str, bool] = {}
        self._npm_install_timeout = int(os.getenv("NPM_INSTALL_TIMEOUT", "120"))
        self._js_test_timeout = int(os.getenv("JS_TEST_TIMEOUT", "180"))

    def _purge_import_cache(self, export_path: Path) -> None:
        """Remove stale __pycache__ dirs and .pytest_cache from the export tree.

        Called at the start of every test run so code changes made by repair agents
        between runs are not masked by cached .pyc bytecode or old pytest node IDs.
        """
        for pycache in export_path.rglob("__pycache__"):
            try:
                shutil.rmtree(pycache, ignore_errors=True)
            except Exception:
                pass
        pytest_cache = export_path / ".pytest_cache"
        if pytest_cache.exists():
            try:
                shutil.rmtree(pytest_cache, ignore_errors=True)
            except Exception:
                pass

    async def run(self, export_dir: str) -> SwarmSummary:
        started = time.perf_counter()
        export_path = Path(export_dir)
        export_key = str(export_path.resolve())
        self._install_error_by_export[export_key] = ""
        self._install_hard_fail_by_export[export_key] = False

        # Bug-3 fix: purge stale bytecode and pytest cache before each run so
        # edits made by repair agents between runs are not shadowed by old .pyc files.
        # Also reset install-done flag so pip reinstalls after code changes.
        self._purge_import_cache(export_path)
        self._editable_install_done.pop(export_key, None)

        session_id = str(uuid.uuid4())

        await self._ensure_db()

        language = self._detect_project_language(export_dir)
        if language == "typescript":
            return await self._run_js_tests(export_dir)

        test_files = self._discover_test_files(export_path)
        await self._broadcast("swarm_started", {
            "export_dir": str(export_path),
            "test_count": len(test_files),
            "language": "python",
        })

        if not test_files:
            duration = time.perf_counter() - started
            await self._broadcast("swarm_complete", {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "duration": duration,
            })
            return SwarmSummary(total=0, passed=0, failed=0, duration=duration, results=[])

        install_ok = await self._maybe_install_editable_package(export_path)
        if not install_ok:
            duration = time.perf_counter() - started
            self._install_hard_fail_by_export[export_key] = True
            await self._broadcast("swarm_complete", {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "duration": duration,
                "install_hard_fail": True,
            })
            return SwarmSummary(
                total=0,
                passed=0,
                failed=0,
                duration=duration,
                results=[],
                install_error=self._install_error_by_export.get(export_key, ""),
                install_hard_fail=True,
            )

        semaphore = asyncio.Semaphore(8)
        tasks = [
            asyncio.create_task(
                self._run_single_test(
                    export_path=export_path,
                    test_file=test_file,
                    source_file=self._infer_source_file(test_file),
                    semaphore=semaphore,
                    session_id=session_id,
                )
            )
            for test_file in test_files
        ]

        results = await asyncio.gather(*tasks)

        passed = sum(1 for result in results if result.passed)
        failed = len(results) - passed
        duration = time.perf_counter() - started

        await self._broadcast("swarm_complete", {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "duration": duration,
        })

        return SwarmSummary(
            total=len(results),
            passed=passed,
            failed=failed,
            duration=duration,
            results=results,
            install_error=self._install_error_by_export.get(export_key, ""),
            install_hard_fail=self._install_hard_fail_by_export.get(export_key, False),
        )

    def _detect_project_language(self, export_dir: str) -> str:
        export_path = Path(export_dir)
        if not export_path.exists() or not export_path.is_dir():
            return "python"
        if (export_path / "package.json").exists():
            return "typescript"
        return "python"

    def _discover_test_files(self, export_path: Path) -> List[Path]:
        if not export_path.exists() or not export_path.is_dir():
            return []

        discovered: List[Path] = []
        for file_path in export_path.rglob("*.py"):
            name = file_path.name
            if name.startswith("test_") or name.endswith("_test.py"):
                discovered.append(file_path)

        discovered.sort(key=lambda p: str(p))
        return discovered

    def _discover_js_test_files(self, export_path: Path) -> List[Path]:
        """Find *.test.js/ts/jsx/tsx and *.spec.js/ts/jsx/tsx files, excluding node_modules."""
        if not export_path.exists() or not export_path.is_dir():
            return []
        discovered: List[Path] = []
        js_test_patterns = [
            "*.test.js", "*.test.ts", "*.test.jsx", "*.test.tsx",
            "*.spec.js", "*.spec.ts", "*.spec.jsx", "*.spec.tsx",
        ]
        for pattern in js_test_patterns:
            for file_path in export_path.rglob(pattern):
                # Skip node_modules
                if "node_modules" in file_path.parts:
                    continue
                discovered.append(file_path)
        discovered = list(dict.fromkeys(discovered))  # deduplicate preserving order
        discovered.sort(key=lambda p: str(p))
        return discovered

    def _detect_js_runner(self, export_path: Path) -> str:
        """Returns 'vitest', 'jest', or 'vitest' as default."""
        pkg_file = export_path / "package.json"
        if pkg_file.exists():
            try:
                pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
                all_deps = {}
                all_deps.update(pkg.get("dependencies", {}))
                all_deps.update(pkg.get("devDependencies", {}))
                if "vitest" in all_deps:
                    return "vitest"
                if "jest" in all_deps:
                    return "jest"
                # Check scripts for hints
                scripts = pkg.get("scripts", {})
                for script_val in scripts.values():
                    if "vitest" in script_val:
                        return "vitest"
                    if "jest" in script_val:
                        return "jest"
            except Exception:
                pass
        return "vitest"  # default

    def _infer_source_file(self, test_file: Path) -> str:
        test_name = test_file.name

        candidate_name: Optional[str] = None
        if test_name.startswith("test_") and test_name.endswith(".py"):
            candidate_name = f"{test_name[5:-3]}.py"
        elif test_name.endswith("_test.py"):
            candidate_name = f"{test_name[:-8]}.py"

        if not candidate_name:
            return ""

        source_candidate = test_file.parent / candidate_name
        if source_candidate.exists():
            return str(source_candidate)
        return ""

    def _infer_js_source_file(self, test_file: Path) -> str:
        """Infer the source file for a JS/TS test file."""
        name = test_file.name
        # Remove .test.js / .spec.ts etc.
        for suffix in [".test.js", ".test.ts", ".test.jsx", ".test.tsx",
                       ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx"]:
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                # Look for source file in same dir or parent src dir
                for ext in [".js", ".ts", ".jsx", ".tsx"]:
                    candidate = test_file.parent / f"{base}{ext}"
                    if candidate.exists():
                        return str(candidate)
                    # Try ../src/
                    candidate2 = test_file.parent.parent / "src" / f"{base}{ext}"
                    if candidate2.exists():
                        return str(candidate2)
                return ""
        return ""

    async def _run_js_tests(self, export_dir: str) -> SwarmSummary:
        started = time.perf_counter()
        export_path = Path(export_dir)
        package_json_path = export_path / "package.json"

        npm_exe = shutil.which("npm")
        npx_exe = shutil.which("npx")

        if not npm_exe:
            await self._broadcast("runner_unavailable", {
                "reason": "npm not found on PATH — install Node.js",
                "export_dir": str(export_dir),
            })
            logger.warning("JS test runner unavailable for %s; npm not on PATH", export_dir)
            return SwarmSummary(total=0, passed=0, failed=0, duration=0.0, results=[])

        package_data: Dict[str, object] = {}
        if package_json_path.exists():
            try:
                package_data = json.loads(package_json_path.read_text(encoding="utf-8"))
            except Exception:
                package_data = {}

        if not (export_path / "node_modules").exists():
            try:
                install_code, install_out, install_err = await self._exec_cmd(
                    [npm_exe, "install", "--prefer-offline"],
                    export_path,
                    timeout=self._npm_install_timeout,
                )
            except FileNotFoundError:
                await self._broadcast("runner_unavailable", {
                    "reason": "JS test runner (npm/npx) not found on PATH",
                    "export_dir": str(export_dir),
                })
                logger.warning("JS test runner unavailable for %s; skipping tests", export_dir)
                return SwarmSummary(total=0, passed=0, failed=0, duration=0.0, results=[])

            if install_code != 0:
                logger.warning("npm install failed in %s: %s", export_dir, (install_err or install_out)[:400])
                return self._js_dict_results_to_summary(
                    [{
                        "test": "npm_install",
                        "outcome": "error",
                        "message": (install_err or install_out or "npm install failed").strip(),
                    }],
                    started,
                )

        scripts = package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
        if not isinstance(scripts, dict):
            scripts = {}

        test_command: List[str]
        json_expected = False
        if "test" in scripts:
            test_command = [npm_exe, "run", "test", "--", "--reporter=json"]
            json_expected = True
        elif "vitest" in scripts:
            test_command = [npm_exe, "run", "vitest", "--", "run", "--reporter=json"]
            json_expected = True
        elif "test:run" in scripts:
            test_command = [npm_exe, "run", "test:run", "--", "--reporter=json"]
            json_expected = True
        else:
            test_command = [npx_exe, "vitest", "run", "--reporter=json"] if npx_exe else [npm_exe, "exec", "vitest", "run", "--reporter=json"]
            json_expected = True

        try:
            test_code, stdout, stderr = await self._exec_cmd(
                test_command,
                export_path,
                timeout=self._js_test_timeout,
            )
        except FileNotFoundError:
            await self._broadcast("runner_unavailable", {
                "reason": "JS test runner (npm/npx) not found on PATH",
                "export_dir": str(export_dir),
            })
            logger.warning("JS test runner unavailable for %s; skipping tests", export_dir)
            return SwarmSummary(total=0, passed=0, failed=0, duration=0.0, results=[])

        parsed_dicts = self._parse_vitest_json_dicts(stdout if json_expected else "", stderr)
        if not parsed_dicts:
            parsed_dicts = self._parse_js_text_dicts(f"{stdout}\n{stderr}".strip())

        if not parsed_dicts:
            parsed_dicts = [{
                "test": "js_tests",
                "outcome": "error",
                "message": "No JS/TS tests discovered or parsed",
            }]

        if test_code != 0 and all(item.get("outcome") == "passed" for item in parsed_dicts):
            parsed_dicts.append({
                "test": "js_tests",
                "outcome": "error",
                "message": (stderr or stdout or "JS test command failed").strip(),
            })

        return self._js_dict_results_to_summary(parsed_dicts, started)

    async def _run_html_tests(self, export_dir: str) -> SwarmSummary:
        logger.info("HTML project detected — no automated test runner configured; skipping tests")
        return SwarmSummary(total=0, passed=0, failed=0, duration=0.0, results=[])

    def _parse_vitest_json_dicts(self, stdout: str, stderr: str) -> List[Dict[str, str]]:
        payload = ""
        for source in (stdout, stderr):
            if not source:
                continue
            match = re.search(r"(\{[\s\S]*\})", source)
            if match:
                payload = match.group(1)
                break

        if not payload:
            return []

        try:
            data = json.loads(payload)
        except Exception:
            return []

        parsed: List[Dict[str, str]] = []

        suites = data.get("testResults", []) if isinstance(data, dict) else []
        if isinstance(suites, list):
            for suite in suites:
                suite_name = str(suite.get("name") or suite.get("testFilePath") or suite.get("filepath") or "suite")
                assertions = suite.get("assertionResults")
                if isinstance(assertions, list) and assertions:
                    for assertion in assertions:
                        title = str(assertion.get("fullName") or assertion.get("title") or "test")
                        status = str(assertion.get("status") or "").lower()
                        outcome = "passed" if status == "passed" else "failed"
                        messages = assertion.get("failureMessages") if isinstance(assertion.get("failureMessages"), list) else []
                        parsed.append({
                            "test": f"{suite_name} > {title}",
                            "outcome": outcome,
                            "message": "\n".join(str(msg) for msg in messages).strip(),
                        })
                else:
                    status = str(suite.get("status") or "").lower()
                    parsed.append({
                        "test": suite_name,
                        "outcome": "passed" if status == "passed" else "failed",
                        "message": str(suite.get("message") or "").strip(),
                    })

        return parsed

    def _parse_js_text_dicts(self, combined_output: str) -> List[Dict[str, str]]:
        text = str(combined_output or "")
        if not text.strip():
            return []

        parsed: List[Dict[str, str]] = []
        patterns = [
            (r"^[\s]*[✓✔]\s+(.+)$", "passed"),
            (r"^[\s]*[✗×xX]\s+(.+)$", "failed"),
            (r"^[\s]*PASS\s+(.+)$", "passed"),
            (r"^[\s]*FAIL\s+(.+)$", "failed"),
        ]

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            for pattern, outcome in patterns:
                match = re.match(pattern, stripped)
                if match:
                    parsed.append({
                        "test": match.group(1).strip(),
                        "outcome": outcome,
                        "message": "" if outcome == "passed" else stripped,
                    })
                    break

        return parsed

    def _js_dict_results_to_summary(self, rows: List[Dict[str, str]], started: float) -> SwarmSummary:
        normalized_rows = rows or []
        results: List[TestResult] = []
        for row in normalized_rows:
            outcome = str(row.get("outcome") or "error").lower()
            passed = outcome == "passed"
            message = str(row.get("message") or "")
            test_name = str(row.get("test") or "<js_test>")
            results.append(
                TestResult(
                    source_file="",
                    test_file=test_name,
                    passed=passed,
                    failure_output="" if passed else message,
                    failure_summary="" if passed else message,
                    duration_seconds=0.0,
                )
            )

        passed = sum(1 for item in normalized_rows if str(item.get("outcome") or "").lower() == "passed")
        failed = len(normalized_rows) - passed
        duration = time.perf_counter() - started
        return SwarmSummary(total=len(normalized_rows), passed=passed, failed=failed, duration=duration, results=results)

    def _parse_vitest_json(self, stdout: str, stderr: str, export_path: Path) -> List[TestResult]:
        """Parse vitest --reporter=json output into TestResult list."""
        results = []
        # vitest JSON is written to stdout, but may have non-JSON lines before it
        # Find the JSON blob
        json_str = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                json_str = line
                break
        # Also try to extract JSON from the full stdout
        if not json_str:
            match = re.search(r'(\{.*\})', stdout, re.DOTALL)
            if match:
                json_str = match.group(1)

        if not json_str:
            return []

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return []

        # vitest JSON format: {"testResults": [{"testFilePath": "...", "status": "passed"|"failed", "assertionResults": [...]}]}
        test_results_list = data.get("testResults", [])
        for tr in test_results_list:
            file_path = tr.get("testFilePath", "") or tr.get("filepath", "")
            status = tr.get("status", "")
            passed = status == "passed"

            failure_output = ""
            if not passed:
                # Collect failure messages from assertionResults
                assertions = tr.get("assertionResults", [])
                failure_lines = []
                for a in assertions:
                    if a.get("status") == "failed":
                        failure_lines.append(f"FAIL: {a.get('fullName', a.get('title', ''))}")
                        for msg in a.get("failureMessages", []):
                            failure_lines.append(msg)
                failure_output = "\n".join(failure_lines) or stderr or "Test failed"

            # Infer source file
            source_file = self._infer_js_source_file(Path(file_path)) if file_path else ""

            results.append(TestResult(
                source_file=source_file,
                test_file=file_path,
                passed=passed,
                failure_output=failure_output,
                failure_summary="",
                duration_seconds=tr.get("duration", 0.0) / 1000.0 if tr.get("duration") else 0.0,
            ))

        return results

    def _parse_jest_json(self, stdout: str, stderr: str, export_path: Path) -> List[TestResult]:
        """Parse jest --json output into TestResult list."""
        results = []
        json_str = None
        # jest writes JSON to stdout
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                json_str = line
                break
        if not json_str:
            match = re.search(r'(\{.*\})', stdout, re.DOTALL)
            if match:
                json_str = match.group(1)

        if not json_str:
            return []

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return []

        for tr in data.get("testResults", []):
            file_path = tr.get("testFilePath", "")
            status = tr.get("status", "")
            passed = status == "passed"

            failure_output = ""
            if not passed:
                failure_output = tr.get("message", "") or stderr or "Test failed"

            source_file = self._infer_js_source_file(Path(file_path)) if file_path else ""

            duration = tr.get("endTime", 0) - tr.get("startTime", 0)
            results.append(TestResult(
                source_file=source_file,
                test_file=file_path,
                passed=passed,
                failure_output=failure_output,
                failure_summary="",
                duration_seconds=duration / 1000.0,
            ))

        return results

    def _parse_vitest_text(self, combined_output: str, export_path: Path) -> List[TestResult]:
        """Parse vitest text output as a fallback when JSON parsing fails."""
        # If we got any output at all, create a single synthetic result representing the run
        if not combined_output.strip():
            return []

        passed = "FAIL" not in combined_output and "failed" not in combined_output.lower()
        # Try to find test file references
        file_refs = re.findall(r'[^\s]+\.(test|spec)\.(js|ts|jsx|tsx)', combined_output)

        if file_refs:
            results = []
            seen = set()
            for match in re.finditer(r'([^\s]+\.(test|spec)\.(js|ts|jsx|tsx))', combined_output):
                fp = match.group(1)
                if fp not in seen:
                    seen.add(fp)
                    file_passed = "✓" in combined_output or passed
                    results.append(TestResult(
                        source_file=self._infer_js_source_file(Path(fp)),
                        test_file=fp,
                        passed=file_passed,
                        failure_output="" if file_passed else combined_output,
                        failure_summary="",
                        duration_seconds=0.0,
                    ))
            return results

        # No file refs found — return a single synthetic result for the whole run
        return [TestResult(
            source_file="",
            test_file="<js_test_suite>",
            passed=passed,
            failure_output="" if passed else combined_output,
            failure_summary="",
            duration_seconds=0.0,
        )]

    def _parse_jest_text(self, combined_output: str, export_path: Path) -> List[TestResult]:
        """Parse jest text output as fallback."""
        return self._parse_vitest_text(combined_output, export_path)  # same logic

    def _strip_markdown_fences(self, raw_text: str) -> Tuple[str, bool]:
        fence_match = re.match(r"^```[a-zA-Z]*\r?\n([\s\S]*?)\r?\n```\s*$", raw_text.strip())
        if not fence_match:
            return raw_text, False
        return fence_match.group(1), True

    def _sanitize_pyproject_toml(self, export_dir: Path) -> None:
        """
        Sanitize pyproject.toml before editable install.

        Rules:
        1) Remove [project].readme if present.
        2) If [project].license is a table containing `file`, replace with {text = "MIT"}.
        3) Ensure [tool.hatch.build.targets.wheel].packages exists and points to inferred package dir.
        """
        pyproject_path = Path(export_dir) / "pyproject.toml"
        if not pyproject_path.exists():
            return

        try:
            raw_toml = pyproject_path.read_text(encoding="utf-8")
            # Strip markdown fences (```toml ... ``` or ``` ... ```) that Codex sometimes wraps output in
            raw_toml, stripped_fences = self._strip_markdown_fences(raw_toml)
            if stripped_fences:
                pyproject_path.write_text(raw_toml, encoding="utf-8")
                logger.info("Stripped markdown fences from pyproject.toml in %s", export_dir)
            data = tomllib.loads(raw_toml)
        except Exception as exc:
            logger.warning("Could not parse pyproject.toml for sanitization in %s: %s", export_dir, exc)
            return

        if not isinstance(data, dict):
            return

        project = data.get("project")
        if not isinstance(project, dict):
            project = {}
            data["project"] = project

        project.pop("readme", None)

        license_value = project.get("license")
        if isinstance(license_value, dict) and "file" in license_value:
            project["license"] = {"text": "MIT"}

        # Strip dynamic version declaration and replace with static version
        dynamic = project.get("dynamic", [])
        if isinstance(dynamic, list) and "version" in dynamic:
            dynamic = [d for d in dynamic if d != "version"]
            if dynamic:
                project["dynamic"] = dynamic
            else:
                project.pop("dynamic", None)
            # Only set static version if not already present
            if "version" not in project:
                project["version"] = "0.1.0"

        # Remove hatch version source config (regex path etc.) if present
        tool = data.get("tool", {})
        hatch = tool.get("hatch", {}) if isinstance(tool, dict) else {}
        if isinstance(hatch, dict) and "version" in hatch:
            del hatch["version"]

        # Replace build backend with setuptools for editable install compatibility
        build_system = data.get("build-system")
        if not isinstance(build_system, dict):
            build_system = {}
            data["build-system"] = build_system
        build_system["requires"] = ["setuptools"]
        build_system["build-backend"] = "setuptools.build_meta"

        project_name = project.get("name")
        package_dir = None
        if isinstance(project_name, str) and project_name.strip():
            package_dir = re.sub(r"[^A-Za-z0-9_]", "_", project_name.strip().lower().replace("-", "_"))

        if package_dir:
            tool = data.get("tool")
            if not isinstance(tool, dict):
                tool = {}
                data["tool"] = tool

            hatch = tool.get("hatch")
            if not isinstance(hatch, dict):
                hatch = {}
                tool["hatch"] = hatch

            build = hatch.get("build")
            if not isinstance(build, dict):
                build = {}
                hatch["build"] = build

            targets = build.get("targets")
            if not isinstance(targets, dict):
                targets = {}
                build["targets"] = targets

            wheel = targets.get("wheel")
            if not isinstance(wheel, dict):
                wheel = {}
                targets["wheel"] = wheel

            wheel["packages"] = [package_dir]

        try:
            import tomli_w  # type: ignore
            serialized = tomli_w.dumps(data)
        except Exception:
            serialized = self._to_toml(data)

        pyproject_path.write_text(serialized, encoding="utf-8")

    def _to_toml(self, payload: dict) -> str:
        lines: List[str] = []

        def key_repr(key: str) -> str:
            if re.fullmatch(r"[A-Za-z0-9_-]+", key):
                return key
            return json.dumps(key)

        def value_repr(value):
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, str):
                return json.dumps(value)
            if isinstance(value, list):
                return "[" + ", ".join(value_repr(item) for item in value) + "]"
            if isinstance(value, dict):
                items = ", ".join(f"{key_repr(str(k))} = {value_repr(v)}" for k, v in value.items())
                return "{" + items + "}"
            return json.dumps(str(value))

        def write_table(path: List[str], table: dict) -> None:
            scalar_items = []
            table_items = []
            for key, value in table.items():
                if isinstance(value, dict):
                    table_items.append((key, value))
                else:
                    scalar_items.append((key, value))

            if path:
                lines.append(f"[{'.'.join(key_repr(p) for p in path)}]")

            for key, value in scalar_items:
                lines.append(f"{key_repr(str(key))} = {value_repr(value)}")

            for key, value in table_items:
                if lines and lines[-1] != "":
                    lines.append("")
                write_table(path + [str(key)], value)

        write_table([], payload)
        return "\n".join(lines).rstrip() + "\n"

    async def _run_single_test(
        self,
        export_path: Path,
        test_file: Path,
        source_file: str,
        semaphore: asyncio.Semaphore,
        session_id: str,
    ) -> TestResult:
        async with semaphore:
            started = time.perf_counter()
            await self._maybe_install_editable_package(export_path)
            base_cmd = [
                "python", "-m", "pytest", str(test_file),
                "-q", "--tb=short", "--cache-clear",
            ]
            returncode, stdout, stderr = await self._exec_cmd(base_cmd, export_path)
            combined_output = f"{stdout}\n{stderr}".strip()

            if returncode != 0 and "No module named pytest" in combined_output:
                fallback_cmd = [
                    sys.executable, "-m", "pytest", str(test_file),
                    "-q", "--tb=short", "--cache-clear",
                ]
                returncode, stdout, stderr = await self._exec_cmd(fallback_cmd, export_path)
                combined_output = f"{stdout}\n{stderr}".strip()

            passed = returncode == 0
            failure_output = "" if passed else combined_output
            failure_summary = ""
            duration = time.perf_counter() - started

            if not passed:
                failure_summary = await self._summarize_failure(failure_output)

            result = TestResult(
                source_file=source_file,
                test_file=str(test_file),
                passed=passed,
                failure_output=failure_output,
                failure_summary=failure_summary,
                duration_seconds=duration,
            )

            await self._persist_result(session_id=session_id, result=result)

            if passed:
                await self._broadcast("test_passed", {
                    "test_file": str(test_file),
                    "duration": duration,
                })
            else:
                await self._broadcast("test_failed", {
                    "test_file": str(test_file),
                    "summary": failure_summary,
                    "duration": duration,
                })

            return result

    def _find_primary_init_file(self, export_path: Path) -> Optional[Path]:
        pyproject = export_path / "pyproject.toml"
        if pyproject.exists():
            try:
                parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                project = parsed.get("project", {}) if isinstance(parsed, dict) else {}
                project_name = str(project.get("name") or "").strip()
                if project_name:
                    package_name = project_name.replace("-", "_")
                    candidate = export_path / package_name / "__init__.py"
                    if candidate.exists():
                        return candidate
            except Exception:
                pass

        for candidate in sorted(export_path.rglob("__init__.py"), key=lambda p: str(p)):
            normalized = candidate.as_posix()
            if "/.venv/" in normalized or "/venv/" in normalized or "/tests/" in normalized:
                continue
            return candidate

        return None

    async def _attempt_install_repair(self, export_path: Path, install_error: str) -> bool:
        if self.ai is None:
            return False

        pyproject_path = export_path / "pyproject.toml"
        init_path = self._find_primary_init_file(export_path)

        pyproject_content = ""
        init_content = ""

        if pyproject_path.exists():
            pyproject_content = pyproject_path.read_text(encoding="utf-8", errors="replace")
        if init_path and init_path.exists():
            init_content = init_path.read_text(encoding="utf-8", errors="replace")

        system_prompt = "You are a packaging repair assistant. Return strictly valid JSON only."
        user_prompt = (
            "Fix only pyproject.toml and/or __init__.py to make pip install -e . succeed. "
            "Do not touch any other files.\n\n"
            "INSTALL_ERROR:\n"
            f"{install_error}\n\n"
            "PYPROJECT_TOML:\n"
            f"{pyproject_content or '<missing>'}\n\n"
            "INIT_PY:\n"
            f"{init_content or '<missing>'}\n\n"
            "Return JSON only in this shape:\n"
            "{\n"
            "  \"pyproject_toml\": \"<updated pyproject.toml or empty string>\",\n"
            "  \"init_py\": \"<updated __init__.py or empty string>\"\n"
            "}"
        )

        try:
            raw = await self.ai.complete(
                backend="gemini-flash",
                system=system_prompt,
                message=user_prompt,
                max_tokens=4096,
                json_mode=True,
            )
        except Exception as exc:
            logger.warning("Install repair model call failed for %s: %s", export_path, exc)
            return False

        payload = None
        try:
            payload = json.loads((raw or "").strip())
        except Exception:
            match = re.search(r"\{[\s\S]*\}", (raw or "").strip())
            if match:
                try:
                    payload = json.loads(match.group(0))
                except Exception:
                    payload = None

        if not isinstance(payload, dict):
            return False

        changed = False

        updated_pyproject = str(payload.get("pyproject_toml") or "")
        if updated_pyproject and pyproject_path.exists() and updated_pyproject != pyproject_content:
            pyproject_path.write_text(updated_pyproject, encoding="utf-8")
            changed = True

        updated_init = str(payload.get("init_py") or "")
        if updated_init:
            target_init = init_path
            if target_init is None:
                target_init = export_path / "__init__.py"
            target_init.parent.mkdir(parents=True, exist_ok=True)
            if not target_init.exists() or updated_init != (target_init.read_text(encoding="utf-8", errors="replace") if target_init.exists() else ""):
                target_init.write_text(updated_init, encoding="utf-8")
                changed = True

        return changed

    async def _maybe_install_editable_package(self, export_path: Path) -> bool:
        export_key = str(export_path.resolve())
        if export_key in self._editable_install_done:
            return self._editable_install_done[export_key]

        self._editable_install_done[export_key] = True

        has_python_package = (export_path / "pyproject.toml").exists() or (export_path / "setup.py").exists()
        if not has_python_package:
            return True

        try:
            self._sanitize_pyproject_toml(export_path)
        except Exception as exc:
            logger.warning("pyproject.toml sanitization failed in %s: %s", export_path, exc)

        install_cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-e",
            ".",
            "--no-deps",
            "--break-system-packages",
            "-q",
        ]
        returncode, stdout, stderr = await self._exec_cmd(install_cmd, export_path)
        if returncode != 0:
            combined_output = f"{stdout}\n{stderr}".strip()
            self._install_error_by_export[export_key] = combined_output
            logger.warning("Editable install failed in %s. Attempting narrow packaging repair.", export_path)

            repaired = await self._attempt_install_repair(export_path, combined_output)
            if repaired:
                retry_code, retry_stdout, retry_stderr = await self._exec_cmd(install_cmd, export_path)
                if retry_code == 0:
                    self._install_error_by_export[export_key] = ""
                    self._editable_install_done[export_key] = True
                    return True
                combined_output = f"{retry_stdout}\n{retry_stderr}".strip()
                self._install_error_by_export[export_key] = combined_output

            self._editable_install_done[export_key] = False
            return False

        self._install_error_by_export[export_key] = ""
        self._editable_install_done[export_key] = True
        return True

    async def _exec_cmd(self, cmd: List[str], cwd: Path, timeout: Optional[int] = None) -> Tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
        try:
            if timeout is not None and timeout > 0:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
            else:
                stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            timeout_msg = f"Command timed out after {timeout}s"
            merged_stderr = (stderr + "\n" + timeout_msg).strip()
            return -1, stdout, merged_stderr
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return process.returncode, stdout, stderr

    async def _summarize_failure(self, failure_output: str) -> str:
        # Use flash-lite — this is a 256-token summarization task that does not
        # need Gemini Pro.  Using "gemini" (Pro) here was setting _gemini_pro_degraded
        # on any transient failure, poisoning all subsequent Pro calls for the session.
        for _backend in ("gemini-flash-lite", "gemini-flash", "gemini-customtools"):
            try:
                summary = await self.ai.complete(
                    backend=_backend,
                    system="You analyze test output from JavaScript/TypeScript or Python test runners.",
                    message=(
                        "Summarize this test failure in 2-3 plain English sentences:\n"
                        f"{failure_output}"
                    ),
                    max_tokens=256,
                )
                return (summary or "").strip()
            except Exception:
                continue
        return "Test failed. Could not generate AI summary."

    async def _ensure_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
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
            await db.commit()

    async def _persist_result(self, session_id: str, result: TestResult) -> None:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
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
                (
                    session_id,
                    result.source_file,
                    result.test_file,
                    1 if result.passed else 0,
                    result.failure_output,
                    result.failure_summary,
                    result.duration_seconds,
                ),
            )
            await db.commit()

    async def _broadcast(self, event_type: str, data: dict) -> None:
        if self._sse_broadcast is None:
            return
        try:
            await self._sse_broadcast(event_type, data)
        except Exception:
            return
