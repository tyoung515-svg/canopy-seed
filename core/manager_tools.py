"""
Canopy Manager Tools — autonomous debug loop for post-build app repair.

Provides tool definitions (passed to Gemini) and their async dispatcher
implementations.  The Manager agent calls these tools inside a standard
_gemini_complete tool loop — same architecture as the Big Fixer.

Tool set:
  read_file        — read any source file from the app directory
  write_file       — overwrite a source file with corrected content
  restart_app      — stop + relaunch the app, capture startup logs
  probe_endpoint   — HTTP GET/POST against the running app
  screenshot_app   — Playwright headless screenshot (returns base64 PNG)
  get_console_logs — Playwright JS console capture
  run_test         — run pytest suite against the app
  declare_done     — signal completion (termination tool, like Big Fixer)
"""

import asyncio
import base64
import logging
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# ── Tool definitions (Gemini function declarations) ─────────────────────────

MANAGER_TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read a source file from the app directory. Use this to inspect code before deciding on a fix.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the app root. IMPORTANT: include the package directory prefix (e.g. 'my_app/main.py', 'my_app/static/app.js'). Check the file tree in the system prompt for exact paths."
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "read_files",
        "description": (
            "Read MULTIPLE source files in a single call. Much more efficient than calling "
            "read_file multiple times — use this when you need to examine several files at once "
            "(e.g. HTML + JS + backend route). Returns contents of all requested files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of relative paths from the app root (e.g. ['my_app/static/index.html', 'my_app/static/app.js', 'my_app/main.py'])"
                }
            },
            "required": ["paths"]
        }
    },
    {
        "name": "write_file",
        "description": "Overwrite a source file in the app directory with corrected content. Always write the COMPLETE file, not a diff. Prefer edit_file for small targeted changes.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the app root"
                },
                "content": {
                    "type": "string",
                    "description": "Complete corrected file content"
                }
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": (
            "Make a surgical find-and-replace edit in a file. MUCH more efficient than write_file "
            "for targeted fixes — you only specify the exact text to find and its replacement. "
            "Use this for fixing IDs, variable names, import paths, status codes, or any small change. "
            "The old_text must match EXACTLY (including whitespace and indentation)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the app root (e.g. 'my_app/static/app.js')"
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find in the file (must be unique in the file)"
                },
                "new_text": {
                    "type": "string",
                    "description": "The replacement text"
                }
            },
            "required": ["path", "old_text", "new_text"]
        }
    },
    {
        "name": "search_files",
        "description": (
            "Search across ALL files in the app directory for a text pattern. "
            "Returns matching lines with file paths and line numbers. "
            "Use this to find where a variable, ID, class name, route, or import is used "
            "across the entire codebase — much faster than reading files one by one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for (e.g. 'chore-form', 'getElementById', 'def extract')"
                },
                "file_glob": {
                    "type": "string",
                    "description": "Optional glob to filter files (e.g. '*.js', '*.py', '*.html'). Searches all files if omitted."
                }
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "restart_app",
        "description": (
            "Stop the running app process and relaunch it. "
            "Returns startup logs from the first few seconds of uvicorn output. "
            "Required after any backend file change (main.py, routes, services). "
            "Not needed for static file changes (HTML, CSS, JS)."
        ),
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "probe_endpoint",
        "description": (
            "Make an HTTP request to the running app and return the status code and response body. "
            "Use this to verify routes exist, return expected data, and catch 404/500 errors. "
            "Try GET / first as a smoke test, then probe specific API routes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                    "description": "HTTP method"
                },
                "path": {
                    "type": "string",
                    "description": "URL path, e.g. /api/batteries or /api/scan"
                },
                "body": {
                    "type": "object",
                    "description": "JSON body for POST/PUT/PATCH (optional)"
                },
                "content_type": {
                    "type": "string",
                    "description": "Request content-type. Default: application/json",
                }
            },
            "required": ["method", "path"]
        }
    },
    {
        "name": "screenshot_app",
        "description": (
            "Take a screenshot of the app UI using a headless Chromium browser. "
            "Returns a PNG image so you can visually verify layout, button placement, "
            "CSS rendering, and form structure. Use after frontend fixes to confirm visual correctness."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "URL path to navigate to (default: /)"
                },
                "viewport_width": {
                    "type": "integer",
                    "description": "Viewport width in pixels (default: 1280)"
                },
                "viewport_height": {
                    "type": "integer",
                    "description": "Viewport height in pixels (default: 800)"
                }
            }
        }
    },
    {
        "name": "get_console_logs",
        "description": (
            "Capture JavaScript console output (errors, warnings, logs) by loading the app "
            "in a headless browser. Returns up to 50 recent console messages. "
            "Essential for diagnosing frontend JS errors that don't appear in uvicorn logs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "URL path to navigate to (default: /)"
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "Seconds to wait for JS execution (default: 3)"
                }
            }
        }
    },
    {
        "name": "run_test",
        "description": "Run the pytest suite against the app and return pass/fail output. Use this to verify fixes didn't break other functionality.",
        "parameters": {
            "type": "object",
            "properties": {
                "test_file": {
                    "type": "string",
                    "description": "Specific test file name to run (optional — runs all discovered tests if omitted)"
                }
            }
        }
    },
    {
        "name": "declare_done",
        "description": (
            "Signal that debugging is complete. Always call this when finished — "
            "either because the app is working correctly, or because you have exhausted "
            "your diagnostic options and need human review."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What was fixed and current app status"
                },
                "needs_human_review": {
                    "type": "boolean",
                    "description": "True if visual/UX issues remain that need a human to verify"
                },
                "fix_files": {
                    "type": "array",
                    "description": "List of files that were written during this session (for the dashboard to show)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"}
                        }
                    }
                }
            },
            "required": ["summary"]
        }
    },
]


# ── Dispatcher ───────────────────────────────────────────────────────────────

class ManagerToolDispatcher:
    """
    Async dispatcher that binds tool calls to a specific running app.

    Instantiate once per Manager agent session with the app directory and
    the DashboardAPI instance (for access to _launched_apps, launch logic).
    """

    def __init__(
        self,
        app_dir: str,
        app_path: Path,
        api_server,  # DashboardAPI instance
        exports_dir: Path,
    ):
        self.app_dir = app_dir
        self.app_path = app_path
        self.api_server = api_server
        self.exports_dir = exports_dir
        # Track files written during this session for declare_done summary
        self._written_files: Dict[str, str] = {}

    async def dispatch(self, tool_name: str, tool_args: dict) -> Any:
        """Route a tool call to the correct implementation."""
        # Log every tool call for debugging visibility
        _arg_preview = {k: (v[:80] + '...' if isinstance(v, str) and len(v) > 80 else v)
                        for k, v in tool_args.items()}
        logger.info("Manager tool call: %s(%s)", tool_name, _arg_preview)
        handlers = {
            "read_file": self._read_file,
            "read_files": self._read_files,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "search_files": self._search_files,
            "restart_app": self._restart_app,
            "probe_endpoint": self._probe_endpoint,
            "screenshot_app": self._screenshot_app,
            "get_console_logs": self._get_console_logs,
            "run_test": self._run_test,
            "declare_done": self._declare_done,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            return await handler(tool_args)
        except Exception as exc:
            logger.warning("Manager tool %r raised: %s", tool_name, exc)
            return {"error": str(exc)}

    # ── read_file ────────────────────────────────────────────────────────────

    async def _read_file(self, args: dict) -> dict:
        path_str = (args.get("path") or "").strip()
        if not path_str:
            return {"error": "path is required"}

        target = (self.app_path / path_str).resolve()
        # Safety: block reads outside app_path
        try:
            target.relative_to(self.app_path.resolve())
        except ValueError:
            return {"error": f"Path escapes app directory: {path_str}"}

        if not target.exists():
            return {"error": f"File not found: {path_str}"}
        if target.is_dir():
            # Return directory listing instead
            items = sorted(str(p.relative_to(self.app_path)) for p in target.iterdir())
            return {"directory_listing": items}

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            # Truncate very large files
            if len(content) > 20000:
                content = content[:20000] + f"\n... [truncated — {len(content) - 20000} chars remaining]"
            return {"path": path_str, "content": content}
        except Exception as exc:
            return {"error": f"Could not read {path_str}: {exc}"}

    # ── read_files (batch) ────────────────────────────────────────────────────

    async def _read_files(self, args: dict) -> dict:
        paths = args.get("paths") or []
        if not paths:
            return {"error": "paths is required (list of file paths)"}
        if len(paths) > 10:
            paths = paths[:10]  # cap to prevent abuse

        results = []
        for path_str in paths:
            path_str = (path_str or "").strip()
            if not path_str:
                continue
            target = (self.app_path / path_str).resolve()
            try:
                target.relative_to(self.app_path.resolve())
            except ValueError:
                results.append({"path": path_str, "error": f"Path escapes app directory"})
                continue
            if not target.exists():
                results.append({"path": path_str, "error": "File not found"})
                continue
            if target.is_dir():
                items = sorted(str(p.relative_to(self.app_path)) for p in target.iterdir())
                results.append({"path": path_str, "directory_listing": items})
                continue
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
                if len(content) > 12000:
                    content = content[:12000] + f"\n... [truncated — {len(content) - 12000} chars remaining]"
                results.append({"path": path_str, "content": content})
            except Exception as exc:
                results.append({"path": path_str, "error": str(exc)})

        return {"files_read": len(results), "results": results}

    # ── write_file ───────────────────────────────────────────────────────────

    async def _write_file(self, args: dict) -> dict:
        path_str = (args.get("path") or "").strip()
        content = args.get("content", "")
        if not path_str:
            return {"error": "path is required"}
        if content is None:
            return {"error": "content is required"}

        target = (self.app_path / path_str).resolve()
        try:
            target.relative_to(self.app_path.resolve())
        except ValueError:
            return {"error": f"Path escapes app directory: {path_str}"}

        # Block overwrites of test files and pyproject.toml (protect contract)
        _blocked = {"pyproject.toml"}
        if target.name in _blocked:
            return {"error": f"Writing {target.name} is not allowed from the Manager (use the build pipeline)"}
        if target.name.startswith("test_") or target.name.endswith("_test.py"):
            return {"error": "Manager cannot overwrite test/contract files"}

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._written_files[path_str] = content
        logger.info("Manager wrote: %s (%d chars)", path_str, len(content))
        return {"written": path_str, "bytes": len(content.encode("utf-8"))}

    # ── edit_file (surgical find-and-replace) ────────────────────────────────

    async def _edit_file(self, args: dict) -> dict:
        path_str = (args.get("path") or "").strip()
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        if not path_str:
            return {"error": "path is required"}
        if not old_text:
            return {"error": "old_text is required (the exact text to find)"}
        if old_text == new_text:
            return {"error": "old_text and new_text are identical — nothing to change"}

        target = (self.app_path / path_str).resolve()
        try:
            target.relative_to(self.app_path.resolve())
        except ValueError:
            return {"error": f"Path escapes app directory: {path_str}"}

        if not target.exists():
            return {"error": f"File not found: {path_str}"}

        # Block edits to test files and pyproject.toml
        _blocked = {"pyproject.toml"}
        if target.name in _blocked:
            return {"error": f"Editing {target.name} is not allowed from the Manager"}
        if target.name.startswith("test_") or target.name.endswith("_test.py"):
            return {"error": "Manager cannot edit test/contract files"}

        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            return {"error": f"Could not read {path_str}: {exc}"}

        # Count occurrences
        count = content.count(old_text)
        if count == 0:
            # Provide helpful context: show first 200 chars near where it might be
            return {
                "error": f"old_text not found in {path_str}. "
                         f"Make sure whitespace and indentation match exactly. "
                         f"Use search_files or read_file to verify the current content."
            }

        # Apply the replacement (all occurrences)
        new_content = content.replace(old_text, new_text)
        target.write_text(new_content, encoding="utf-8")
        self._written_files[path_str] = new_content
        logger.info("Manager edit_file: %s (%d replacement(s))", path_str, count)
        return {
            "edited": path_str,
            "replacements": count,
            "old_text_preview": old_text[:100],
            "new_text_preview": new_text[:100],
        }

    # ── search_files (grep across app directory) ─────────────────────────────

    async def _search_files(self, args: dict) -> dict:
        import fnmatch
        import re as _re

        pattern_str = (args.get("pattern") or "").strip()
        file_glob = (args.get("file_glob") or "").strip()
        if not pattern_str:
            return {"error": "pattern is required"}

        try:
            pattern = _re.compile(pattern_str, _re.IGNORECASE)
        except _re.error:
            # Fall back to literal search if regex is invalid
            pattern = _re.compile(_re.escape(pattern_str), _re.IGNORECASE)

        matches = []
        files_searched = 0
        for fp in sorted(self.app_path.rglob('*')):
            if not fp.is_file():
                continue
            if fp.name.startswith('.') or '__pycache__' in str(fp):
                continue
            # Apply file glob filter
            if file_glob and not fnmatch.fnmatch(fp.name, file_glob):
                continue
            # Skip binary files
            if fp.suffix in {'.pyc', '.pyo', '.whl', '.egg', '.png', '.jpg', '.gif', '.ico', '.woff', '.woff2', '.ttf'}:
                continue

            files_searched += 1
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            rel_path = str(fp.relative_to(self.app_path)).replace('\\', '/')
            for line_num, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    matches.append({
                        "file": rel_path,
                        "line": line_num,
                        "text": line.strip()[:200],
                    })
                    if len(matches) >= 50:  # cap results
                        break
            if len(matches) >= 50:
                break

        return {
            "pattern": pattern_str,
            "files_searched": files_searched,
            "match_count": len(matches),
            "matches": matches,
        }

    # ── restart_app ──────────────────────────────────────────────────────────

    async def _restart_app(self, _args: dict) -> dict:
        launched = self.api_server._launched_apps

        # Stop if running
        if self.app_dir in launched:
            info = launched[self.app_dir]
            proc = info.get("process")
            if proc and proc.poll() is None:
                import signal as _signal
                import platform as _platform
                try:
                    if _platform.system() == "Windows":
                        proc.terminate()
                    else:
                        proc.send_signal(_signal.SIGTERM)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, proc.wait),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
            del launched[self.app_dir]
            logger.info("Manager stopped app: %s", self.app_dir)

        # Re-use the same launch logic from handle_hub_launch
        launch_result = await self._internal_launch()
        if "error" in launch_result:
            return launch_result

        # Wait for startup then capture logs
        await asyncio.sleep(2.5)
        info = launched.get(self.app_dir, {})
        log_buf = info.get("log_buffer")
        startup_logs = ""
        if log_buf:
            startup_logs = "\n".join(list(log_buf)[-30:])

        proc = info.get("process")
        running = proc is not None and proc.poll() is None

        return {
            "url": launch_result.get("url", ""),
            "startup_logs": startup_logs,
            "running": running,
        }

    async def _internal_launch(self) -> dict:
        """Minimal re-implementation of handle_hub_launch logic for internal use."""
        app_path = self.app_path
        app_dir_name = self.app_dir

        # Discover package name
        package_name = app_dir_name
        pyproject = app_path / "pyproject.toml"
        if pyproject.exists():
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.lower().startswith("name") and "=" in line:
                    package_name = line.split("=", 1)[1].strip().strip("\"'")
                    break

        # Find entry module
        _ENTRY_CANDIDATES = [
            ("app.py", "app"),
            ("main.py", "main"),
            ("run.py", "run"),
            ("server.py", "server"),
        ]
        app_module = None
        for _fname, _mname in _ENTRY_CANDIDATES:
            candidate = app_path / package_name / _fname
            if candidate.exists():
                app_module = f"{package_name}.{_mname}:app"
                break
        if not app_module:
            for _fname, _mname in _ENTRY_CANDIDATES:
                for f in sorted(app_path.rglob(_fname)):
                    rel_parts = list(f.relative_to(app_path).parts)
                    if len(rel_parts) >= 2:
                        app_module = ".".join(rel_parts[:-1]) + f".{_mname}:app"
                        break
                if app_module:
                    break

        if not app_module:
            return {"error": "Could not find app entry point (app.py / main.py)"}

        # Find a free port
        import socket as _socket
        s = _socket.socket()
        s.bind(("", 0))
        port = s.getsockname()[1]
        s.close()

        cmd = [sys.executable, "-m", "uvicorn", app_module,
               "--host", "0.0.0.0", "--port", str(port)]

        import platform as _platform
        extra = {}
        if _platform.system() == "Windows":
            extra["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            proc = subprocess.Popen(
                cmd, cwd=str(app_path),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                **extra,
            )
        except Exception as exc:
            return {"error": f"Failed to launch: {exc}"}

        log_buffer: deque = deque(maxlen=150)

        def _drain(p, buf):
            try:
                for raw_line in p.stdout:
                    buf.append(raw_line.decode("utf-8", errors="replace").rstrip())
            except Exception:
                pass

        threading.Thread(target=_drain, args=(proc, log_buffer), daemon=True).start()

        url = f"http://localhost:{port}"
        self.api_server._launched_apps[app_dir_name] = {
            "process": proc, "port": port, "url": url,
            "name": app_dir_name, "log_buffer": log_buffer,
        }
        logger.info("Manager (re)launched %s on port %d (pid %d)", app_dir_name, port, proc.pid)
        return {"url": url, "port": port}

    # ── probe_endpoint ───────────────────────────────────────────────────────

    async def _probe_endpoint(self, args: dict) -> dict:
        method = (args.get("method") or "GET").upper()
        path = args.get("path") or "/"
        body = args.get("body")
        content_type = args.get("content_type", "application/json")

        # Get current app URL
        info = self.api_server._launched_apps.get(self.app_dir, {})
        app_url = info.get("url")
        if not app_url:
            return {"error": "App is not running. Call restart_app first."}

        proc = info.get("process")
        if proc and proc.poll() is not None:
            return {"error": f"App process exited (return code {proc.returncode}). Call restart_app."}

        url = f"{app_url.rstrip('/')}{path}"
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.request(
                    method, url,
                    json=body if body and "application/json" in content_type else None,
                    headers=headers,
                )
                body_text = resp.text[:4000]
                if len(resp.text) > 4000:
                    body_text += f"\n... [truncated — {len(resp.text) - 4000} chars remaining]"
                return {
                    "status_code": resp.status_code,
                    "ok": 200 <= resp.status_code < 400,
                    "url": str(resp.url),
                    "body": body_text,
                }
        except httpx.ConnectError:
            return {"error": f"Connection refused at {url}. App may still be starting up — wait a moment and retry."}
        except Exception as exc:
            return {"error": str(exc), "ok": False}

    # ── screenshot_app ───────────────────────────────────────────────────────

    async def _screenshot_app(self, args: dict) -> dict:
        path = args.get("path", "/")
        viewport_width = int(args.get("viewport_width") or 1280)
        viewport_height = int(args.get("viewport_height") or 800)

        info = self.api_server._launched_apps.get(self.app_dir, {})
        app_url = info.get("url")
        if not app_url:
            return {"error": "App is not running. Call restart_app first."}

        url = f"{app_url.rstrip('/')}{path}"

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {
                "error": (
                    "Playwright is not installed. "
                    "Run: pip install playwright && playwright install chromium"
                ),
                "fallback": "Use probe_endpoint and get_console_logs instead for text-based diagnostics.",
            }

        console_messages = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(
                    viewport={"width": viewport_width, "height": viewport_height}
                )
                page.on("console", lambda msg: console_messages.append(f"[{msg.type}] {msg.text}"))

                try:
                    await page.goto(url, wait_until="networkidle", timeout=10000)
                except Exception:
                    try:
                        await page.goto(url, wait_until="load", timeout=8000)
                    except Exception as nav_exc:
                        await browser.close()
                        return {"error": f"Navigation failed: {nav_exc}"}

                screenshot_bytes = await page.screenshot(full_page=True)
                await browser.close()

            image_b64 = base64.b64encode(screenshot_bytes).decode()
            logger.info("Manager screenshot: %s (%d bytes PNG)", url, len(screenshot_bytes))
            return {
                # _vision_part is intercepted by the dispatcher wrapper in api_server.py
                # and injected as an inline_data Gemini part for vision analysis.
                "_vision_part": {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": image_b64,
                    }
                },
                "url": url,
                "console_messages": console_messages[-30:],
                "note": "Screenshot captured. Analyze the image to verify visual layout.",
            }

        except Exception as exc:
            logger.warning("Manager screenshot failed: %s", exc)
            return {"error": f"Screenshot failed: {exc}"}

    # ── get_console_logs ─────────────────────────────────────────────────────

    async def _get_console_logs(self, args: dict) -> dict:
        path = args.get("path", "/")
        wait_seconds = int(args.get("wait_seconds") or 3)

        info = self.api_server._launched_apps.get(self.app_dir, {})
        app_url = info.get("url")
        if not app_url:
            return {"error": "App is not running. Call restart_app first."}

        url = f"{app_url.rstrip('/')}{path}"

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {
                "error": (
                    "Playwright is not installed. "
                    "Run: pip install playwright && playwright install chromium"
                ),
            }

        console_messages = []
        page_errors = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                page.on("console", lambda msg: console_messages.append({"type": msg.type, "text": msg.text}))
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))

                try:
                    await page.goto(url, wait_until="load", timeout=10000)
                    await asyncio.sleep(wait_seconds)
                except Exception as nav_exc:
                    await browser.close()
                    return {"error": f"Navigation failed: {nav_exc}"}

                await browser.close()

            return {
                "url": url,
                "console_messages": console_messages[-50:],
                "page_errors": page_errors,
                "error_count": sum(1 for m in console_messages if m["type"] == "error") + len(page_errors),
            }

        except Exception as exc:
            logger.warning("Manager console log capture failed: %s", exc)
            return {"error": f"Console capture failed: {exc}"}

    # ── run_test ─────────────────────────────────────────────────────────────

    async def _run_test(self, args: dict) -> dict:
        test_file = (args.get("test_file") or "").strip()

        # Find test files
        tests_dir = self.app_path / "tests"
        if not tests_dir.exists():
            return {"error": "No tests/ directory found in app"}

        if test_file:
            test_path = tests_dir / test_file
            if not test_path.exists():
                # Try searching
                matches = list(tests_dir.rglob(test_file))
                if not matches:
                    return {"error": f"Test file not found: {test_file}"}
                test_path = matches[0]
            cmd_args = [str(test_path)]
        else:
            cmd_args = [str(tests_dir)]

        cmd = [sys.executable, "-m", "pytest"] + cmd_args + [
            "-v", "--tb=short", "--no-header", "-q",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.app_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                return {"error": "pytest timed out after 120s", "passed": False}

            output = stdout.decode("utf-8", errors="replace")
            passed = proc.returncode == 0

            # Truncate
            if len(output) > 8000:
                output = output[:8000] + f"\n... [truncated — {len(output) - 8000} chars]"

            return {
                "passed": passed,
                "return_code": proc.returncode,
                "output": output,
            }
        except Exception as exc:
            return {"error": f"Failed to run pytest: {exc}", "passed": False}

    # ── declare_done ─────────────────────────────────────────────────────────

    async def _declare_done(self, args: dict) -> dict:
        # This is intercepted upstream (like the Big Fixer's declare_done),
        # but return a dict anyway for logging completeness.
        return {
            "summary": args.get("summary", ""),
            "needs_human_review": args.get("needs_human_review", False),
            "fix_files": args.get("fix_files") or list(
                {"path": p, "content": c} for p, c in self._written_files.items()
            ),
        }
