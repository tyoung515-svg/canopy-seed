"""
Canopy Seed Dashboard API Server
Lightweight HTTP API for the TaskFlow dashboard.
Runs alongside the Telegram bot on port 7821.
"""

import asyncio
import json
import logging
import platform
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from aiohttp import web
from aiohttp.web import middleware

from core.sdk_reference import render_sdk_guidance as _render_sdk

# Project root — always the parent of core/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)

# Pre-rendered SDK guidance — computed once at import time
_sdk_ref_manager = _render_sdk("manager")


def _find_free_port(start: int = 8100, end: int = 8300) -> int:
    """Return an available TCP port in [start, end)."""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('', port))
                return port
            except OSError:
                continue
    raise RuntimeError(f'No free port found in range {start}–{end}')


@middleware
async def cors_middleware(request, handler):
    """Allow requests from file:// and localhost origins."""
    if request.method == 'OPTIONS':
        return web.Response(
            status=204,
            headers={
                'Access-Control-Allow-Origin': 'http://localhost:7822, http://127.0.0.1:7822',
                'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
            }
        )
    response = await handler(request)
    response.headers['Access-Control-Allow-Origin'] = 'http://localhost:7822, http://127.0.0.1:7822'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


class DashboardAPI:
    def __init__(self, settings, context, router, ai_backend):
        self.settings = settings
        self.context = context
        self.router = router
        self.ai_backend = ai_backend
        self.ai = ai_backend
        self.brain_state = None
        self.start_time = time.time()
        self._sse_clients: list = []  # list of aiohttp StreamResponse objects
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._last_swarm_summary = None
        self._canopy_sessions: dict[str, dict] = {}
        self._canopy_session_messages: dict[str, list] = {}
        
        # Build API state
        self._active_build: Optional[dict] = None
        self._inject_queue: list[str] = []
        self._build_sse_clients: list = []

        # Canopy Hub — last user_guided_debug_state payload (persisted across SSE)
        self._last_debug_state: Optional[dict] = None

        # Canopy Hub — launched app subprocesses {app_dir_name: {process, port, url, name}}
        self._launched_apps: dict = {}

        # Agent pool — created immediately so it's never None
        from core.agent_pool import AgentPool
        self.agent_pool = AgentPool(
            ai_backend=ai_backend,
            settings=settings,
            sse_broadcast=self._sse_broadcast,
        )
        self.agent_pool.auto_register_from_settings()
        # Give AgentPool a back-reference so the post-build smoke test can
        # reuse the hub's launch logic and _launched_apps registry.
        self.agent_pool._api_server_ref = self

        # Canopy Seed Components
        from core.context_builder import ContextBuilder
        self.context_builder = ContextBuilder(
            ai_backend=self.ai,
            settings=self.settings,
            sse_broadcast=self._sse_broadcast
        )

    async def start(self):
        """Start the API server."""
        port = getattr(self.settings, 'DASHBOARD_API_PORT', 7821)
        self._app = web.Application(middlewares=[cors_middleware])

        # Routes
        self._app.router.add_get('/api/health', self.handle_health)
        self._app.router.add_get('/api/status', self.handle_status)
        self._app.router.add_post('/api/chat', self.handle_chat)
        self._app.router.add_get('/api/processes', self.handle_processes)
        self._app.router.add_get('/api/models', self.handle_models)
        self._app.router.add_post('/api/vault/push', self.handle_vault_push)
        # ── Built-in Vault (Session 55) ──
        self._app.router.add_get('/api/vault/status',  self.handle_vault_status)
        self._app.router.add_post('/api/vault/setup',  self.handle_vault_setup)
        self._app.router.add_post('/api/vault/unlock', self.handle_vault_unlock)
        self._app.router.add_post('/api/vault/lock',   self.handle_vault_lock)
        self._app.router.add_post('/api/vault/reset',  self.handle_vault_reset)
        self._app.router.add_post('/api/vault/update', self.handle_vault_update)
        # ── Key Provider for exported apps (localhost-only) ──
        self._app.router.add_get('/api/vault/keys', self.handle_vault_keys_export)
        # ── Workflow Profile (ADR-040) ──
        self._app.router.add_get('/api/settings/profile', self.handle_get_profile)
        self._app.router.add_post('/api/settings/profile', self.handle_post_profile)

        # ── Dashboard Store Endpoints ──
        self._app.router.add_get('/api/tasks', self.handle_get_tasks)
        self._app.router.add_post('/api/tasks', self.handle_post_tasks)
        self._app.router.add_get('/api/research_notes', self.handle_get_research_notes)
        self._app.router.add_post('/api/research_notes', self.handle_post_research_notes)
        self._app.router.add_get('/api/notebooks', self.handle_get_notebooks)
        self._app.router.add_post('/api/notebooks', self.handle_post_notebooks)
        self._app.router.add_get('/api/frontend_jobs', self.handle_get_frontend_jobs)
        self._app.router.add_delete('/api/frontend_jobs/{job_id}', self.handle_delete_frontend_job)

        # --- Dev Hub endpoints ---
        self._app.router.add_get('/api/devhub/agents', self.handle_agents_list)
        self._app.router.add_post('/api/devhub/agents/register', self.handle_agent_register)
        self._app.router.add_delete('/api/devhub/agents/{agent_id}', self.handle_agent_remove)
        self._app.router.add_post('/api/devhub/agents/refresh', self.handle_agents_refresh)
        self._app.router.add_get('/api/devhub/agents/summary', self.handle_pool_summary)
        self._app.router.add_get('/api/devhub/events', self.handle_devhub_events)
        self._app.router.add_post('/api/devhub/chat/targeted', self.handle_targeted_chat)
        self._app.router.add_post('/api/devhub/chat/routed', self.handle_routed_chat)
        self._app.router.add_post('/api/devhub/dispatch', self.handle_dispatch)
        self._app.router.add_get('/api/devhub/tasks', self.handle_tasks_list)
        self._app.router.add_get('/api/devhub/tasks/{task_id}', self.handle_task_detail)
        self._app.router.add_get('/api/devhub/tester/run', self.handle_tester_run)
        self._app.router.add_get('/api/devhub/autofix/run', self.handle_autofix_run)
        self._app.router.add_get('/api/devhub/changelog/sessions', self.handle_changelog_sessions)
        self._app.router.add_get('/api/devhub/changelog/export', self.handle_changelog_export)
        self._app.router.add_get('/api/devhub/calibration/run', self.handle_calibration_run)
        self._app.router.add_post('/api/devhub/calibration/apply', self.handle_calibration_apply)
        self._app.router.add_get('/api/devhub/calibration/history', self.handle_calibration_history)
        self._app.router.add_get('/api/devhub/models/available', self.handle_models_available)
        self._app.router.add_get('/api/devhub/models/ollama', self.handle_ollama_models)
        self._app.router.add_post('/api/devhub/files/read', self.handle_file_read)
        self._app.router.add_post('/api/devhub/files/write', self.handle_file_write)
        self._app.router.add_get('/api/devhub/files/tree', self.handle_file_tree)
        self._app.router.add_get('/api/devhub/brain/projects', self.handle_brain_projects)
        self._app.router.add_post('/api/devhub/brain/projects', self.handle_brain_create_project)
        self._app.router.add_get('/api/devhub/brain/projects/{project_id}', self.handle_brain_project_detail)
        self._app.router.add_post('/api/devhub/brain/projects/{project_id}', self.handle_brain_update_project)
        self._app.router.add_get('/api/devhub/brain/log/{project_id}', self.handle_brain_log)
        
        # --- Canopy Seed Specific Routes ---
        # Primary routes
        self._app.router.add_post('/api/canopy/start', self.handle_canopy_start)
        self._app.router.add_post('/api/canopy/message', self.handle_canopy_message)
        self._app.router.add_get('/api/canopy/context', self.handle_canopy_context)
        # Aliases used by CS2/CS3 frontend (/session/ prefix)
        self._app.router.add_post('/api/canopy/session/start', self.handle_canopy_start)
        self._app.router.add_post('/api/canopy/session/message', self.handle_canopy_message)
        self._app.router.add_get('/api/canopy/session/context', self.handle_canopy_context)
        self._app.router.add_post('/api/canopy/session/load', self.handle_canopy_session_load)
        # CS3.5 endpoints
        self._app.router.add_post('/api/canopy/session/export', self.handle_canopy_session_export)
        self._app.router.add_get('/api/canopy/snapshots', self.handle_canopy_snapshots_list)
        self._app.router.add_post('/api/canopy/snapshots/rollback', self.handle_canopy_snapshots_rollback)

        # ── Build API Endpoints ──
        self._app.router.add_post('/api/build/start', self.handle_build_start)
        self._app.router.add_get('/api/build/stream', self.handle_build_stream)
        self._app.router.add_post('/api/inject', self.handle_inject)
        self._app.router.add_post('/api/build/cancel', self.handle_build_cancel)

        # ── Server control ──
        self._app.router.add_post('/api/devhub/server/restart', self.handle_server_restart)

        # ── Canopy Hub API ──
        self._app.router.add_get('/api/hub/apps', self.handle_hub_apps)
        self._app.router.add_get('/api/hub/debug_state', self.handle_hub_debug_state)
        self._app.router.add_post('/api/hub/expand', self.handle_hub_expand)
        self._app.router.add_post('/api/hub/launch', self.handle_hub_launch)
        self._app.router.add_post('/api/hub/stop', self.handle_hub_stop)
        self._app.router.add_get('/api/hub/running', self.handle_hub_running)
        self._app.router.add_post('/api/hub/chat', self.handle_hub_chat)
        self._app.router.add_post('/api/hub/apply_fix', self.handle_hub_apply_fix)

        # ── Static UI files ──
        ui_dir = Path(__file__).parent.parent / 'canopy-ui'
        if ui_dir.exists():
            self._app.router.add_get('/', self._handle_index)
            self._app.router.add_get('/devhub', self._handle_devhub)
            self._app.router.add_get('/hub', self._handle_hub)
            self._app.router.add_static('/ui', ui_dir)
            # Serve canopy-ui static assets (styles.css, app.js, etc.)
            # Must be added AFTER specific routes so /devhub and /hub still take priority
            _ui_static_path = Path(__file__).resolve().parent.parent / "canopy-ui"
            if _ui_static_path.exists():
                self._app.router.add_static('/', path=str(_ui_static_path), name='ui_static', show_index=False, follow_symlinks=False)
            logger.info(f"Serving UI from {ui_dir}")

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, 'localhost', port)
        await site.start()
        logger.info(f"Dashboard API started on http://localhost:{port}") 

    async def stop(self):
        """Stop the API server cleanly."""
        if self._runner:
            await self._runner.cleanup()
            logger.info("Dashboard API stopped.")

    # ── / index ────────────────────────────────────

    async def _handle_index(self, request):
        index = Path(__file__).parent.parent / 'canopy-ui' / 'index.html'
        return web.FileResponse(index)

    async def _handle_devhub(self, request):
        devhub = Path(__file__).parent.parent / 'canopy-ui' / 'devhub.html'
        return web.FileResponse(devhub)

    async def _handle_hub(self, request):
        hub = Path(__file__).parent.parent / 'canopy-ui' / 'hub.html'
        return web.FileResponse(hub)

    # ── /api/health ────────────────────────────────

    async def handle_health(self, request):
        return web.json_response({
            "status": "ok",
            "uptime_seconds": round(time.time() - self.start_time),
        })

    # ── /api/status ────────────────────────────────

    async def handle_status(self, request):
        """PC stats: CPU, RAM, GPU, disk, model info."""
        import psutil

        # CPU
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_freq = psutil.cpu_freq()

        # RAM
        ram = psutil.virtual_memory()

        # Disk
        disks = []
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "drive": part.device,
                    "total_gb": round(usage.total / (1024**3), 1),
                    "used_gb": round(usage.used / (1024**3), 1),
                    "free_gb": round(usage.free / (1024**3), 1),
                    "percent": usage.percent,
                })
            except (PermissionError, OSError):
                continue

        # LM Studio models
        # lm_models = await self._get_lm_models() # Removed undefined method, relying on basic checks

        return web.json_response({
            "hostname": platform.node(),
            "uptime_seconds": round(time.time() - self.start_time),
            "cpu": {
                "percent": cpu_percent,
                "freq_mhz": round(cpu_freq.current) if cpu_freq else None,
                "cores": psutil.cpu_count(logical=False),
                "threads": psutil.cpu_count(logical=True),
            },
            "ram": {
                "total_gb": round(ram.total / (1024**3), 1),
                "used_gb": round(ram.used / (1024**3), 1),
                "available_gb": round(ram.available / (1024**3), 1),
                "percent": ram.percent,
            },
            "disks": disks,
            # "gpus": gpus, # Removed GPU check to simplify dependencies
        })

    # ── /api/chat ──────────────────────────────────

    async def handle_chat(self, request):
        """Send a message through Canopy Seed's full router."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        message = body.get("message", "").strip()
        if not message:
            return web.json_response({"error": "Empty message"}, status=400)

        # Basic stub for router update
        class _DashboardUpdateStub:
            class _Msg:
                class _Chat:
                    async def send_action(self, *args, **kwargs): pass
                chat = _Chat()
                async def reply_text(self, text, parse_mode=None):
                    pass
            message = _Msg()
            effective_user = type('obj', (object,), {'id': 0})
            effective_chat = type('obj', (object,), {'id': 0})

        try:
            update = _DashboardUpdateStub()
            response = await self.router.route(message, update)
            return web.json_response({
                "response": response,
                "message": message,
            })
        except Exception as e:
            logger.error(f"Dashboard chat error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    # ── /api/processes ─────────────────────────────

    async def handle_processes(self, request):
        """Service status + top processes by RAM."""
        import psutil

        checks = {"lm_studio": False, "python": False}
        process_list = []

        for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
            try:
                name = proc.info['name'].lower()
                if 'lm studio' in name or 'lm-studio' in name or 'lmstudio' in name:
                    checks['lm_studio'] = True
                if 'python' in name:
                    checks['python'] = True
                mem_mb = proc.info['memory_info'].rss / (1024**2) if proc.info['memory_info'] else 0
                if mem_mb > 100:
                    process_list.append({
                        "pid": proc.info['pid'],
                        "name": proc.info['name'],
                        "memory_mb": round(mem_mb, 1),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        # Simple port check
        def _check_port(host, port):
             with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                 return s.connect_ex((host, port)) == 0

        port_open = _check_port('localhost', 1234)

        process_list.sort(key=lambda p: p['memory_mb'], reverse=True)

        return web.json_response({
            "services": {
                "canopy": True,
                "lm_studio": checks['lm_studio'],
                "lm_studio_port": port_open,
                "python": checks['python'],
            },
            "top_processes": process_list[:20],
        })

    # ── /api/models ────────────────────────────────

    async def handle_models(self, request):
        """Detailed model info from LM Studio."""
        # Simplified model fetch
        models = []
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{self.settings.LMSTUDIO_BASE_URL}/models")
                if r.status_code == 200:
                   models = r.json().get("data", [])
        except Exception:
            pass
            
        return web.json_response({
            "models": models,
            "primary_configured": self.settings.LMSTUDIO_MODEL,
        })

    async def handle_vault_push(self, request):
        """Push runtime API keys into the live settings object."""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return web.json_response(
                    {"success": False, "error": "JSON body must be an object"},
                    status=400,
                )

            keys_updated: list[str] = []

            if "anthropic_api_key" in body:
                self.settings.ANTHROPIC_API_KEY = body["anthropic_api_key"]
                keys_updated.append("anthropic_api_key")

            if "google_api_key" in body:
                self.settings.GOOGLE_API_KEY = body["google_api_key"]
                self.settings.GEMINI_API_KEY = body["google_api_key"]
                keys_updated.append("google_api_key")

            if "openai_api_key" in body:
                self.settings.OPENAI_API_KEY = body["openai_api_key"]
                keys_updated.append("openai_api_key")

            if "anthropic_api_key" in body and hasattr(self.ai_backend, "_claude_client"):
                self.ai_backend._claude_client = None

            return web.json_response({"success": True, "keys_updated": keys_updated})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ── Built-in Vault Endpoints (Session 55) ───────

    def _forge_connected(self) -> bool:
        """Quick non-blocking check: is the Forge proxy reachable and serving keys?"""
        import urllib.request
        try:
            req = urllib.request.Request("http://localhost:5151/vault/keys")
            with urllib.request.urlopen(req, timeout=0.4) as resp:
                data = __import__("json").loads(resp.read().decode())
                return bool(data.get("keys"))
        except Exception:
            return False

    async def handle_vault_status(self, request):
        """GET /api/vault/status — return vault + Forge connection state.

        Response:
            {
              "setup":           bool,   # vault.enc exists and is valid
              "unlocked":        bool,   # vault decrypted this session
              "forge_connected": bool,   # Forge proxy at :5151 has live keys
              "active_profile":  str,    # current WORKFLOW_PROFILE
            }
        """
        try:
            from memory.vault_store import vault_status
            status = vault_status()
            status["forge_connected"] = self._forge_connected()
            status["active_profile"] = (
                getattr(self.settings, "WORKFLOW_PROFILE", "gemini") or "gemini"
            )
            return web.json_response(status)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_vault_setup(self, request):
        """POST /api/vault/setup — first-time vault creation.

        Body:
            {
              "keys":     { "google_api_key": "...", "anthropic_api_key": "...", ... },
              "password": "strong-passphrase",
              "profile":  "gemini"   (optional, defaults to "gemini")
            }

        Creates memory/vault.enc, immediately unlocks it (live-pushes keys to
        settings), and optionally switches the active workflow profile.
        """
        try:
            from memory.vault_store import setup_vault
            body = await request.json()
            keys     = body.get("keys", {})
            password = body.get("password", "")
            profile  = body.get("profile", "gemini")

            if not isinstance(keys, dict) or not keys:
                return web.json_response(
                    {"success": False, "error": "keys must be a non-empty object"},
                    status=400,
                )
            if not password:
                return web.json_response(
                    {"success": False, "error": "password must not be empty"},
                    status=400,
                )

            setup_vault(keys, password)   # auto-unlocks after creation
            self._push_keys_to_settings(keys)

            # Switch profile and align DEFAULT_AI_BACKEND to match
            from config.profiles import VALID_PROFILES
            if profile in VALID_PROFILES:
                self.settings.WORKFLOW_PROFILE = profile
            # Ensure DEFAULT_AI_BACKEND matches the chosen profile so all
            # code paths (including context_builder) use the right backend.
            profile_to_backend = {
                "gemini": "gemini",
                "claude": "claude",
                "qwen":   "qwen",
            }
            if profile in profile_to_backend:
                self.settings.DEFAULT_AI_BACKEND = profile_to_backend[profile]

            logger.info("Built-in vault created and unlocked via /api/vault/setup")
            return web.json_response({
                "success": True,
                "keys_loaded": list(keys.keys()),
                "active_profile": self.settings.WORKFLOW_PROFILE,
            })
        except FileExistsError as e:
            return web.json_response({"success": False, "error": str(e)}, status=409)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_vault_unlock(self, request):
        """POST /api/vault/unlock — decrypt the vault and push keys to live settings.

        Body: {"password": "..."}

        On success the in-process settings object is mutated — all subsequent
        AI calls immediately pick up the new keys without a restart.
        """
        try:
            from memory.vault_store import unlock, is_vault_setup
            body = await request.json()
            password = body.get("password", "")
            if not password:
                return web.json_response(
                    {"success": False, "error": "password must not be empty"},
                    status=400,
                )
            if not is_vault_setup():
                return web.json_response(
                    {"success": False, "error": "No vault configured. Use /api/vault/setup first."},
                    status=404,
                )

            keys = unlock(password)
            self._push_keys_to_settings(keys)
            logger.info("Built-in vault unlocked via /api/vault/unlock")
            return web.json_response({
                "success": True,
                "keys_loaded": list(keys.keys()),
            })
        except ValueError as e:
            # Wrong password / corrupted file
            return web.json_response({"success": False, "error": str(e)}, status=401)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_vault_lock(self, request):
        """POST /api/vault/lock — wipe in-memory keys for this session."""
        try:
            from memory.vault_store import lock
            lock()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_vault_reset(self, request):
        """POST /api/vault/reset — delete vault.enc from disk (password recovery).

        WARNING: this is irreversible. The user must run setup again.
        """
        try:
            from memory.vault_store import reset_vault
            reset_vault()
            logger.warning("Built-in vault deleted via /api/vault/reset")
            return web.json_response({"success": True, "message": "Vault deleted. Run setup to create a new one."})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_vault_update(self, request):
        """POST /api/vault/update — verify password, merge new keys, re-encrypt vault.

        Body: {"password": "...", "keys": {"google_api_key": "new-key", ...}}

        Verifies the current password, merges the supplied keys into the vault
        (existing keys not in the payload are preserved), re-encrypts, and
        immediately live-pushes the merged keys to the running settings object.
        """
        try:
            from memory.vault_store import update_vault, is_vault_setup
            body = await request.json()
            password = body.get('password', '')
            keys     = body.get('keys') or {}

            if not password:
                return web.json_response({'success': False, 'error': 'password is required'}, status=400)
            if not isinstance(keys, dict) or not keys:
                return web.json_response({'success': False, 'error': 'keys must be a non-empty object'}, status=400)
            if not is_vault_setup():
                return web.json_response({'success': False, 'error': 'No vault configured. Use /api/vault/setup first.'}, status=404)

            merged = update_vault(password, keys)
            self._push_keys_to_settings(merged)
            logger.info('Vault updated via /api/vault/update (%d keys)', len(merged))
            return web.json_response({'success': True, 'keys_updated': list(keys.keys())})
        except ValueError as e:
            return web.json_response({'success': False, 'error': str(e)}, status=401)
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=400)

    async def handle_vault_keys_export(self, request):
        """GET /api/vault/keys — return live API keys to local exported apps.

        Localhost-only endpoint.  Exported apps (battery_datasheet_scanner, etc.)
        call this at startup so they share the single Canopy vault rather than
        maintaining their own key storage.

        Security:
          - Only responds to connections from 127.0.0.1 / ::1.
          - Returns keys only when the vault is currently unlocked.
          - Key values are redacted in logs; only names are logged.

        Response (vault unlocked):
            200 { "keys": { "GOOGLE_API_KEY": "...", "ANTHROPIC_API_KEY": "...", ... },
                  "unlocked": true }

        Response (vault locked / not set up):
            200 { "keys": {}, "unlocked": false,
                  "note": "Vault is locked — unlock it in the Canopy dashboard." }
        """
        # Restrict to loopback only
        peer = request.transport.get_extra_info("peername")
        peer_ip = peer[0] if peer else ""
        if peer_ip not in ("127.0.0.1", "::1", "localhost"):
            return web.json_response(
                {"error": "Forbidden — this endpoint is localhost-only"},
                status=403,
            )

        try:
            from memory.vault_store import is_unlocked, get_unlocked_keys, _KEY_ENV_MAP
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        if not is_unlocked():
            return web.json_response({
                "keys": {},
                "unlocked": False,
                "note": "Canopy vault is locked — unlock it in the dashboard first.",
            })

        raw_keys = get_unlocked_keys()
        # Translate vault key names → env var names for the client
        exported: dict = {}
        for vault_key, env_var in _KEY_ENV_MAP.items():
            val = raw_keys.get(vault_key, "")
            if isinstance(val, str) and val.strip():
                exported[env_var] = val.strip()
        # GEMINI_API_KEY alias
        if "GOOGLE_API_KEY" in exported:
            exported["GEMINI_API_KEY"] = exported["GOOGLE_API_KEY"]

        logger.info(
            "Vault keys served to exported app at %s (keys: %s)",
            peer_ip,
            list(exported.keys()),
        )
        return web.json_response({"keys": exported, "unlocked": True})

    def _push_keys_to_settings(self, keys: dict) -> None:
        """Apply a key dict to the live settings object and os.environ.

        Updating os.environ ensures that any tool using os.getenv() —
        including swarm_tools.web_search, spawned subprocesses, and
        exported apps on the same machine — can see the keys without
        importing vault_store directly.
        """
        if "google_api_key" in keys:
            self.settings.GOOGLE_API_KEY = keys["google_api_key"]
            self.settings.GEMINI_API_KEY = keys["google_api_key"]
        if "anthropic_api_key" in keys:
            self.settings.ANTHROPIC_API_KEY = keys["anthropic_api_key"]
            if hasattr(self.ai_backend, "_claude_client"):
                self.ai_backend._claude_client = None   # force reconnect
        if "openai_api_key" in keys:
            self.settings.OPENAI_API_KEY = keys["openai_api_key"]
        if "qwen_api_key" in keys:
            self.settings.QWEN_API_KEY = keys["qwen_api_key"]
        # Mirror to os.environ so os.getenv() works in all tools/subprocesses
        try:
            from memory.vault_store import _push_to_env
            _push_to_env(keys)
        except Exception:
            pass  # non-fatal — settings object is the primary source

    # ── Workflow Profile Endpoints (ADR-040) ────────

    async def handle_get_profile(self, request):
        """GET /api/settings/profile — return active profile info."""
        try:
            from config.profiles import profile_info, VALID_PROFILES
            active = getattr(self.settings, "WORKFLOW_PROFILE", "gemini") or "gemini"
            return web.json_response({
                "active_profile": active,
                "valid_profiles": VALID_PROFILES,
                "profile_info": profile_info(active),
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_post_profile(self, request):
        """POST /api/settings/profile — switch active workflow profile at runtime.

        Body: {"profile": "gemini" | "claude" | "qwen"}

        Mutates settings.WORKFLOW_PROFILE so subsequent get_role_chain() calls
        immediately return chains from the new profile.  No restart required.
        """
        try:
            from config.profiles import VALID_PROFILES, profile_info
            body = await request.json()
            if not isinstance(body, dict) or "profile" not in body:
                return web.json_response(
                    {"success": False, "error": "body must be {\"profile\": \"<name>\"}"},
                    status=400,
                )
            requested = str(body["profile"]).strip().lower()
            if requested not in VALID_PROFILES:
                return web.json_response(
                    {
                        "success": False,
                        "error": f"Unknown profile '{requested}'. Valid: {VALID_PROFILES}",
                    },
                    status=400,
                )
            previous = getattr(self.settings, "WORKFLOW_PROFILE", "gemini") or "gemini"
            self.settings.WORKFLOW_PROFILE = requested
            logger.info(f"Workflow profile switched: {previous} → {requested}")
            return web.json_response({
                "success": True,
                "previous_profile": previous,
                "active_profile": requested,
                "profile_info": profile_info(requested),
            })
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ── Dashboard Data Store Endpoints ─────────────

    async def handle_get_tasks(self, request):
        try:
            from memory.dashboard_store import get_tasks
            return web.json_response(get_tasks())
        except ImportError:
            return web.json_response({"tasks": []}) # Fallback if store not ported yet
        
    async def handle_post_tasks(self, request):
        from memory.dashboard_store import set_tasks
        try:
            tasks = await request.json()
            set_tasks(tasks)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_get_research_notes(self, request):
        try:
            from memory.dashboard_store import get_research_notes
            return web.json_response({"notes": get_research_notes()})
        except ImportError:
             return web.json_response({"notes": ""})
             
    async def handle_post_research_notes(self, request):
        from memory.dashboard_store import set_research_notes
        try:
            body = await request.json()
            set_research_notes(body.get("notes", ""))
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_get_notebooks(self, request):
        try:
            from memory.dashboard_store import get_notebooks
            return web.json_response(get_notebooks())
        except ImportError:
            return web.json_response({"notebooks": []})
        
    async def handle_post_notebooks(self, request):
        from memory.dashboard_store import set_notebooks
        try:
            notebooks = await request.json()
            set_notebooks(notebooks)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_get_frontend_jobs(self, request):
        try:
            from memory.dashboard_store import get_frontend_jobs
            return web.json_response(get_frontend_jobs())
        except ImportError:
            return web.json_response({"jobs": []})
        
    async def handle_delete_frontend_job(self, request):
        from memory.dashboard_store import remove_frontend_job
        job_id = request.match_info.get('job_id')
        if job_id:
            remove_frontend_job(job_id)
        return web.json_response({"success": True})

    # ── Dev Hub: Agent Pool ──────────────────────────

    async def handle_agents_list(self, request):
        agents = self.agent_pool.get_agents()
        return web.json_response({"agents": agents})

    async def handle_agent_register(self, request):
        """Dynamically register a new agent into the pool."""
        try:
            data = await request.json()
            name = data.get('name', '').strip()
            agent_type = data.get('type', '').strip()
            model_id = data.get('model_id', '').strip()
            count = max(1, min(int(data.get('count', 1)), 20))

            if not name or not agent_type or not model_id:
                return web.json_response({'error': 'name, type, and model_id are required'}, status=400)

            if self.agent_pool is None:
                return web.json_response({'error': 'Agent pool not initialized'}, status=503)

            from core.agent_pool import AgentConfig, AgentType

            type_map = {
                'claude': AgentType.CLAUDE,
                'gemini': AgentType.GEMINI,
                'lmstudio': AgentType.LMSTUDIO,
                'ollama': AgentType.OLLAMA,
            }
            if agent_type not in type_map:
                return web.json_response({'error': f'Unsupported agent type: {agent_type}'}, status=400)

            registered = []
            for i in range(count):
                suffix = f'-{i+1}' if count > 1 else ''
                agent_id = f'{agent_type}-{model_id.split("/")[-1].split(":")[0]}{suffix}-{int(time.time())}'
                config = AgentConfig(
                    id=agent_id,
                    name=f'{name}{" #"+str(i+1) if count > 1 else ""}',
                    agent_type=type_map[agent_type],
                    endpoint=self._endpoint_for_type(agent_type),
                    model_id=model_id,
                    capabilities=['code', 'chat'],
                )
                self.agent_pool.register_agent(config)
                registered.append(agent_id)

            return web.json_response({'registered': registered, 'count': len(registered)})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_agent_remove(self, request):
        """Remove an agent from the pool by id."""
        try:
            agent_id = request.match_info.get('agent_id', '')
            if not agent_id:
                return web.json_response({'error': 'agent_id required'}, status=400)
            if self.agent_pool is None:
                return web.json_response({'error': 'Agent pool not initialized'}, status=503)
            removed = self.agent_pool.remove_agent(agent_id)
            if removed:
                return web.json_response({'removed': agent_id})
            return web.json_response({'error': 'Agent not found'}, status=404)
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    def _endpoint_for_type(self, agent_type: str) -> str:
        endpoints = {
            'claude': 'https://api.anthropic.com/v1',
            'gemini': 'https://generativelanguage.googleapis.com/v1',
            'lmstudio': 'http://localhost:1234/v1',
            'ollama': 'http://localhost:11434',
        }
        return endpoints.get(agent_type, '')

    async def handle_agents_refresh(self, request):
        await self.agent_pool.refresh_agent_status()
        return web.json_response({"status": "refreshed", "agents": self.agent_pool.get_agents()})

    async def handle_pool_summary(self, request):
        return web.json_response(self.agent_pool.get_pool_summary())

    # ── Dev Hub: Targeted Chat ──────────────────────────

    async def handle_targeted_chat(self, request):
        """Send a message to a specific backend/model. Used by Dev Hub chat panels."""
        body = await request.json()
        message = body.get("message", "").strip()
        backend = body.get("backend", "lmstudio")
        model_id = body.get("model_id", "")
        history = body.get("history", [])
        endpoint = body.get("endpoint", "")
        max_tokens = body.get("max_tokens", 4096)

        _default_prompt = (
            self.router._build_system_prompt()
            if backend == "claude"
            else "You are a helpful coding assistant."
        )
        system_prompt = body.get("system_prompt") or _default_prompt

        if not message:
            return web.json_response({"error": "Empty message"}, status=400)

        try:
            result = await self.ai.targeted_complete(
                backend=backend,
                model_id=model_id,
                system=system_prompt,
                message=message,
                history=history,
                endpoint=endpoint,
                max_tokens=max_tokens,
            )
            asyncio.ensure_future(self._sse_broadcast({
                'type': 'agent_response',
                'backend': body.get('backend', ''),
                'model_id': body.get('model_id', ''),
                'duration_ms': result.get('duration_ms', 0),
                'ts': int(time.time() * 1000),
            }))
            return web.json_response(result)
        except Exception as e:
            logger.error(f"Targeted chat error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_routed_chat(self, request):
        """
        Route a message through the full engine (_handle_ai loop).
        """
        body = await request.json()
        message = body.get("message", "").strip()
        backend = body.get("backend", "claude")

        if not message:
            return web.json_response({"error": "Empty message"}, status=400)

        # Basic stub for routed chat interaction
        class _Stub:
            class chat:
                @staticmethod
                async def send_action(*args, **kwargs): pass
            @staticmethod
            async def reply_text(*args, **kwargs): pass
        
        class _UpdateStub:
            message = _Stub()

        try:
            t0 = time.time()
            result_text = await self.router._handle_ai(
                message=message,
                update=_UpdateStub(),
                backend=backend,
            )
            duration_ms = round((time.time() - t0) * 1000)
            return web.json_response({
                "content": result_text,
                "backend": backend,
                "duration_ms": duration_ms,
                "routed": True,
            })
        except Exception as e:
            logger.error(f"Routed chat error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_devhub_events(self, request):
        """SSE stream for Dev Hub agent activity events."""
        response = web.StreamResponse()
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['X-Accel-Buffering'] = 'no'
        await response.prepare(request)
        self._sse_clients.append(response)
        try:
            # Send initial connected event
            await response.write(b'data: {"type":"connected"}\n\n')
            # Keep alive until client disconnects
            while True:
                await asyncio.sleep(15)
                try:
                    await response.write(b': keepalive\n\n')
                except Exception:
                    break
        finally:
            if response in self._sse_clients:
                self._sse_clients.remove(response)
        return response

    async def _sse_broadcast(self, event_type: str, data: dict):
        """Broadcast a named SSE event to all connected clients.

        Called by ContextBuilder as: await sse_broadcast(event_type, data)
        Emits proper named SSE events so the frontend can use addEventListener(type, ...).
        """
        import json as _json

        # Cache debug state for the Hub to retrieve later
        if event_type == "user_guided_debug_state":
            self._last_debug_state = dict(data)

        payload = (
            f"event: {event_type}\n"
            f"data: {_json.dumps(data)}\n\n"
        ).encode()
        dead = []
        for client in self._sse_clients:
            try:
                await client.write(payload)
            except Exception:
                dead.append(client)
        for d in dead:
            self._sse_clients.remove(d)

    # ── Canopy Hub API handlers ────────────────────────────────────────────────

    async def handle_hub_apps(self, request):
        """GET /api/hub/apps — list all built apps from the exports directory."""
        exports_dir = _PROJECT_ROOT / 'exports'
        apps = []
        if exports_dir.exists():
            for app_dir in sorted(exports_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if not app_dir.is_dir() or app_dir.name.startswith('.'):
                    continue
                ctx_file = app_dir / 'PROJECT_CONTEXT.json'
                overview_file = app_dir / 'PROJECT_OVERVIEW.md'
                # Count test files and source files
                py_files = list(app_dir.rglob('*.py'))
                test_files = [f for f in py_files if f.name.startswith('test_') or f.name.endswith('_test.py')]
                ctx_data: dict = {}
                if ctx_file.exists():
                    try:
                        import json as _j
                        ctx_data = _j.loads(ctx_file.read_text(encoding='utf-8'))
                    except Exception:
                        pass
                apps.append({
                    'name': ctx_data.get('project_name') or ctx_data.get('name') or app_dir.name,
                    'dir': app_dir.name,
                    'export_path': str(app_dir),
                    'modified': app_dir.stat().st_mtime,
                    'has_context': ctx_file.exists(),
                    'has_overview': overview_file.exists(),
                    'source_files': len(py_files) - len(test_files),
                    'test_files': len(test_files),
                })
        return web.json_response({'apps': apps})

    async def handle_hub_debug_state(self, request):
        """GET /api/hub/debug_state — return the last user_guided_debug_state payload."""
        if self._last_debug_state is None:
            return web.json_response({'debug_state': None, 'message': 'No debug session recorded yet.'})
        return web.json_response({'debug_state': self._last_debug_state})

    async def handle_hub_expand(self, request):
        """POST /api/hub/expand — kick off a new expansion build from an existing context.

        Body: { export_dir: str, expansion_prompt: str }
        Loads the existing PROJECT_CONTEXT.json, injects the expansion prompt, and
        starts a new build session. The build streams via /api/build/stream SSE.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON body'}, status=400)

        export_dir = body.get('export_dir', '').strip()
        expansion_prompt = body.get('expansion_prompt', '').strip()

        if not export_dir:
            return web.json_response({'error': 'export_dir is required'}, status=400)

        ctx_path = Path(export_dir) / 'PROJECT_CONTEXT.json'
        if not ctx_path.exists():
            # Try relative to exports/
            ctx_path = _PROJECT_ROOT / 'exports' / export_dir / 'PROJECT_CONTEXT.json'

        if not ctx_path.exists():
            return web.json_response({'error': f'PROJECT_CONTEXT.json not found at {export_dir}'}, status=404)

        try:
            import json as _j
            ctx = _j.loads(ctx_path.read_text(encoding='utf-8'))
        except Exception as e:
            return web.json_response({'error': f'Could not read context: {e}'}, status=500)

        project_name = ctx.get('project_name') or ctx.get('name') or export_dir
        session_id = str(uuid.uuid4())[:8]

        # Inject the expansion prompt into the context and queue a new build
        ctx['expansion_prompt'] = expansion_prompt
        ctx['is_expansion'] = True
        ctx['parent_export_dir'] = export_dir

        # Store as active build context
        self._active_build = {
            'session_id': session_id,
            'project_name': project_name,
            'context': ctx,
            'export_dir': export_dir,
            'started_at': time.time(),
            'is_expansion': True,
        }

        # Trigger build asynchronously
        async def _run_expansion():
            try:
                from core.agent_pool import AgentPool
                pool = AgentPool(
                    ai_backend=self.ai,
                    settings=self.settings,
                    sse_broadcast=self._build_sse_broadcast,
                )
                await self._broadcast("build_started", {
                    "session_id": session_id,
                    "project_name": project_name,
                    "is_expansion": True,
                })
                await pool.run_orchestrated_build(
                    project_context=ctx,
                    session_id=session_id,
                    export_dir=str(_PROJECT_ROOT / 'exports' / export_dir),
                )
            except Exception as exc:
                logger.exception(f"Hub expansion build failed: {exc}")
                await self._broadcast("build_error", {"session_id": session_id, "error": str(exc)})

        asyncio.create_task(_run_expansion())

        return web.json_response({
            'ok': True,
            'session_id': session_id,
            'project_name': project_name,
            'message': f'Expansion build started for {project_name}. Stream via /api/build/stream.',
        })

    # ── Canopy Hub: App Launcher ────────────────────────

    async def handle_hub_launch(self, request):
        """POST /api/hub/launch — start a built app as a subprocess.

        Body: { app_dir: str }  (name of folder inside exports/)
        Returns: { url, port, already_running }
        """
        import sys as _sys
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON body'}, status=400)

        app_dir_name = body.get('app_dir', '').strip()
        if not app_dir_name:
            return web.json_response({'error': 'app_dir is required'}, status=400)

        exports_dir = _PROJECT_ROOT / 'exports'
        app_path = exports_dir / app_dir_name
        if not app_path.exists():
            return web.json_response({'error': f'App directory not found: {app_dir_name}'}, status=404)

        # Already running and still alive? Return existing URL.
        if app_dir_name in self._launched_apps:
            info = self._launched_apps[app_dir_name]
            if info['process'].poll() is None:
                return web.json_response({'url': info['url'], 'port': info['port'], 'already_running': True})
            del self._launched_apps[app_dir_name]

        # Discover package name from pyproject.toml
        package_name = app_dir_name
        pyproject = app_path / 'pyproject.toml'
        if pyproject.exists():
            for line in pyproject.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line.lower().startswith('name') and '=' in line:
                    package_name = line.split('=', 1)[1].strip().strip('"\'')
                    break

        # Find entry module → uvicorn module string
        # Try common FastAPI entry-point filenames in priority order
        _ENTRY_CANDIDATES = [
            ('app.py',    'app'),
            ('main.py',   'main'),
            ('run.py',    'run'),
            ('server.py', 'server'),
        ]
        app_module = None
        # 1) Check <package_name>/<filename> directly
        for _fname, _mname in _ENTRY_CANDIDATES:
            candidate = app_path / package_name / _fname
            if candidate.exists():
                app_module = f'{package_name}.{_mname}:app'
                break
        # 2) Recursive search for any matching filename anywhere under the export dir
        if not app_module:
            for _fname, _mname in _ENTRY_CANDIDATES:
                for f in sorted(app_path.rglob(_fname)):
                    rel_parts = list(f.relative_to(app_path).parts)
                    if len(rel_parts) >= 2:
                        app_module = '.'.join(rel_parts[:-1]) + f'.{_mname}:app'
                        break
                if app_module:
                    break

        if not app_module:
            return web.json_response(
                {'error': 'Could not find app entry point (app.py / main.py) in the built package'},
                status=404,
            )

        port = _find_free_port()
        cmd = [_sys.executable, '-m', 'uvicorn', app_module,
               '--host', '0.0.0.0', '--port', str(port)]

        extra = {}
        if platform.system() == 'Windows':
            extra['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            proc = subprocess.Popen(
                cmd, cwd=str(app_path),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                **extra
            )
        except Exception as exc:
            return web.json_response({'error': f'Failed to launch: {exc}'}, status=500)

        # Capture uvicorn stdout in a background thread so the hub chat can read it
        import threading
        from collections import deque
        log_buffer: deque = deque(maxlen=150)

        def _drain_stdout(p, buf):
            try:
                for raw_line in p.stdout:
                    buf.append(raw_line.decode('utf-8', errors='replace').rstrip())
            except Exception:
                pass

        threading.Thread(target=_drain_stdout, args=(proc, log_buffer), daemon=True).start()

        url = f'http://localhost:{port}'
        self._launched_apps[app_dir_name] = {
            'process': proc, 'port': port, 'url': url, 'name': app_dir_name,
            'log_buffer': log_buffer,
        }
        logger.info(f'Hub launched app {app_dir_name} on port {port} (pid {proc.pid})')

        # Give uvicorn a moment to bind
        await asyncio.sleep(1.5)
        return web.json_response({'url': url, 'port': port, 'already_running': False})

    async def handle_hub_stop(self, request):
        """POST /api/hub/stop — stop a running app subprocess.

        Body: { app_dir: str }
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON body'}, status=400)

        app_dir_name = body.get('app_dir', '').strip()
        if app_dir_name not in self._launched_apps:
            return web.json_response({'error': 'App is not running'}, status=404)

        info = self._launched_apps.pop(app_dir_name)
        proc = info['process']
        try:
            if platform.system() == 'Windows':
                subprocess.call(
                    ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        logger.info(f'Hub stopped app {app_dir_name}')
        return web.json_response({'stopped': True})

    async def handle_hub_running(self, request):
        """GET /api/hub/running — return all currently running apps with their URLs."""
        running = {}
        dead = []
        for app_dir, info in self._launched_apps.items():
            if info['process'].poll() is None:
                running[app_dir] = {'url': info['url'], 'port': info['port']}
            else:
                dead.append(app_dir)
        for d in dead:
            del self._launched_apps[d]
        return web.json_response({'running': running})

    async def handle_hub_chat(self, request):
        """POST /api/hub/chat — Hub-aware AI agent for autonomous app debugging.

        Upgraded from prompt→JSON to a full tool-calling agent loop.
        The Manager can read files, write fixes, restart the app, probe
        endpoints, take screenshots, and run tests autonomously — without
        human round-trips for diagnosable issues.

        Body: {
            message: str,
            history: [{role, content}],   // recent chat turns
            context: { app: {...} }        // selected app info
        }
        Returns: { response: str, fix_files: [{path, content}] | [] }
        """
        import re as _re
        import html as _html

        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON body'}, status=400)

        message = body.get('message', '').strip()
        if not message:
            return web.json_response({'error': 'message is required'}, status=400)

        app_ctx  = (body.get('context') or {}).get('app') or {}
        app_name = app_ctx.get('name', '') or app_ctx.get('dir', '') or 'this app'
        app_dir  = app_ctx.get('dir', '').strip()

        # Convert HTML chat history to plain text for the AI (last 8 turns)
        raw_history = body.get('history') or []
        clean_history = []
        for msg in raw_history[-8:]:
            role = 'assistant' if msg.get('role') == 'assistant' else 'user'
            text = _html.unescape(_re.sub(r'<[^>]+>', ' ', str(msg.get('content', ''))))
            text = _re.sub(r'\s+', ' ', text).strip()
            if text:
                clean_history.append({'role': role, 'content': text})

        # ── Build app context for the system prompt ───────────────────────────
        app_path = _PROJECT_ROOT / 'exports' / app_dir if app_dir else None

        process_status = ''
        if app_dir and app_dir in self._launched_apps:
            info = self._launched_apps[app_dir]
            if info['process'].poll() is None:
                process_status = f'\nThe app is **running** at {info["url"]}.'
            else:
                rc = info['process'].returncode
                process_status = (
                    f'\nThe app process **exited** (return code {rc}) — it crashed after launch. '
                    f'Call restart_app to relaunch it.'
                )
            log_buf = info.get('log_buffer')
            if log_buf:
                recent = list(log_buf)[-40:]
                if recent:
                    process_status += (
                        '\n\n### Recent app logs (uvicorn stdout)\n```\n'
                        + '\n'.join(recent) + '\n```'
                    )
        elif app_dir:
            process_status = '\nThe app is **not running**. Call restart_app to launch it.'

        # ── Load Manager repair memory (persists across chat sessions) ────────
        import json as _json
        from datetime import datetime as _dt
        repair_memory_section = ''
        _memory_file = app_path / '.manager_memory.json' if app_path else None
        _memory_entries = []
        if _memory_file and _memory_file.exists():
            try:
                _memory_entries = _json.loads(_memory_file.read_text(encoding='utf-8'))
                if _memory_entries:
                    _mem_lines = []
                    for entry in _memory_entries[-8:]:  # last 8 sessions
                        ts = entry.get('timestamp', '?')
                        summary = entry.get('summary', 'no summary')
                        files_edited = entry.get('files_edited', [])
                        _mem_lines.append(f'- [{ts}] {summary}')
                        if files_edited:
                            _mem_lines.append(f'  Files changed: {", ".join(files_edited)}')
                    repair_memory_section = (
                        '\n\n## Previous Manager sessions (DO NOT re-investigate these — build on them)\n'
                        + '\n'.join(_mem_lines)
                        + '\n\nFocus on what has NOT been tried yet. If a previous fix was partial, '
                        'continue from where it left off — do not start from scratch.'
                    )
            except Exception:
                pass  # corrupted memory file, ignore

        # Build a file tree so the Manager knows exact paths (prevents wasted read_file rounds)
        file_tree_section = ''
        if app_path and app_path.exists():
            _tree_lines = []
            _count = 0
            for fp in sorted(app_path.rglob('*')):
                if fp.is_file() and not fp.name.startswith('.') and '__pycache__' not in str(fp):
                    rel = str(fp.relative_to(app_path)).replace('\\', '/')
                    _tree_lines.append(rel)
                    _count += 1
                    if _count >= 60:  # cap to avoid prompt bloat
                        _tree_lines.append('... (truncated)')
                        break
            if _tree_lines:
                file_tree_section = (
                    '\n\n## File tree (use these EXACT paths with read_file / write_file)\n```\n'
                    + '\n'.join(_tree_lines) + '\n```'
                )

        # Inject latest swarm test failures if available
        test_context = ''
        if getattr(self, '_last_swarm_summary', None) is not None:
            failed_tests = [
                r for r in self._last_swarm_summary.results
                if not r.passed and app_dir in str(r.test_file)
            ]
            if failed_tests:
                test_context = '\n\n## Recent Test Failures (from automated build)\n'
                for ft in failed_tests[:5]:
                    _out = str(ft.failure_output)
                    if len(_out) > 2000:
                        _out = _out[:2000] + '\n...[truncated]'
                    test_context += f'\n### {ft.test_file}\n```\n{_out}\n```'

        HUB_SYSTEM = f"""You are Canopy Manager — an autonomous debugging agent for the Canopy Hub.
You have a full set of tools: search across files, read files, make surgical edits, restart the app,
probe HTTP endpoints, take screenshots, capture JS console logs, and run tests.

Current app: **{app_name}**{f' (exports/{app_dir})' if app_dir else ''}{process_status}{file_tree_section}{repair_memory_section}{test_context}

## Your autonomous debugging workflow
1. **Start with logs** — read the startup logs above for tracebacks. A traceback is the ground truth.
2. **Search first** — use search_files to find where a variable, ID, route, or import is used across ALL files. This is faster than reading files one by one.
3. **Read the relevant files** — use read_file to see the full context around search hits.
4. **Fix with edit_file** — use edit_file for targeted fixes (ID mismatches, import paths, status codes). It does surgical find-and-replace, so you don't need to rewrite the entire file. Only use write_file if you need to rewrite most of the file.
5. **Probe endpoints** — use probe_endpoint to verify routes return expected status codes.
6. **Verify after fixing** — after edit_file/write_file, call restart_app (for backend changes) then probe_endpoint or run_test.
7. **Screenshot for visual issues** — after frontend changes, call screenshot_app to verify layout.
8. **Console logs for JS errors** — call get_console_logs to catch frontend failures not in uvicorn logs.
9. **Call declare_done** — always finish with declare_done, even if issues remain.

## Tool efficiency tips — EVERY TOOL ROUND IS PRECIOUS
You have a limited number of tool rounds. Maximize each one:
- **read_files (plural)** — when you need to see HTML + JS + backend, call read_files with ALL paths in one call. NEVER call read_file three times in a row.
- **search_files → edit_file** is the fastest fix path. Search for the broken string, then edit_file to replace it. Two rounds total.
- **edit_file** handles multiple occurrences automatically. One call can fix all instances of a wrong ID.
- Don't read an entire file just to find one string — use search_files first.
- Don't rewrite an entire file just to change one line — use edit_file.
- **Act fast**: read what you need in round 1, fix in round 2-3, verify in round 4. Don't spend 8 rounds investigating and 0 fixing.

## Common failure patterns (check first)
- **"Analyzing..." forever / extraction hangs** → Two causes to check in order:
  1. Wrong SDK package: must be `import google.genai as genai; client = genai.Client(api_key=...)`. NOT `import google.generativeai` (deprecated beta v1).
  2. Wrong model string: current stable models are `gemini-2.5-flash` (fast) and `gemini-2.5-pro` (quality). If code uses `gemini-2.0-flash` (deprecated, blocked for new users), `gemini-3.0-flash` (never existed), `gemini-1.5-pro`, or `gemini-1.5-flash` → replace with `gemini-2.5-flash`.
  3. Wrong API: use `client.models.generate_content(model='gemini-2.5-flash', ...)` — NOT `genai.GenerativeModel()`.
- **"Nothing happens" on button click** → fetch URL in JS doesn't match backend route. Read both files and compare.
- **API key never saves** → frontend POSTing to wrong URL. Read main.py routes and compare to JS fetch() calls.
- **File upload fails** → MIME check too strict. Check extension OR content_type, not content_type alone.
- **Startup crash (exit code 1)** → missing dependency. Look for ModuleNotFoundError in startup logs.
- **CORS errors** → add CORSMiddleware to FastAPI app in main.py.
- **Static files 404** → StaticFiles not mounted after all API routes in main.py.
- **Image generation 500 / "model not found"** → Three common causes:
  1. Using `imagen-3.0-generate-002` which is DEPRECATED (removed Nov 2025). Replace with `imagen-4.0-generate-001`.
  2. Using `await client.aio.models.generate_images()` — `generate_images` is SYNC ONLY, no async variant exists.
     Correct: `response = client.models.generate_images(model='imagen-4.0-generate-001', prompt=..., config=types.GenerateImagesConfig(number_of_images=1))`
  3. Adding `http_options` or `api_version` to Client — NOT needed. Plain `genai.Client(api_key=key)` works.
  Do NOT try different api_version values. The fix is ALWAYS: update the model string and use synchronous call.

{_sdk_ref_manager}
## Architecture facts
- Apps are FastAPI + SQLite + vanilla JS. Frontend in `static/` served at `/`.
- API routes use `/api` prefix so they don't conflict with StaticFiles.
- `canopy_keys.load_keys_into_env()` loads API keys into os.environ at startup.
- Backend changes need restart_app. Frontend-only changes (HTML/CSS/JS) take effect on browser refresh.

## CRITICAL: Port & Launch
- The user launches apps from the Canopy Hub UI using a "Launch App" button.
- The Hub ALWAYS launches apps on port **8100** (or the next free port from 8100+).
- Your restart_app tool uses a DIFFERENT random port for debugging — that is normal.
- The user does NOT control what port the app runs on. Do NOT tell them to "use the correct port".
- If the app needs a `if __name__ == "__main__":` block, it MUST use port 8100:
  ```python
  if __name__ == "__main__":
      import uvicorn
      uvicorn.run(app, host="0.0.0.0", port=8100)
  ```
- If the user reports "site can't be reached" or a port mismatch, the fix is to ensure main.py has this block.

Use your tools. Don't ask the user for information you can get by reading files or probing endpoints.
When you're done fixing (or have exhausted options), call declare_done with a summary."""

        # ── Build tool dispatcher bound to this app ───────────────────────────
        if not app_dir or not app_path:
            # No app selected — fall back to a simple conversational response
            try:
                raw = await self.ai_backend.complete(
                    system="You are Canopy Manager. Help the user with their Canopy Hub app.",
                    message=message,
                    history=clean_history,
                    backend='gemini-customtools',
                    max_tokens=4096,
                )
                return web.json_response({'response': raw or "No response.", 'fix_files': []})
            except Exception:
                return web.json_response({'response': "No app selected. Please select an app first.", 'fix_files': []})

        from core.manager_tools import ManagerToolDispatcher, MANAGER_TOOL_DEFINITIONS

        dispatcher = ManagerToolDispatcher(
            app_dir=app_dir,
            app_path=app_path,
            api_server=self,
            exports_dir=_PROJECT_ROOT / 'exports',
        )

        # ── Run the autonomous tool loop ──────────────────────────────────────
        # declare_done is intercepted by _gemini_complete like the Big Fixer's version.
        # The dispatcher's _declare_done is also called so we get fix_files populated.
        _declare_done_result = {}

        async def _tool_dispatcher(tool_name: str, tool_args: dict):
            result = await dispatcher.dispatch(tool_name, tool_args)
            # Capture declare_done args for the response
            if tool_name == "declare_done":
                _declare_done_result.update(result)
            return result

        try:
            raw = await self.ai_backend.complete(
                backend='gemini-customtools',
                system=HUB_SYSTEM,
                message=message,
                history=clean_history,
                max_tokens=32768,
                tools=MANAGER_TOOL_DEFINITIONS,
                tool_dispatcher=_tool_dispatcher,
                tool_mode="AUTO",  # AUTO lets model decide when to use tools vs return text
                max_tool_rounds=20,  # restored: new tools (edit_file, search_files, read_files) make rounds productive
            )
        except Exception as exc:
            logger.warning('Manager tool loop failed (%s) — falling back to single-shot', exc)
            # Graceful degradation: single-shot JSON response (old behaviour)
            raw = await self._manager_fallback(
                message=message,
                history=clean_history,
                app_name=app_name,
                app_dir=app_dir,
                process_status=process_status,
                test_context=test_context,
            )

        # ── Build response from tool session results ──────────────────────────
        # Files written by the dispatcher during the session
        fix_files = [
            {'path': p, 'content': c}
            for p, c in dispatcher._written_files.items()
        ]

        # The model's final text is the conversational reply
        reply = (raw or '').strip() or _declare_done_result.get('summary', 'Done.')

        # ── Record to repair memory ──────────────────────────────────────────
        if _memory_file:
            try:
                _summary = _declare_done_result.get('summary', reply[:200])
                _memory_entries.append({
                    'timestamp': _dt.now().strftime('%H:%M:%S'),
                    'user_message': message[:200],
                    'summary': _summary,
                    'files_edited': list(dispatcher._written_files.keys()),
                })
                # Keep last 20 entries max
                _memory_entries = _memory_entries[-20:]
                _memory_file.write_text(
                    _json.dumps(_memory_entries, indent=2),
                    encoding='utf-8',
                )
            except Exception as exc:
                logger.warning('Failed to write Manager repair memory: %s', exc)

        return web.json_response({'response': reply, 'fix_files': fix_files})

    async def _manager_fallback(
        self,
        message: str,
        history: list,
        app_name: str,
        app_dir: str,
        process_status: str,
        test_context: str,
    ) -> str:
        """Single-shot JSON fallback when the tool loop fails — preserves old behaviour."""
        import re as _re
        import json as _json

        # ── Read source files for context ──────────────────────────────────
        source_context = ''
        if app_dir:
            app_path = _PROJECT_ROOT / 'exports' / app_dir
            file_snippets = []
            _extensions = {'.py', '.html', '.css', '.js', '.json', '.toml'}
            _exclude_dirs = {'.git', '__pycache__', 'node_modules', 'venv', '.pytest_cache'}

            def _get_files(dir_path):
                files = []
                if not dir_path.exists() or not dir_path.is_dir():
                    return files
                for item in dir_path.iterdir():
                    if item.is_dir():
                        if item.name not in _exclude_dirs and not item.name.endswith('.egg-info'):
                            files.extend(_get_files(item))
                    elif item.suffix in _extensions:
                        files.append(item)
                return files

            all_files = _get_files(app_path)

            def _sort_weight(p):
                n = p.name.lower()
                if n in ('main.py', 'api.py', 'app.js', 'index.html'):
                    return 0
                if n.startswith('test_') or n.endswith('_test.py'):
                    return 2
                return 1

            all_files.sort(key=lambda p: (_sort_weight(p), p.name))
            for fp in all_files[:20]:
                try:
                    content = fp.read_text(encoding='utf-8', errors='replace')
                    if len(content) > 10000:
                        content = content[:10000] + f'\n... [truncated]'
                    file_snippets.append(f'### {fp.relative_to(app_path)}\n```\n{content}\n```')
                except Exception:
                    pass
            if file_snippets:
                source_context = '\n\n## App source files\n' + '\n\n'.join(file_snippets)

        fallback_system = f"""You are Canopy Manager. Current app: **{app_name}**{process_status}{test_context}
Diagnose and fix bugs. Return valid JSON: {{"reply": "...", "fix_files": [{{"path": "...", "content": "..."}}]}}{source_context}"""

        raw = None
        for backend_name in ('gemini-customtools', 'gemini-flash-high', 'claude'):
            try:
                raw = await self.ai_backend.complete(
                    system=fallback_system,
                    message=message,
                    history=history,
                    backend=backend_name,
                    json_mode=True,
                    max_tokens=32768,
                )
                if raw:
                    break
            except Exception as exc:
                logger.warning('Manager fallback %s failed: %s', backend_name, exc)

        if not raw:
            return "I'm having trouble reaching the AI right now. Check that your API key is configured."

        # Parse the old JSON shape and return just the reply text
        cleaned = _re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=_re.DOTALL)
        try:
            parsed = _json.loads(cleaned)
            return str(parsed.get('reply', '')).strip() or raw
        except Exception:
            m = _re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, _re.DOTALL)
            if m:
                try:
                    return _json.loads('"' + m.group(1) + '"')
                except Exception:
                    return m.group(1).replace('\\n', '\n').replace('\\"', '"')
        return raw

    async def handle_hub_apply_fix(self, request):
        """POST /api/hub/apply_fix — Write patched files into an app's export directory.

        Body: { app_dir: str, fix_files: [{path, content}] }
        Returns: { written: [paths], count: int }
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON body'}, status=400)

        app_dir   = str(body.get('app_dir', '')).strip()
        fix_files = body.get('fix_files') or []

        if not app_dir:
            return web.json_response({'error': 'app_dir is required'}, status=400)
        if not fix_files:
            return web.json_response({'error': 'fix_files is required'}, status=400)

        # Package files live at exports/<app_dir>/<app_dir>/
        package_root = (_PROJECT_ROOT / 'exports' / app_dir / app_dir).resolve()
        exports_root = (_PROJECT_ROOT / 'exports').resolve()

        written = []
        for f in fix_files:
            rel_path = str(f.get('path', '')).strip()
            content  = f.get('content', '')
            if not rel_path:
                continue

            # Normalise: the AI often prefixes paths with the package dir name
            # (e.g. "deku_chore_tracker/static/app.js") — strip it so we don't
            # double-nest. Accept both "static/app.js" and "<app_dir>/static/app.js".
            stripped = rel_path
            prefix = app_dir + '/'
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]

            dest = (package_root / stripped).resolve()
            # Security: must stay inside exports/
            try:
                dest.relative_to(exports_root)
            except ValueError:
                logger.warning(f'apply_fix: rejected path traversal attempt: {rel_path}')
                return web.json_response({'error': f'Invalid path: {rel_path}'}, status=400)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding='utf-8')
            written.append(stripped)
            logger.info(f'apply_fix: wrote {dest}')

        return web.json_response({'written': written, 'count': len(written)})

    # ── Dev Hub: Task Dispatch ──────────────────────────

    async def handle_dispatch(self, request):
        """Dispatch a task spec to an agent (auto-picks first idle if agent_id omitted)."""
        body = await request.json()
        from core.agent_pool import TaskSpec

        task = TaskSpec(
            title=body.get("title", ""),
            description=body.get("description", ""),
            system_prompt=body.get("system_prompt", ""),
            context_files=body.get("context_files", []),
            target_files=body.get("target_files", []),
            constraints=body.get("constraints", ""),
            context=body.get("context", {}),
        )
        agent_id = body.get("agent_id", "")

        # Auto-pick first idle agent if caller didn't specify one
        if not agent_id:
            agents = self.agent_pool.get_agents()
            idle = [a for a in agents if a.get("status") == "idle"]
            if not idle:
                if not agents:
                    return web.json_response(
                        {"error": "No agents registered. Add an agent and try again."},
                        status=503,
                    )
                # All busy — pick first anyway
                agent_id = agents[0]["id"]
            else:
                agent_id = idle[0]["id"]

        try:
            result = await self.agent_pool.dispatch_task(task, agent_id)
            return web.json_response(self.agent_pool.get_task(result.id))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=404)
        except Exception as e:
            logger.error(f"Dispatch error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_tasks_list(self, request):
        return web.json_response({"tasks": self.agent_pool.get_active_tasks()})

    async def handle_task_detail(self, request):
        task_id = request.match_info["task_id"]
        task = self.agent_pool.get_task(task_id)
        if not task:
            return web.json_response({"error": "Task not found"}, status=404)
        return web.json_response(task)

    async def handle_tester_run(self, request):
        export_dir = request.query.get("export_dir", "").strip()
        if not export_dir:
            return web.json_response({"error": "export_dir query param required"}, status=400)

        from core.tester_swarm import TesterSwarm

        swarm = TesterSwarm(ai_backend=self.ai_backend, sse_broadcast=self._sse_broadcast)
        summary = await swarm.run(export_dir=export_dir)
        self._last_swarm_summary = summary

        return web.json_response({
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration": summary.duration,
            "results": [
                {
                    "source_file": result.source_file,
                    "test_file": result.test_file,
                    "passed": result.passed,
                    "failure_output": result.failure_output,
                    "failure_summary": result.failure_summary,
                    "duration_seconds": result.duration_seconds,
                }
                for result in summary.results
            ],
        })

    async def handle_autofix_run(self, request):
        export_dir = request.query.get("export_dir", "").strip()
        if not export_dir:
            return web.json_response({"error": "export_dir query param required"}, status=400)
        if self._last_swarm_summary is None:
            return web.json_response({"error": "No swarm summary available. Run /api/devhub/tester/run first."}, status=400)

        from core.auto_fix import AutoFixLoop

        session_id = f"session-{int(time.time())}"
        fixer = AutoFixLoop(ai_backend=self.ai_backend, sse_broadcast=self._sse_broadcast)
        summary = await fixer.run(
            swarm_summary=self._last_swarm_summary,
            export_dir=export_dir,
            session_id=session_id,
        )

        return web.json_response({
            "fixed": summary.fixed,
            "failed": summary.failed,
            "restored": summary.restored,
            "attempts": [
                {
                    "test_file": attempt.test_file,
                    "source_file": attempt.source_file,
                    "attempt_number": attempt.attempt_number,
                    "success": attempt.success,
                    "diff": attempt.diff,
                    "final_failure_output": attempt.final_failure_output,
                }
                for attempt in summary.attempts
            ],
            "giant_brain_review": summary.giant_brain_review,
        })

    async def handle_changelog_sessions(self, request):
        from core.master_changelog import MasterChangelog

        sessions = await MasterChangelog().list_sessions()
        return web.json_response({"sessions": sessions})

    async def handle_changelog_export(self, request):
        session_id = request.query.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        from core.master_changelog import MasterChangelog

        output_path = await MasterChangelog().write_export_file(session_id=session_id)
        return web.json_response({"path": output_path, "session_id": session_id})

    async def handle_calibration_run(self, request):
        from core.calibration import CalibrationSystem
        calib = CalibrationSystem(ai_backend=self.ai_backend)
        result = await calib.run_benchmark()
        return web.json_response({
            "any_flagged": result.any_flagged,
            "suggested_adjustments": result.suggested_adjustments,
            "runs": [{"tier": r.tier, "predicted_tokens": r.predicted_tokens, "actual_tokens": r.actual_tokens, "drift_pct": r.drift_pct, "flagged": r.flagged} for r in result.runs],
        })

    async def handle_calibration_apply(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        adjustments = body.get("adjustments", {})
        if not isinstance(adjustments, dict):
            return web.json_response({"error": "adjustments must be a dict"}, status=400)
        from core.calibration import CalibrationSystem
        calib = CalibrationSystem(ai_backend=self.ai_backend)
        await calib.apply_adjustments(adjustments)
        return web.json_response({"ok": True, "applied": adjustments})

    async def handle_calibration_history(self, request):
        try:
            last_n = int(request.query.get("last_n", "5"))
        except ValueError:
            last_n = 5
        from core.calibration import CalibrationSystem
        calib = CalibrationSystem(ai_backend=self.ai_backend)
        runs = await calib.get_history(last_n=last_n)
        return web.json_response({
            "runs": [{"tier": r.tier, "predicted_tokens": r.predicted_tokens, "actual_tokens": r.actual_tokens, "drift_pct": r.drift_pct, "flagged": r.flagged} for r in runs]
        })


    # ── Dev Hub: Model Discovery ──────────────────────────

    async def handle_models_available(self, request):
        """List all models across all backends."""
        models = {"lmstudio": [], "ollama": [], "api": []}

        # LM Studio
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{self.settings.LMSTUDIO_BASE_URL}/models")
                if r.status_code == 200:
                    data = r.json()
                    models["lmstudio"] = [
                        {"id": m.get("id", ""), "object": m.get("object", "")}
                        for m in data.get("data", [])
                    ]
        except Exception:
            pass

        # Ollama
        models["ollama"] = await self.agent_pool.list_ollama_models()

        # API models (static — we know what's available)
        if self.settings.ANTHROPIC_API_KEY:
            models["api"].append({"id": "claude-sonnet-4-6", "provider": "anthropic", "type": "api"})
            models["api"].append({"id": "claude-opus-4-6", "provider": "anthropic", "type": "api"})
        if self.settings.GOOGLE_API_KEY:
            models["api"].append({"id": self.settings.GOOGLE_GEMINI_MODEL, "provider": "google", "type": "api"})

        return web.json_response(models)

    async def handle_ollama_models(self, request):
        models = await self.agent_pool.list_ollama_models()
        return web.json_response({"models": models})

    # ── Dev Hub: File Operations ──────────────────────────

    async def handle_file_read(self, request):
        """Read a file's contents. Subject to JOAT's allowed_read_paths."""
        body = await request.json()
        file_path = body.get("path", "")

        if not file_path:
            return web.json_response({"error": "path required"}, status=400)

        # Use JOAT's existing file safety
        from tools.file_tools import FileTool
        ft = FileTool(self.settings)
        result = ft.read(file_path)
        if result.startswith("Error") or result.startswith("Access denied"):
            return web.json_response({"error": result}, status=403)

        return web.json_response({"path": file_path, "content": result})

    async def handle_file_write(self, request):
        """Write content to a file. Subject to JOAT's allowed_write_paths."""
        body = await request.json()
        file_path = body.get("path", "")
        content = body.get("content", "")

        if not file_path:
            return web.json_response({"error": "path required"}, status=400)

        from tools.file_tools import FileTool
        ft = FileTool(self.settings)
        result = ft.write(file_path, content)
        if "Error" in result or "denied" in result.lower():
            return web.json_response({"error": result}, status=403)

        return web.json_response({"path": file_path, "status": "written", "message": result})

    async def handle_file_tree(self, request):
        """List directory tree for the file browser. Query param: path."""
        import os
        base_path = request.query.get("path", "")

        if not base_path:
            return web.json_response({"error": "path query param required"}, status=400)

        # Validate against allowed read paths
        allowed = False
        for ap in self.settings.ALLOWED_READ_PATHS:
            if os.path.commonpath([os.path.abspath(ap), os.path.abspath(base_path)]) == os.path.abspath(ap):
                allowed = True
                break

        if not allowed:
            return web.json_response({"error": "Path not in allowed_read_paths"}, status=403)

        tree = []
        try:
            for entry in sorted(os.listdir(base_path)):
                full = os.path.join(base_path, entry)
                tree.append({
                    "name": entry,
                    "path": full,
                    "is_dir": os.path.isdir(full),
                    "size": os.path.getsize(full) if os.path.isfile(full) else 0,
                })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        return web.json_response({"path": base_path, "entries": tree})

    # ── Dev Hub: Brain State ──────────────────────────

    async def handle_brain_projects(self, request):
        projects = await self.brain_state.list_projects()
        return web.json_response({"projects": projects})

    async def handle_brain_create_project(self, request):
        body = await request.json()
        project_id = await self.brain_state.create_project(body)
        return web.json_response({"id": project_id, "status": "created"})

    async def handle_brain_project_detail(self, request):
        project_id = request.match_info["project_id"]
        proj = await self.brain_state.get_project(project_id)
        if not proj:
            return web.json_response({"error": "Project not found"}, status=404)
        return web.json_response(proj)

    async def handle_brain_update_project(self, request):
        project_id = request.match_info["project_id"]
        body = await request.json()
        await self.brain_state.update_project(project_id, body)
        return web.json_response({"status": "updated"})

    async def handle_brain_log(self, request):
        project_id = request.match_info["project_id"]
        limit = int(request.query.get("limit", "20"))
        log = await self.brain_state.get_recent_log(project_id, limit)
        return web.json_response({"log": log})

    # ── Canopy Seed Routes ──
    async def handle_canopy_start(self, request):
        """Start a new ContextBuilder session."""
        try:
             body = {}
             try:
                 body = await request.json()
             except Exception:
                 pass
             # Respect the model the UI selected (claude / gemini / local).
             # Default to DEFAULT_AI_BACKEND from settings so .env overrides apply.
             # Fall back to gemini (not claude) since gemini is the battle-tested profile.
             default_model = getattr(self.ai_backend.settings, "DEFAULT_AI_BACKEND", "gemini")
             model = body.get("model", default_model) or default_model
             initial_message = await self.context_builder.start(model=model)
             session_id = initial_message.get("session_id") or self.context_builder.session_id
             self._canopy_sessions[session_id] = self.context_builder.get_context_snapshot()
             self._canopy_session_messages[session_id] = self.context_builder.get_messages()
             return web.json_response(initial_message)
        except Exception as e:
             return web.json_response({"error": str(e)}, status=500)

    async def handle_canopy_message(self, request):
        """Send message to Canopy ContextBuilder."""
        try:
            data = await request.json()
            text = data.get("text", "")
            session_id = str(data.get("session_id", "")).strip()

            # Decode image if the frontend sent a base64 data URL
            image_bytes = None
            raw_image = data.get("image_data")
            if raw_image and isinstance(raw_image, str) and "base64," in raw_image:
                import base64 as _b64
                try:
                    b64_payload = raw_image.split("base64,", 1)[1]
                    image_bytes = _b64.b64decode(b64_payload)
                except Exception as img_err:
                    logger.warning(f"Failed to decode image_data: {img_err}")

            result = await self.context_builder.send_message(text, image_data=image_bytes)

            active_session_id = self.context_builder.session_id
            context_snapshot = self.context_builder.get_context_snapshot()
            messages = self.context_builder.get_messages()
            self._canopy_sessions[active_session_id] = context_snapshot
            self._canopy_session_messages[active_session_id] = messages
            if session_id:
                self._canopy_sessions[session_id] = context_snapshot
                self._canopy_session_messages[session_id] = messages

            return web.json_response(result)
        except Exception as e:
            logger.error(f"handle_canopy_message error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_canopy_context(self, request):
        """Get live context snapshot."""
        try:
            requested_session_id = request.query.get("session_id", "").strip()

            if requested_session_id and requested_session_id in self._canopy_sessions:
                return web.json_response({
                    "context": self._canopy_sessions.get(requested_session_id, {}),
                    "messages": self._canopy_session_messages.get(requested_session_id, []),
                })

            ctx = self.context_builder.get_context_snapshot()
            messages = self.context_builder.get_messages()

            if self.context_builder.session_id:
                self._canopy_sessions[self.context_builder.session_id] = ctx
                self._canopy_session_messages[self.context_builder.session_id] = messages

            return web.json_response({"context": ctx, "messages": messages})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_canopy_session_load(self, request):
        """Load an existing PROJECT_CONTEXT.json and create a new session for DevHub."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        context_path = str(body.get("context_path", "")).strip()
        if not context_path:
            return web.json_response({"error": "context_path required"}, status=400)

        resolved_path = Path(context_path)
        if not resolved_path.is_absolute():
            resolved_path = (_PROJECT_ROOT / context_path).resolve()
        if not resolved_path.exists():
            return web.json_response({"error": f"File not found: {resolved_path}"}, status=400)
        try:
            data = json.loads(resolved_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return web.json_response({"error": f"Invalid JSON: {exc}"}, status=400)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        project_name = data.get("name") or data.get("project_name") or "Unnamed Project"
        context = {
            "name": project_name,
            "project_name": project_name,
            "description": data.get("description", ""),
            "goals": data.get("goals", []),
            "constraints": data.get("constraints", []),
            "target_users": data.get("target_users", ""),
            "tech_preferences": data.get("tech_preferences", []),
            "architecture_notes": data.get("architecture_notes", []),
            "open_questions": data.get("open_questions", []),
            "ready": True,
        }

        session_id = str(uuid.uuid4())
        self._canopy_sessions[session_id] = context
        self._canopy_session_messages[session_id] = []

        base_url = f"{request.scheme}://{request.host}"
        devhub_url = f"{base_url}/devhub?session_id={quote(session_id)}&export_path={quote(str(resolved_path))}"
        return web.json_response({
            "session_id": session_id,
            "project_name": project_name,
            "devhub_url": devhub_url,
        })

    # ── Server control ──
    # ── CS3.5: Export + Snapshot API (2026-02-25) ────────────────────────────────
    # Added by: Claude Sonnet 4.6 (Orchestrator)
    # Wires up the three endpoints CS3 frontend calls that were missing.

    async def handle_canopy_session_export(self, request):
        """
        POST /api/canopy/session/export

        Reads context directly from the live ContextBuilder (no disk lookup needed),
        converts to NotebookLM-friendly Markdown, saves both .md and .json to exports/.
        Returns absolute paths so the frontend can display exactly where files landed.
        """
        try:
            from pathlib import Path
            import datetime
            import json as json_module

            # Pull context directly from the live context builder
            context_data = self.context_builder.get_context_snapshot()
            project_name = context_data.get("name", "") or "Project"

            # Make a filesystem-safe slug from the project name
            slug = "".join(c if c.isalnum() or c in " _-" else "_" for c in project_name)
            slug = slug.strip().replace(" ", "_")[:40] or "Project"

            # Ensure exports directory exists and get absolute path
            exports_dir = Path("exports").resolve()
            exports_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

            # Build Markdown export
            markdown_content = self._build_markdown_export(context_data, self.context_builder.session_id)

            # Save with project-specific names
            markdown_filename = f"{slug}_PROJECT_OVERVIEW_{timestamp}.md"
            json_filename = f"{slug}_PROJECT_CONTEXT_{timestamp}.json"
            markdown_path = exports_dir / markdown_filename
            json_path = exports_dir / json_filename

            markdown_path.write_text(markdown_content, encoding="utf-8")
            json_path.write_text(json_module.dumps(context_data, indent=2), encoding="utf-8")

            logger.info(f"Exported '{project_name}' → {markdown_path}")

            return web.json_response({
                "markdown_path": str(markdown_path),
                "json_path": str(json_path),
                "project_name": project_name,
                "session_id": self.context_builder.session_id,
            })

        except Exception as e:
            logger.error(f"Export error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_canopy_snapshots_list(self, request):
        """
        GET /api/canopy/snapshots?session_id={session_id}
        
        Lists all .zip files in memory/sessions/{session_id}/snapshots/ 
        or memory/snapshots/ fallback. Returns newest first.
        """
        try:
            from pathlib import Path
            
            session_id = request.query.get("session_id", "").strip()
            
            # Try session-specific snapshots first
            snapshots = []
            if session_id:
                snapshot_dir = Path("memory") / "sessions" / session_id / "snapshots"
                if snapshot_dir.exists():
                    for snap_file in sorted(snapshot_dir.glob("*.zip"), 
                                           key=lambda p: p.stat().st_mtime, 
                                           reverse=True):
                        size_kb = snap_file.stat().st_size / 1024
                        created_at = snap_file.stat().st_mtime
                        snapshots.append({
                            "name": snap_file.name,
                            "path": str(snap_file),
                            "created_at": created_at,
                            "size_kb": round(size_kb, 2),
                        })
            
            # Fallback to global snapshots
            if not snapshots:
                snapshot_dir = Path("memory") / "snapshots"
                if snapshot_dir.exists():
                    for snap_file in sorted(snapshot_dir.glob("*.zip"), 
                                           key=lambda p: p.stat().st_mtime, 
                                           reverse=True):
                        size_kb = snap_file.stat().st_size / 1024
                        created_at = snap_file.stat().st_mtime
                        snapshots.append({
                            "name": snap_file.name,
                            "path": str(snap_file),
                            "created_at": created_at,
                            "size_kb": round(size_kb, 2),
                        })
            
            return web.json_response({"snapshots": snapshots})
        
        except Exception as e:
            logger.error(f"Snapshots list error: {e}", exc_info=True)
            return web.json_response({"snapshots": []})

    async def handle_canopy_snapshots_rollback(self, request):
        """
        POST /api/canopy/snapshots/rollback
        
        Restores a snapshot using SnapshotManager.restore_snapshot()
        to the project working directory.
        """
        try:
            body = await request.json()
            snapshot_path = body.get("snapshot_path", "").strip()
            session_id = body.get("session_id", "")
            
            if not snapshot_path:
                return web.json_response(
                    {"success": False, "error": "snapshot_path required"}, 
                    status=400
                )
            
            # Import SnapshotManager
            from core.snapshot import SnapshotManager
            
            snapshot_mgr = SnapshotManager(self.settings)
            
            # Determine restore directory (use cwd or settings)
            restore_dir = getattr(self.settings, 'PROJECT_ROOT', '.')
            
            # Restore the snapshot
            success = await snapshot_mgr.restore_snapshot(snapshot_path, restore_dir)
            
            if success:
                logger.info(f"Snapshot restored from {snapshot_path}")
                return web.json_response({
                    "success": True,
                    "restored_from": snapshot_path,
                })
            else:
                return web.json_response(
                    {"success": False, "error": "Failed to restore snapshot"}, 
                    status=500
                )
        
        except Exception as e:
            logger.error(f"Rollback error: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)}, 
                status=500
            )

    def _build_markdown_export(self, context_data: dict, session_id: str) -> str:
        """
        Build a NotebookLM-friendly Markdown export from ProjectContext.
        
        Format: Clean structured sections for import into research tools.
        """
        # Normalize field access for both dict and dataclass inputs
        def get_field(key, default=""):
            val = context_data.get(key, default) if isinstance(context_data, dict) else getattr(context_data, key, default)
            return val or default
        
        def format_list(items):
            if not items:
                return ""
            if isinstance(items, str):
                return items
            return "\n".join(f"- {item}" for item in items if item)
        
        name = get_field("name", "Unnamed Project")
        description = get_field("description", "")
        goals = get_field("goals", [])
        target_users = get_field("target_users", "")
        tech_prefs = get_field("tech_preferences", [])
        arch_notes = get_field("architecture_notes", [])
        constraints = get_field("constraints", [])
        open_q = get_field("open_questions", [])
        research = get_field("research_log", [])
        
        md = f"""# {name}

## Overview
{description}

## Target Users
{target_users}

## Goals
{format_list(goals)}

## Technical Preferences
{format_list(tech_prefs)}

## Architecture Notes
{format_list(arch_notes)}

## Constraints
{format_list(constraints)}

## Open Questions
{format_list(open_q)}

## Research Used
"""
        
        # Add research entries if available
        if research:
            if isinstance(research, list):
                for entry in research:
                    if isinstance(entry, dict):
                        query = entry.get("query", "")
                        summary = entry.get("summary", "")
                        if query:
                            md += f"\n### {query}\n{summary}\n"
        
        md += f"\n---\n*Generated from Canopy Seed session {session_id}*\n"
        
        return md

    # ── Server control ──
    async def handle_server_restart(self, request):
        return web.json_response({"status": "not_implemented_yet"})

    # ── Build API Handlers ──
    async def handle_build_start(self, request):
        if self._active_build:
            return web.json_response({"success": False, "error": "Build already active"}, status=409)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        proposal = body.get("proposal", "")
        if not proposal:
            return web.json_response({"error": "No proposal provided"}, status=400)

        build_id = f"build-{int(time.time()*1000)}"
        title = body.get("title", f"Build {build_id}")

        self._active_build = {
            "id": build_id,
            "title": title,
            "proposal": proposal,
            "started_at": time.time(),
            "phase": "plan",
            "cancelled": False
        }

        asyncio.create_task(self._run_background_build(build_id, proposal, title))
        return web.json_response({"success": True, "build_id": build_id})

    async def handle_build_stream(self, request):
        response = web.StreamResponse()
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['X-Accel-Buffering'] = 'no'
        await response.prepare(request)

        if not self._active_build:
            await response.write(b'event: build_idle\ndata: {"status": "idle"}\n\n')
            return response

        self._build_sse_clients.append(response)
        try:
            await response.write(f'event: build_started\ndata: {json.dumps({"build_id": self._active_build["id"], "title": self._active_build["title"]})}\n\n'.encode())
            await response.write(f'event: build_phase\ndata: {json.dumps({"phase": self._active_build["phase"]})}\n\n'.encode())
            
            while self._active_build and not self._active_build.get("cancelled", False):
                await asyncio.sleep(15)
                try:
                    await response.write(b': keepalive\n\n')
                except Exception:
                    break
        finally:
            if response in self._build_sse_clients:
                self._build_sse_clients.remove(response)
            
        return response

    async def _build_sse_broadcast(self, event_type: str, data: dict):
        import json as _json
        payload = (f"event: {event_type}\ndata: {_json.dumps(data)}\n\n").encode()
        dead = []
        for client in self._build_sse_clients:
            try:
                await client.write(payload)
            except Exception:
                dead.append(client)
        for d in dead:
            if d in self._build_sse_clients:
                self._build_sse_clients.remove(d)

    async def handle_inject(self, request):
        if not self._active_build:
            return web.json_response({"success": False, "error": "No active build"})
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        
        msg = body.get("message", "")
        if not msg:
            return web.json_response({"success": False, "error": "Message required"})

        self._inject_queue.append(msg)
        await self._build_sse_broadcast("build_log", {"line": f"[INJECTED] Guidance received: {msg[:80]}..."})
        return web.json_response({"success": True})

    async def handle_build_cancel(self, request):
        if not self._active_build:
            return web.json_response({"success": False, "error": "No active build"})
        self._active_build["cancelled"] = True
        return web.json_response({"success": True})

    async def _run_background_build(self, build_id: str, proposal: str, title: str):
        class _BuildUpdateStub:
            class _Msg:
                class _Chat:
                    async def send_action(self, *args, **kwargs): pass
                chat = _Chat()
                async def reply_text(self, text, parse_mode=None):
                    pass
            message = _Msg()
            active_build = self._active_build
            inject_queue = self._inject_queue
            build_sse = self._build_sse_broadcast

        update = _BuildUpdateStub()

        # Wire reply_text to send build_log and catch tool execution messages
        async def mock_reply(text, parse_mode=None):
            await self._build_sse_broadcast("build_log", {"line": text})
        
        update.message.reply_text = mock_reply

        try:
            # We don't have distinct phases in router.py inherently without specific phase wrapping.
            # Using 'generate' for the main loop phase.
            self._active_build["phase"] = "generate"
            await self._build_sse_broadcast("build_phase", {"phase": "generate"})
            
            start_msg = f"BUILD PROPOSAL:\n{proposal}"
            
            t0 = time.time()
            result_text = await self.router._handle_ai(start_msg, update, backend="claude")
            duration_s = int(time.time() - t0)

            # Check if it was cancelled during the loop
            if self._active_build and self._active_build.get("cancelled", False):
                await self._build_sse_broadcast("build_cancelled", {"build_id": build_id})
            else:
                if self._active_build:
                    self._active_build["phase"] = "done"
                await self._build_sse_broadcast("build_phase", {"phase": "done"})
                await self._build_sse_broadcast("build_complete", {
                    "build_id": build_id, 
                    "duration_s": duration_s, 
                    "tests_passed": 0, 
                    "tests_failed": 0
                })
        except Exception as e:
            logger.error(f"Background build failed: {e}", exc_info=True)
            phase = self._active_build["phase"] if self._active_build else "unknown"
            await self._build_sse_broadcast("build_failed", {"build_id": build_id, "error": str(e), "phase": phase})
        finally:
            self._active_build = None

