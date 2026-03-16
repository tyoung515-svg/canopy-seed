"""
start.py — Canopy Seed standalone launcher

Boots the API server (port 7822) without the Telegram bot.
Opens the DevHub in your default browser automatically.

Run with: python start.py

OWNED BY: Orchestrator (Claude Sonnet 4.6)
"""

import asyncio
import logging
import os
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

# Pin working directory to the project root so relative paths always resolve correctly
_PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(_PROJECT_ROOT)

load_dotenv(override=True)  # override=True ensures .env values win over inherited env vars

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG_MODE", "false").lower() == "true" else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/canopy_start.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("canopy.start")


def is_vault_mode_enabled() -> bool:
    return os.getenv("VAULT_MODE", "true").lower() == "true"


def run_preflight_checks() -> None:
    if not is_vault_mode_enabled():
        check_env()
    else:
        logger.warning("VAULT_MODE enabled - API keys expected via /api/vault/push from Forge")


def check_env():
    """Warn about missing keys before starting."""
    anthropic = os.getenv("ANTHROPIC_API_KEY", "")
    gemini = os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")

    if not anthropic and not gemini:
        logger.error(
            "No AI key found. Launch Canopy Seed with VAULT_MODE=true (default) and enter your key in the vault setup screen."
        )
        sys.exit(1)

    if not anthropic:
        logger.warning("ANTHROPIC_API_KEY not set — Claude models unavailable.")
    if not gemini:
        logger.warning("GOOGLE_API_KEY not set — Gemini models unavailable.")


async def main():
    # ── Pre-flight ────────────────────────────────────────────────────────────
    Path("logs").mkdir(exist_ok=True)
    Path("memory/sessions").mkdir(parents=True, exist_ok=True)
    Path("exports").mkdir(exist_ok=True)

    run_preflight_checks()

    # ── Import after env is confirmed ─────────────────────────────────────────
    from config.settings import Settings
    from core.api_server import DashboardAPI
    from core.ai_backend import AIBackend
    from core.context import AgentContext
    from core.router import MessageRouter
    from memory.canopy import MemoryStore

    settings = Settings()
    port = int(os.getenv("DASHBOARD_API_PORT", "7822"))

    # ── Wire dependencies ─────────────────────────────────────────────────────
    memory_store = MemoryStore(str(settings.DB_PATH))
    ai_backend = AIBackend(settings)
    context = AgentContext(settings, memory_store=memory_store)
    router = MessageRouter(settings, context, memory_store=memory_store)

    # ── Start API server ──────────────────────────────────────────────────────
    server = DashboardAPI(settings, context, router, ai_backend)
    await server.start()

    base_url = f"http://localhost:{port}"
    devhub_url = f"{base_url}/devhub"
    hub_url = f"{base_url}/hub"

    logger.info("=" * 60)
    logger.info("  Canopy Seed is running")
    logger.info(f"  UI:     {base_url}")
    logger.info(f"  DevHub: {devhub_url}")
    logger.info(f"  Hub:    {hub_url}")
    logger.info("  Stop: Ctrl+C")
    logger.info("=" * 60)

    # Open browser after short delay:
    # → DevHub if vault needs setup, main UI if vault already configured
    async def open_browser():
        await asyncio.sleep(1.2)
        from memory.vault_store import is_vault_setup
        if is_vault_setup():
            webbrowser.open(hub_url)
        else:
            webbrowser.open(devhub_url)

    asyncio.create_task(open_browser())

    # ── Keep running ─────────────────────────────────────────────────────────
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down…")
        await server.stop()
        logger.info("Canopy Seed stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
