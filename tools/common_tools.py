"""
common_tools.py — Stub for Canopy Seed standalone mode

run_tool() is used by router.py for Telegram !tool commands.
Not needed when running via start.py (web UI only).

OWNED BY: Orchestrator (Claude Sonnet 4.6)
"""

import logging

logger = logging.getLogger(__name__)


async def run_tool(tool_name: str, tool_args: str = "") -> str:
    """Stub — !tool commands are Telegram-only, not available in web UI mode."""
    logger.debug(f"run_tool called in web mode (no-op): {tool_name} {tool_args}")
    return f"Tool '{tool_name}' is only available via the Telegram bot."
