"""
Proactive Monitor
Watches for conditions and pushes alerts to your Telegram
Enable via ENABLE_PROACTIVE=true in .env
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class ProactiveMonitor:
    """
    Runs background tasks that can push messages to you without prompting.
    
    Add your own monitors as async methods following the pattern below.
    Each monitor runs on its own schedule.
    """

    def __init__(self, settings, bot):
        self.settings = settings
        self.bot = bot
        self.chat_id = settings.PROACTIVE_CHAT_ID
        self._running = False

    async def push(self, message: str):
        """Send a proactive message to your Telegram"""
        if not self.chat_id:
            logger.warning("PROACTIVE_CHAT_ID not set, can't push message")
            return
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to push message: {e}")

    async def start(self):
        """Start all monitors as background tasks"""
        if not self.settings.ENABLE_PROACTIVE:
            return
        
        self._running = True
        logger.info("Starting proactive monitors...")
        
        await asyncio.gather(
            self.monitor_gpu_temp(),
            self.monitor_disk_space(),
            self.morning_briefing(),
            # Add more monitors here
        )

    async def monitor_gpu_temp(self, threshold: int = 85, interval: int = 60):
        """Alert if GPU temp exceeds threshold"""
        while self._running:
            try:
                proc = await asyncio.create_subprocess_shell(
                    "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                
                for i, line in enumerate(stdout.decode().strip().split('\n')):
                    try:
                        temp = int(line.strip())
                        if temp > threshold:
                            await self.push(
                                f"🌡️ *GPU{i} temperature alert!*\n"
                                f"Current: {temp}°C (threshold: {threshold}°C)"
                            )
                    except ValueError:
                        pass
            except Exception:
                pass
            
            await asyncio.sleep(interval)

    async def monitor_disk_space(self, threshold_pct: int = 90, interval: int = 3600):
        """Alert if disk usage exceeds threshold"""
        while self._running:
            try:
                import psutil
                disk = psutil.disk_usage('/')
                if disk.percent > threshold_pct:
                    free_gb = (disk.total - disk.used) / 1e9
                    await self.push(
                        f"💾 *Disk space warning!*\n"
                        f"Usage: {disk.percent:.0f}% (only {free_gb:.1f} GB free)"
                    )
            except Exception:
                pass
            
            await asyncio.sleep(interval)

    async def morning_briefing(self):
        """Send a morning briefing at 8am"""
        try:
            # We don't have this implemented yet or might skip it if library missing
            from datetime import time as dtime
        except ImportError:
            return
            
        target_time = dtime(8, 0)  # 8:00 AM
        
        while self._running:
            now = datetime.now()
            
            # Check if it's within the minute of 8am
            if now.hour == target_time.hour and now.minute == target_time.minute:
                briefing = await self._build_briefing()
                await self.push(briefing)
                await asyncio.sleep(61)  # Avoid double-trigger within the minute
            
            await asyncio.sleep(30)  # Check every 30 seconds

    async def _build_briefing(self) -> str:
        """Build morning briefing message"""
        try:
            from tools.system_tools import get_system_status
            status = await get_system_status()
        except ImportError:
            status = "System status unavailable."
            
        date_str = datetime.now().strftime("%A, %B %d")
        
        return (
            f"☀️ *Good morning!* — {date_str}\n\n"
            f"{status}\n\n"
            f"_Canopy Seed ready._"
        )

    def stop(self):
        self._running = False
