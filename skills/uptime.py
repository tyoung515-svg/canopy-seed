"""Simple skill that reports how long the bot has been running."""

from datetime import datetime, timezone

from skills.base import Skill

# Stored on first import
START_TIME = datetime.now(timezone.utc)

def format_uptime(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    
    return " ".join(parts)

class UptimeSkill(Skill):
    name = "uptime"
    description = "Reports how long the bot has been running."
    triggers = ["uptime"]
    enabled = True

    async def execute(self, args: str, update, context) -> str:
        now = datetime.now(timezone.utc)
        uptime_seconds = int((now - START_TIME).total_seconds())
        human_readable = format_uptime(uptime_seconds)
        return f"⏱️ *Bot Uptime:* {human_readable}"
