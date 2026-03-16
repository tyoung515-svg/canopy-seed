"""
Swarm Tools (ADR-033)
─────────────────────
Callable tool functions that swarm agents can invoke during code generation.

Each tool is a plain async function with an MCP-compatible signature.
A companion TOOL_DEFINITIONS list provides the JSON schema blocks ready
for injection into Claude tool_use API calls or Qwen function-calling.

Tools provided
──────────────
  run_code        — execute Python or JavaScript in a subprocess sandbox
  lint_code       — run ruff (Python) or eslint (JS) and return violations
  read_export_file  — read a file from the active exports directory
  write_export_file — write a file to the active exports directory (guarded)
  web_search      — Gemini Google Search grounding for current API docs
  fetch_url       — HTTP fetch of a documentation page or README

Usage in agent_pool
───────────────────
The tools are injected into subtask prompts via SwarmToolContext, which
renders a compact "tools available" block for the agent system prompt.
The agent calls a tool by emitting a JSON tool-call block; agent_pool
parses and executes it before writing the final file.

MCP compatibility
─────────────────
TOOL_DEFINITIONS matches the MCP tool schema format so the same list can
be registered with an MCP server in Session 45 without modification.
"""

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── Tool definitions (MCP-compatible JSON schema) ────────────────────────────

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "run_code",
        "description": (
            "Execute Python or JavaScript code in a sandboxed subprocess. "
            "Use this to verify your implementation produces the correct output "
            "before finalising a file. Returns stdout, stderr, and exit_code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The code to execute.",
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "javascript"],
                    "description": "Language of the code snippet.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default 30, max 60).",
                    "default": 30,
                },
            },
            "required": ["code", "language"],
        },
    },
    {
        "name": "lint_code",
        "description": (
            "Lint Python code with ruff or JavaScript/TypeScript with eslint. "
            "Returns a list of violations. An empty list means the code is clean. "
            "Fix all violations before writing the file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Source code to lint.",
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "javascript", "typescript"],
                    "description": "Language of the source code.",
                },
            },
            "required": ["code", "language"],
        },
    },
    {
        "name": "read_export_file",
        "description": (
            "Read a file that has already been written to the export directory "
            "by a sibling agent. Use this to check interfaces, imports, and "
            "data structures before writing a dependent file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the export directory, e.g. 'src/utils.py'.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_export_file",
        "description": (
            "Write content to a file in the export directory. "
            "This is the canonical way to emit your final output — "
            "the pipeline will not read any other output. "
            "Path must be relative and within the export directory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the export directory.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "search_docs",
        "description": (
            "Search documentation for a library or technology. "
            "Returns a short summary with the most relevant information. "
            "Use when you are unsure about an API signature, parameter name, "
            "or behaviour."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query.",
                },
                "library": {
                    "type": "string",
                    "description": "Library or framework name to scope the search (e.g. 'pytest', 'react').",
                    "default": "",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_test",
        "description": (
            "Run a specific test file (or a single test function) from the export directory "
            "using pytest. Use this AFTER writing a fix to verify the failing test now passes "
            "before declaring done. Returns pytest output with pass/fail status. "
            "Much faster than a full suite re-run — targeted to one file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "test_file": {
                    "type": "string",
                    "description": (
                        "Relative path to the test file within the export directory, "
                        "e.g. 'tests/test_processor.py'."
                    ),
                },
                "test_name": {
                    "type": "string",
                    "description": (
                        "Optional: specific test function name to run (passed as -k filter), "
                        "e.g. 'test_extract_specs_returns_battery_spec'. "
                        "Omit to run all tests in the file."
                    ),
                    "default": "",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default 60, max 120).",
                    "default": 60,
                },
            },
            "required": ["test_file"],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search for a text pattern across all source files in the export directory. "
            "Use this to find all usages of a class, function, or variable before "
            "modifying its interface — prevents missed call sites. "
            "Returns matched lines with file path and line number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Text or regex pattern to search for, "
                        "e.g. 'BatterySpec(' or 'extract_specs'."
                    ),
                },
                "file_glob": {
                    "type": "string",
                    "description": (
                        "Optional glob to restrict search to matching filenames, "
                        "e.g. '*.py' or 'tests/*.py'. Defaults to all files."
                    ),
                    "default": "",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether the search is case-sensitive (default false).",
                    "default": False,
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "install_package_deps",
        "description": (
            "Run 'pip install -e .' in the export directory to install any Python packages "
            "listed in pyproject.toml dependencies. Use this AFTER updating pyproject.toml to "
            "add a missing dependency (e.g. sqlalchemy, pydantic, fastapi) so the package is "
            "actually installed before the next test run. Also use this as the FIRST step "
            "whenever the test failure is a ModuleNotFoundError or ImportError for a third-party "
            "library — do NOT waste tool rounds trying to remove import statements; install the "
            "package instead, then verify with run_test."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief note on why you're installing deps, e.g. 'added sqlalchemy to pyproject.toml'.",
                    "default": "",
                },
            },
            "required": [],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the internet for up-to-date API docs, error messages, library changelogs, "
            "and developer guides. Use this BEFORE guessing at API signatures — search for the "
            "exact error message or 'library-name method-name example' to find current usage. "
            "Returns up to 5 results with title, snippet, and URL. Follow up with fetch_url "
            "to read a specific result in full."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query, e.g. 'sqlalchemy 2.0 Session.execute example' or "
                        "'pytest asyncio fixture scope error'. Be specific."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (default 5, max 8).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch the text content of a specific URL — use this to read documentation pages, "
            "GitHub README files, PyPI pages, or any other public web page. "
            "HTML tags are stripped; only the readable text is returned. "
            "Use after web_search to read a result in full."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to fetch, e.g. 'https://docs.sqlalchemy.org/en/20/orm/session_api.html'.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 6000, max 12000).",
                    "default": 6000,
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "edit_export_file",
        "description": (
            "Make a surgical find-and-replace edit in a file. MUCH more efficient than "
            "write_export_file for targeted fixes — you only specify the exact text to find "
            "and its replacement. Use this for fixing variable names, import paths, function "
            "signatures, status codes, or any small change. The old_text must match EXACTLY "
            "(including whitespace and indentation). Prefer this over write_export_file "
            "whenever possible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the export directory.",
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find in the file (must match precisely).",
                },
                "new_text": {
                    "type": "string",
                    "description": "The replacement text.",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "read_export_files",
        "description": (
            "Read MULTIPLE source files in a single call. Much more efficient than calling "
            "read_export_file multiple times — use this when you need to examine several "
            "files at once. Returns all file contents in one response."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of relative paths within the export directory.",
                },
            },
            "required": ["paths"],
        },
    },
    {
        "name": "declare_done",
        "description": (
            "Signal that all fixes are complete and the Big Fixer session should end. "
            "Call this ONLY after all previously failing tests pass (verified with run_test). "
            "Do NOT call this until you have confirmed passing tests. "
            "Include a short plain-text summary of what was fixed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "Brief summary of the fixes applied, "
                        "e.g. 'Fixed import error in processor.py and updated BatterySpec constructor signature.'"
                    ),
                },
            },
            "required": ["summary"],
        },
    },
]


# ─── Tool implementations ─────────────────────────────────────────────────────

async def run_code(
    code: str,
    language: str,
    timeout: int = 30,
    **_kwargs,
) -> Dict[str, Any]:
    """
    Execute Python or JS code in a subprocess sandbox.

    Returns {"stdout": str, "stderr": str, "exit_code": int, "timed_out": bool}
    """
    timeout = min(max(1, timeout), 60)  # clamp 1–60 s

    if language == "python":
        cmd = [sys.executable, "-c", code]
    elif language in {"javascript", "js"}:
        node = shutil.which("node")
        if not node:
            return {
                "stdout": "",
                "stderr": "node not found — JavaScript execution unavailable",
                "exit_code": 1,
                "timed_out": False,
            }
        cmd = [node, "-e", code]
    else:
        return {
            "stdout": "",
            "stderr": f"Unsupported language: {language!r}. Use 'python' or 'javascript'.",
            "exit_code": 1,
            "timed_out": False,
        }

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return {
                "stdout": stdout_bytes.decode("utf-8", errors="replace")[:4000],
                "stderr": stderr_bytes.decode("utf-8", errors="replace")[:2000],
                "exit_code": proc.returncode or 0,
                "timed_out": False,
            }
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {
                "stdout": "",
                "stderr": f"Execution timed out after {timeout}s",
                "exit_code": -1,
                "timed_out": True,
            }
    except Exception as exc:
        return {
            "stdout": "",
            "stderr": f"Execution error: {exc}",
            "exit_code": -1,
            "timed_out": False,
        }


async def lint_code(
    code: str,
    language: str,
    **_kwargs,
) -> Dict[str, Any]:
    """
    Lint source code using ruff (Python) or eslint (JS/TS).

    Returns {"violations": List[str], "clean": bool, "linter": str}
    Violations are short human-readable strings, e.g. "line 5: F821 undefined name 'x'"
    """
    lang = language.lower()

    if lang == "python":
        return await _lint_python(code)
    elif lang in {"javascript", "typescript", "js", "ts"}:
        return await _lint_js(code, lang)
    else:
        return {
            "violations": [],
            "clean": True,
            "linter": "none",
            "note": f"No linter for language {language!r}",
        }


async def _lint_python(code: str) -> Dict[str, Any]:
    """Lint Python code using ruff (preferred) or pyflakes fallback."""
    ruff = shutil.which("ruff")
    if not ruff:
        # Try installing ruff inline; if unavailable, skip gracefully
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "ruff", "--quiet",
                "--break-system-packages",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            ruff = shutil.which("ruff") or (
                Path(sys.executable).parent / "ruff"
            )
            if not Path(str(ruff)).exists():
                ruff = None
        except Exception:
            ruff = None

    if not ruff:
        # pyflakes fallback — only catches import/name errors, not style
        return await _lint_python_pyflakes(code)

    import tempfile, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            str(ruff), "check", "--output-format=text", tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        raw = stdout_bytes.decode("utf-8", errors="replace")
        # ruff output: "<file>:<line>:<col>: <code> <message>"
        violations = []
        for line in raw.splitlines():
            # Replace temp file path with "<code>" for readability
            clean = line.replace(tmp, "<code>").strip()
            if clean:
                violations.append(clean)
        return {"violations": violations, "clean": len(violations) == 0, "linter": "ruff"}
    except asyncio.TimeoutError:
        return {"violations": [], "clean": True, "linter": "ruff", "note": "lint timed out"}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def _lint_python_pyflakes(code: str) -> Dict[str, Any]:
    """Fallback linter using pyflakes via subprocess."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pyflakes", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=code.encode()), timeout=15
        )
        output = (stdout_bytes + stderr_bytes).decode("utf-8", errors="replace")
        violations = [line.strip() for line in output.splitlines() if line.strip()]
        return {"violations": violations, "clean": len(violations) == 0, "linter": "pyflakes"}
    except Exception as exc:
        return {"violations": [], "clean": True, "linter": "none", "note": str(exc)}


async def _lint_js(code: str, lang: str) -> Dict[str, Any]:
    """Lint JS/TS with eslint if available, else skip gracefully."""
    eslint = shutil.which("eslint")
    if not eslint:
        return {
            "violations": [],
            "clean": True,
            "linter": "none",
            "note": "eslint not found — JS/TS linting skipped",
        }

    import tempfile, os
    ext = ".ts" if "typescript" in lang else ".js"
    with tempfile.NamedTemporaryFile(mode="w", suffix=ext, delete=False) as f:
        f.write(code)
        tmp = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            eslint, "--format=compact", tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        raw = stdout_bytes.decode("utf-8", errors="replace")
        violations = [
            line.replace(tmp, "<code>").strip()
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("0 problems")
        ]
        return {"violations": violations, "clean": len(violations) == 0, "linter": "eslint"}
    except asyncio.TimeoutError:
        return {"violations": [], "clean": True, "linter": "eslint", "note": "lint timed out"}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def read_export_file(
    path: str,
    export_dir: str = "",
    **_kwargs,
) -> Dict[str, Any]:
    """
    Read a file from the export directory.

    Returns {"content": str, "found": bool, "path": str}
    Path traversal outside export_dir is blocked.
    """
    if not export_dir:
        return {"content": "", "found": False, "path": path, "error": "export_dir not set"}

    export_path = Path(export_dir).resolve()
    target = (export_path / path).resolve()

    # Path traversal guard
    try:
        target.relative_to(export_path)
    except ValueError:
        return {
            "content": "",
            "found": False,
            "path": path,
            "error": f"Path traversal blocked: {path!r} is outside export_dir",
        }

    if not target.exists():
        return {"content": "", "found": False, "path": path}

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        # Cap at 8000 chars to stay within context budget
        if len(content) > 8000:
            content = content[:8000] + "\n... [truncated — file too large for tool read]"
        return {"content": content, "found": True, "path": str(target.relative_to(export_path))}
    except Exception as exc:
        return {"content": "", "found": False, "path": path, "error": str(exc)}


async def write_export_file(
    path: str,
    content: str,
    export_dir: str = "",
    **_kwargs,
) -> Dict[str, Any]:
    """
    Write content to a file in the export directory.

    Returns {"written": bool, "path": str, "bytes": int}
    Path traversal outside export_dir is blocked.
    """
    if not export_dir:
        return {"written": False, "path": path, "error": "export_dir not set"}

    export_path = Path(export_dir).resolve()
    target = (export_path / path).resolve()

    # Path traversal guard
    try:
        target.relative_to(export_path)
    except ValueError:
        return {
            "written": False,
            "path": path,
            "error": f"Path traversal blocked: {path!r} is outside export_dir",
        }

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "written": True,
            "path": str(target.relative_to(export_path)),
            "bytes": len(content.encode("utf-8")),
        }
    except Exception as exc:
        return {"written": False, "path": path, "error": str(exc)}


async def edit_export_file(
    path: str,
    old_text: str,
    new_text: str,
    export_dir: str = "",
    **_kwargs,
) -> Dict[str, Any]:
    """
    Surgical find-and-replace edit in an export file.

    Returns {"edited": str, "replacements": int} or {"error": str}
    """
    if not export_dir:
        return {"error": "export_dir not set"}

    export_path = Path(export_dir).resolve()
    target = (export_path / path).resolve()

    # Path traversal guard
    try:
        target.relative_to(export_path)
    except ValueError:
        return {"error": f"Path traversal blocked: {path!r} is outside export_dir"}

    if not target.exists():
        return {"error": f"File not found: {path}. Use search_code or read_export_file to find the right path."}

    # Block test file edits
    rel = str(target.relative_to(export_path))
    if rel.startswith("tests/") or rel.startswith("test_") or "/test_" in rel:
        return {"error": "Cannot edit test files — they are read-only."}

    try:
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            # Show a snippet to help debug
            snippet = content[:300] + "..." if len(content) > 300 else content
            return {
                "error": f"old_text not found in {path}. Use search_code or read_export_file to verify exact text.",
                "file_preview": snippet,
            }
        new_content = content.replace(old_text, new_text)
        target.write_text(new_content, encoding="utf-8")
        return {
            "edited": str(target.relative_to(export_path)),
            "replacements": count,
        }
    except Exception as exc:
        return {"error": f"Edit failed: {exc}"}


async def read_export_files(
    paths: List[str],
    export_dir: str = "",
    **_kwargs,
) -> Dict[str, Any]:
    """
    Batch-read multiple files from the export directory.

    Returns {"files_read": int, "results": [{path, content}, ...]}
    """
    if not export_dir:
        return {"error": "export_dir not set"}

    export_path = Path(export_dir).resolve()
    results = []
    max_files = 10
    max_chars_per_file = 6000  # tighter than single read to stay within budget

    for p in paths[:max_files]:
        target = (export_path / p).resolve()
        try:
            target.relative_to(export_path)
        except ValueError:
            results.append({"path": p, "content": "", "error": "path traversal blocked"})
            continue

        if not target.exists():
            results.append({"path": p, "content": "", "error": "file not found"})
            continue

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            if len(content) > max_chars_per_file:
                content = content[:max_chars_per_file] + "\n... [truncated]"
            results.append({"path": str(target.relative_to(export_path)), "content": content})
        except Exception as exc:
            results.append({"path": p, "content": "", "error": str(exc)})

    return {"files_read": len(results), "results": results}


async def search_docs(
    query: str,
    library: str = "",
    **_kwargs,
) -> Dict[str, Any]:
    """
    Search documentation using DuckDuckGo scoped to docs sites.

    Returns {"results": List[str], "query_used": str}
    Each result is a short extracted snippet.
    """
    scoped_query = query
    if library:
        # Scope to known docs domains for this library
        docs_domains = _get_docs_domain(library)
        if docs_domains:
            scoped_query = f"{query} site:{docs_domains}"
        else:
            scoped_query = f"{library} {query} documentation"

    try:
        import httpx
        # DuckDuckGo HTML search (no API key required)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; CanopySwarm/1.0; +https://github.com/canopy-seed)"
            )
        }
        params = {"q": scoped_query, "kl": "us-en", "kp": "-1"}
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params=params,
                headers=headers,
            )
            r.raise_for_status()
            html = r.text

        # Extract result snippets from DDG HTML structure
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )
        # Clean HTML tags from snippets
        clean: List[str] = []
        for s in snippets[:5]:
            text = re.sub(r"<[^>]+>", "", s).strip()
            if text:
                clean.append(text[:300])

        if not clean:
            return {
                "results": ["No documentation snippets found. Try rephrasing your query."],
                "query_used": scoped_query,
            }

        return {"results": clean, "query_used": scoped_query}

    except Exception as exc:
        return {
            "results": [],
            "query_used": scoped_query,
            "error": f"Search failed: {exc}",
        }


def _get_docs_domain(library: str) -> str:
    """Return a site: filter domain for well-known libraries."""
    mapping = {
        "pytest": "docs.pytest.org",
        "pytest-asyncio": "pytest-asyncio.readthedocs.io",
        "pydantic": "docs.pydantic.dev",
        "fastapi": "fastapi.tiangolo.com",
        "sqlalchemy": "docs.sqlalchemy.org",
        "aiohttp": "docs.aiohttp.org",
        "httpx": "www.python-httpx.org",
        "react": "react.dev",
        "vue": "vuejs.org",
        "vitest": "vitest.dev",
        "jest": "jestjs.io",
        "typescript": "www.typescriptlang.org",
        "numpy": "numpy.org/doc",
        "pandas": "pandas.pydata.org/docs",
        "django": "docs.djangoproject.com",
        "flask": "flask.palletsprojects.com",
        "anthropic": "docs.anthropic.com",
        "openai": "platform.openai.com/docs",
    }
    return mapping.get(library.lower(), "")


async def run_test(
    test_file: str,
    test_name: str = "",
    timeout: int = 60,
    export_dir: str = "",
    **_kwargs,
) -> Dict[str, Any]:
    """
    Run a specific pytest file (or single test) from the export directory.

    Mirrors TesterSwarm's subprocess approach: runs with cwd=export_dir so
    relative imports resolve correctly.  Attempts a quick editable install
    if the test file imports the package and the package has a pyproject.toml.

    Returns {"passed": bool, "output": str, "exit_code": int, "timed_out": bool}
    """
    if not export_dir:
        return {"passed": False, "output": "export_dir not set", "exit_code": -1, "timed_out": False}

    export_path = Path(export_dir).resolve()
    timeout = min(max(10, timeout), 120)

    # Quick editable install if a pyproject.toml is present (best-effort, silent on failure)
    has_pkg = (export_path / "pyproject.toml").exists() or (export_path / "setup.py").exists()
    if has_pkg:
        try:
            install_proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "-e", ".",
                "--no-deps", "--break-system-packages", "-q",
                cwd=str(export_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(install_proc.wait(), timeout=30)
        except Exception:
            pass  # install failure surfaced by pytest output below

    cmd = [sys.executable, "-m", "pytest", test_file, "-q", "--tb=short", "--cache-clear"]
    if test_name:
        cmd += ["-k", test_name]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(export_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = (stdout_b.decode("utf-8", errors="replace") + stderr_b.decode("utf-8", errors="replace")).strip()
            # Cap output to keep context budget sane — short tb format is concise anyway
            if len(output) > 6000:
                output = output[:6000] + "\n... [truncated]"
            passed = proc.returncode == 0
            return {"passed": passed, "output": output, "exit_code": proc.returncode or 0, "timed_out": False}
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"passed": False, "output": f"Test run timed out after {timeout}s", "exit_code": -1, "timed_out": True}
    except Exception as exc:
        return {"passed": False, "output": f"run_test error: {exc}", "exit_code": -1, "timed_out": False}


async def search_code(
    pattern: str,
    file_glob: str = "",
    case_sensitive: bool = False,
    export_dir: str = "",
    **_kwargs,
) -> Dict[str, Any]:
    """
    Search for a text/regex pattern across all source files in the export directory.

    Uses ripgrep (rg) if available for speed; falls back to Python glob + re.

    Returns {"matches": [{"file": str, "line": int, "content": str}], "match_count": int}
    """
    if not export_dir:
        return {"matches": [], "match_count": 0, "error": "export_dir not set"}

    export_path = Path(export_dir).resolve()
    MAX_MATCHES = 50  # cap so we don't blow the context window

    # ── Try ripgrep first ────────────────────────────────────────────────────
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--line-number", "--with-filename", "--no-heading"]
        if not case_sensitive:
            cmd.append("--ignore-case")
        if file_glob:
            cmd += ["--glob", file_glob]
        cmd += [pattern, str(export_path)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            lines = stdout_b.decode("utf-8", errors="replace").splitlines()
            matches: List[Dict[str, Any]] = []
            for raw_line in lines[:MAX_MATCHES]:
                # rg output: /abs/path/file.py:42:matched content
                parts = raw_line.split(":", 2)
                if len(parts) >= 3:
                    rel = str(Path(parts[0]).relative_to(export_path))
                    matches.append({"file": rel, "line": int(parts[1]), "content": parts[2].strip()})
            return {"matches": matches, "match_count": len(matches)}
        except Exception:
            pass  # fall through to Python fallback

    # ── Python fallback ──────────────────────────────────────────────────────
    import fnmatch
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        return {"matches": [], "match_count": 0, "error": f"Invalid regex: {exc}"}

    glob_pat = file_glob or "**/*"
    matches = []
    for fpath in sorted(export_path.glob(glob_pat)):
        if not fpath.is_file():
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if compiled.search(line):
                rel = str(fpath.relative_to(export_path))
                matches.append({"file": rel, "line": lineno, "content": line.strip()})
                if len(matches) >= MAX_MATCHES:
                    break
        if len(matches) >= MAX_MATCHES:
            break

    return {"matches": matches, "match_count": len(matches)}


async def install_package_deps(
    reason: str = "",
    export_dir: str = "",
    **_kwargs,
) -> Dict[str, Any]:
    """
    Run pip install -e . (with deps) in the export directory.

    Called by Big Fixer when it detects a ModuleNotFoundError / ImportError
    that requires a new package to be installed rather than a code change.
    """
    if not export_dir:
        return {"success": False, "output": "export_dir not provided — cannot run pip install"}

    cmd = [sys.executable, "-m", "pip", "install", "-e", ".", "--break-system-packages", "-q"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=export_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
        combined = (stdout_b + stderr_b).decode(errors="replace").strip()
        success = proc.returncode == 0
        return {
            "success": success,
            "exit_code": proc.returncode,
            "output": combined[:1000] if combined else ("Install succeeded — no output" if success else "Install failed — no output"),
        }
    except asyncio.TimeoutError:
        return {"success": False, "output": "pip install timed out after 120s"}
    except Exception as exc:
        return {"success": False, "output": f"pip install error: {exc}"}


async def web_search(
    query: str,
    max_results: int = 5,
    **_kwargs,
) -> Dict[str, Any]:
    """
    Search the internet using Gemini's built-in Google Search grounding.

    Returns {"results": [{"title": str, "snippet": str, "url": str}],
             "summary": str, "query_used": str}
    The summary is Gemini's grounded answer; results are the cited sources.
    Use fetch_url to read any result URL in full.
    """
    import os
    import httpx

    max_results = min(max(1, max_results), 8)

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    if not api_key:
        return {
            "results": [],
            "query_used": query,
            "error": "web_search: GOOGLE_API_KEY not set — cannot call Gemini Search grounding",
        }

    # Use flash model for fast, cheap search grounding
    search_model = os.getenv("GEMINI_FLASH_MODEL", "gemini-3-flash-preview")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{search_model}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
        # Keep generation brief — we care about grounding chunks, not a long essay
        "generationConfig": {"maxOutputTokens": 512},
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        candidate = (data.get("candidates") or [{}])[0]
        content = candidate.get("content", {})
        parts = content.get("parts", [])
        summary = " ".join(p.get("text", "") for p in parts if "text" in p).strip()

        grounding_meta = candidate.get("groundingMetadata", {})
        chunks = grounding_meta.get("groundingChunks", [])
        supports = grounding_meta.get("groundingSupports", [])

        # Build a snippet map: chunkIndex -> list of supporting text segments
        snippet_map: Dict[int, List[str]] = {}
        for sup in supports:
            seg = sup.get("segment", {})
            text_seg = seg.get("text", "").strip()
            if not text_seg:
                continue
            for idx in sup.get("groundingChunkIndices", []):
                snippet_map.setdefault(idx, []).append(text_seg)

        results: List[Dict[str, Any]] = []
        for i, chunk in enumerate(chunks[:max_results]):
            web = chunk.get("web", {})
            title = web.get("title", "").strip() or "(no title)"
            chunk_url = web.get("uri", "").strip()
            if not chunk_url:
                continue
            # Join up to 2 supporting snippets for this source
            snippets = snippet_map.get(i, [])
            snippet_text = " … ".join(snippets[:2])[:400] if snippets else ""
            results.append({
                "title": title[:120],
                "snippet": snippet_text,
                "url": chunk_url,
            })

        if not results and not summary:
            return {
                "results": [],
                "query_used": query,
                "note": "Gemini grounding returned no sources. Try rephrasing the query.",
            }

        return {
            "results": results,
            "summary": summary[:1200],
            "query_used": query,
        }

    except Exception as exc:
        return {"results": [], "query_used": query, "error": f"web_search failed: {exc}"}


async def fetch_url(
    url: str,
    max_chars: int = 6000,
    **_kwargs,
) -> Dict[str, Any]:
    """
    Fetch a URL and return its readable text content (HTML tags stripped).

    Returns {"content": str, "url": str, "ok": bool}
    """
    max_chars = min(max(500, max_chars), 12000)
    try:
        import httpx
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=True,
        ) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            raw = r.text

        # Strip <script>, <style>, <nav>, <header>, <footer> blocks entirely
        cleaned = re.sub(r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", "", raw, flags=re.DOTALL | re.IGNORECASE)
        # Strip all remaining HTML tags
        text = re.sub(r"<[^>]+>", " ", cleaned)
        # Collapse whitespace
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated — {len(text) - max_chars} more chars available]"

        return {"content": text, "url": url, "ok": True, "chars": len(text)}

    except Exception as exc:
        return {"content": "", "url": url, "ok": False, "error": f"fetch_url failed: {exc}"}


async def declare_done(summary: str = "", **_kwargs) -> Dict[str, Any]:
    """
    Termination signal for the Big Fixer tool loop.

    In production this call is intercepted by _gemini_complete BEFORE reaching
    dispatch_tool — the summary is returned directly as the completion text.

    This passthrough implementation exists so dispatch_tool doesn't 404 if
    the call somehow slips through (e.g. in unit tests that bypass the
    Gemini layer).
    """
    return {"done": True, "summary": summary}


# ─── Tool dispatcher ──────────────────────────────────────────────────────────

_TOOL_FUNCS = {
    "run_code": run_code,
    "lint_code": lint_code,
    "read_export_file": read_export_file,
    "read_export_files": read_export_files,
    "write_export_file": write_export_file,
    "edit_export_file": edit_export_file,
    "search_docs": search_docs,
    "run_test": run_test,
    "search_code": search_code,
    "install_package_deps": install_package_deps,
    "web_search": web_search,
    "fetch_url": fetch_url,
    "declare_done": declare_done,
}


async def dispatch_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    export_dir: str = "",
) -> Dict[str, Any]:
    """
    Dispatch a tool call by name.

    Injects export_dir into file-operation tools automatically.
    Returns the tool result dict, or an error dict if the tool is unknown.
    """
    func = _TOOL_FUNCS.get(tool_name)
    if func is None:
        return {"error": f"Unknown tool: {tool_name!r}. Available: {list(_TOOL_FUNCS)}"}

    # Pre-dispatch validation: write_export_file requires path + content.
    # Flash-lite models sometimes emit an empty `input {}` on the first
    # TOOL_CALL round.  Return a clear error so the tool-loop follow-up
    # message tells the agent exactly what's missing.
    # NOTE: content="" is VALID (e.g. __init__.py) — only reject if the
    # key is truly absent or None.  Path must be non-empty.
    if tool_name == "write_export_file":
        missing = []
        if "path" not in tool_input or not tool_input["path"]:
            missing.append("path")
        if "content" not in tool_input or tool_input["content"] is None:
            missing.append("content")
        if missing:
            err_msg = (
                f"write_export_file is missing required field(s): {missing}. "
                f"Got input keys: {sorted(k for k in tool_input if k != 'export_dir')}. "
                "Please retry the TOOL_CALL and include both 'path' and 'content' in the input."
            )
            logger.warning("SwarmTool pre-dispatch: %s", err_msg)
            return {"error": err_msg}

    # Inject export_dir for file and test operations
    if tool_name in {"read_export_file", "read_export_files", "write_export_file", "edit_export_file", "run_test", "search_code"}:
        tool_input = {**tool_input, "export_dir": export_dir}

    # Log tool calls at INFO level for debugging visibility (matches Manager tool logging)
    _arg_preview = {k: (v[:80] + '...' if isinstance(v, str) and len(v) > 80 else v)
                    for k, v in tool_input.items() if k != 'export_dir'}
    logger.info("SwarmTool call: %s(%s)", tool_name, _arg_preview)

    try:
        result = await func(**tool_input)
        logger.debug(f"SwarmTool dispatch: {tool_name} → {str(result)[:120]}")
        return result
    except Exception as exc:
        logger.error(f"SwarmTool dispatch error for {tool_name!r}: {exc}")
        return {"error": f"Tool execution error: {exc}"}


# ─── Context block for agent prompts ─────────────────────────────────────────

class SwarmToolContext:
    """
    Renders a compact 'tools available' block for injection into a
    swarm agent's system prompt.

    Agents call tools by emitting a JSON block:
        TOOL_CALL: {"tool": "lint_code", "input": {"code": "...", "language": "python"}}

    agent_pool parses these from the AI response and dispatches them
    via dispatch_tool() before writing the final file.
    """

    HEADER = (
        "\n\n## Swarm Tools Available\n"
        "Call a tool by emitting a line that starts with exactly `TOOL_CALL:` followed by a "
        "single-line JSON object on the SAME line.  Never split a TOOL_CALL across multiple lines.\n"
        "The JSON must have two keys: `tool` (the tool name) and `input` (an object with all "
        "required fields).  Copy the exact field names shown in the examples below.\n\n"
        "### Available Tools\n"
    )

    # Concrete per-tool TOOL_CALL examples — shown verbatim in the prompt so agents
    # can copy-paste the structure rather than interpreting abstract type hints.
    _EXAMPLES = {
        "run_code": (
            'TOOL_CALL: {"tool": "run_code", "input": {"code": "print(1+1)", "language": "python"}}'
        ),
        "lint_code": (
            'TOOL_CALL: {"tool": "lint_code", "input": {"code": "x=1\\ny=2", "language": "python"}}'
        ),
        "read_export_file": (
            'TOOL_CALL: {"tool": "read_export_file", "input": {"path": "mypackage/utils.py"}}'
        ),
        "write_export_file": (
            'TOOL_CALL: {"tool": "write_export_file", "input": {"path": "mypackage/utils.py", "content": "# full file content here"}}'
        ),
        "edit_export_file": (
            'TOOL_CALL: {"tool": "edit_export_file", "input": {"path": "mypackage/utils.py", "old_text": "def old_name(", "new_text": "def new_name("}}'
        ),
        "read_export_files": (
            'TOOL_CALL: {"tool": "read_export_files", "input": {"paths": ["mypackage/main.py", "mypackage/utils.py", "tests/test_main.py"]}}'
        ),
        "search_docs": (
            'TOOL_CALL: {"tool": "search_docs", "input": {"query": "click argument parsing"}}'
        ),
        "run_test": (
            'TOOL_CALL: {"tool": "run_test", "input": {"test_file": "tests/test_processor.py", "test_name": "test_extract_specs_returns_battery_spec"}}'
        ),
        "search_code": (
            'TOOL_CALL: {"tool": "search_code", "input": {"pattern": "BatterySpec(", "file_glob": "*.py"}}'
        ),
        "web_search": (
            'TOOL_CALL: {"tool": "web_search", "input": {"query": "sqlalchemy 2.0 Session execute select example"}}'
        ),
        "fetch_url": (
            'TOOL_CALL: {"tool": "fetch_url", "input": {"url": "https://docs.sqlalchemy.org/en/20/orm/session_api.html"}}'
        ),
        "declare_done": (
            'TOOL_CALL: {"tool": "declare_done", "input": {"summary": "Fixed import error in processor.py and updated BatterySpec constructor signature."}}'
        ),
    }

    @classmethod
    def render(cls, enabled: bool = True) -> str:
        """Return the full tools block for system prompt injection.

        Shows a concrete TOOL_CALL example for every tool so agents know
        exactly what JSON to emit.  Abstract type-hint signatures were
        consistently misread — agents emitted empty input dicts.
        """
        if not enabled:
            return ""
        lines = [cls.HEADER]
        for tool in TOOL_DEFINITIONS:
            name = tool["name"]
            desc = tool["description"].split(".")[0]  # first sentence only
            schema = tool.get("input_schema", {})
            required = schema.get("required", [])
            example = cls._EXAMPLES.get(name, f'TOOL_CALL: {{"tool": "{name}", "input": {{}}}}')
            req_str = ", ".join(f'"{r}"' for r in required)
            lines.append(f"**{name}** — {desc}")
            lines.append(f"  Required fields: {req_str}")
            lines.append(f"  Example: {example}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def extract_tool_calls(ai_response: str) -> List[Dict[str, Any]]:
        """
        Parse TOOL_CALL: JSON lines from an AI response.

        Returns a list of {"tool": str, "input": dict} dicts.

        Robust against two common model failure modes:
        1. "Extra data" — model writes valid JSON then keeps going on the same
           line.  We scan for the first balanced {...} object and stop there.
        2. Truncated / malformed JSON — logged and skipped.
        """
        calls: List[Dict[str, Any]] = []
        for line in ai_response.splitlines():
            stripped = line.strip()
            if not stripped.startswith("TOOL_CALL:"):
                continue
            json_part = stripped[len("TOOL_CALL:"):].strip()
            parsed = None
            # Fast path: whole string is valid JSON
            try:
                parsed = json.loads(json_part)
            except json.JSONDecodeError:
                # Slow path: find the first balanced {...} object in the string
                depth = 0
                start = json_part.find("{")
                if start != -1:
                    for i, ch in enumerate(json_part[start:], start):
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                candidate = json_part[start : i + 1]
                                try:
                                    parsed = json.loads(candidate)
                                except json.JSONDecodeError as exc2:
                                    logger.warning(
                                        f"Failed to parse TOOL_CALL JSON: {exc2} "
                                        f"— line: {json_part[:80]}"
                                    )
                                break
                if parsed is None and start == -1:
                    logger.warning(f"Failed to parse TOOL_CALL JSON — no object found: {json_part[:80]}")
            if isinstance(parsed, dict) and "tool" in parsed:
                calls.append({
                    "tool": parsed["tool"],
                    "input": parsed.get("input", {}),
                })
        return calls
