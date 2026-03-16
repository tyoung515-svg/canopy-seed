"""Skill to check system status, reusing functionality from system_tools."""

import sys
import logging
from datetime import datetime, timezone

from skills.base import Skill
from skills.uptime import START_TIME, format_uptime
from tools.system_tools import get_system_status

logger = logging.getLogger(__name__)

class StatusSkill(Skill):
    name = "status"
    description = "Detailed system and bot status."
    triggers = ["status"]
    enabled = True

    async def execute(self, args: str, update, context) -> str:
        # Get base system status from tools
        base_status = await get_system_status()
        
        # Bot Uptime calculation
        now = datetime.now(timezone.utc)
        uptime_seconds = int((now - START_TIME).total_seconds())
        uptime_str = format_uptime(uptime_seconds)
        
        # Number of skills workaround
        skill_count_str = ""
        try:
            # Check if skill_registry was attached to context or if we can infer it
            if hasattr(context, "skill_registry") and context.skill_registry:
                count = len(context.skill_registry.list_skills())
                skill_count_str = f"  Skills Loaded: {count}\n"
        except Exception:
            pass

        python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        
        # Build Bot Context
        bot_info = (
            f"\n*CanopySeed Runtime:*\n"
            f"  Uptime: {uptime_str}\n"
            f"  Python: {python_version}\n"
            f"{skill_count_str}"
        )

        # AI Backend Health Summary
        backend_status = ""
        if hasattr(context, "router") and hasattr(context.router, "ai"):
             backend_status = f"\n{await context.router.ai.get_backend_status()}"
        else:
            try:
                from core.ai_backend import AIBackend
                temp_ai = AIBackend(context.settings)
                backend_status = f"\n{await temp_ai.get_backend_status()}"
            except Exception as e:
                logger.error(f"Failed to get AI backend status: {e}")
                
        return f"{base_status}{bot_info}{backend_status}"
