"""
System Tools - Status and monitoring (Windows)
"""

import asyncio
import logging
import platform
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


async def get_system_status() -> str:
    """Get system status summary"""
    lines = [f"*CanopySeed Status* — {datetime.now().strftime('%H:%M:%S')}"]

    # CPU & Memory
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        lines.append(f"\n*System:*")
        lines.append(f"  CPU: {cpu:.1f}%")
        lines.append(f"  RAM: {mem.used/1e9:.1f}/{mem.total/1e9:.1f} GB ({mem.percent:.0f}%)")

        # Check all drives
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                lines.append(
                    f"  {part.device} {usage.used/1e9:.0f}/{usage.total/1e9:.0f} GB "
                    f"({usage.percent:.0f}%)"
                )
            except (PermissionError, OSError):
                continue
    except ImportError:
        lines.append("  _(psutil not installed)_")

    # GPU (NVIDIA)
    try:
        proc = await asyncio.create_subprocess_shell(
            "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu "
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        gpu_info = stdout.decode().strip()
        if gpu_info:
            lines.append(f"\n*GPU:*")
            for i, gpu_line in enumerate(gpu_info.split('\n')):
                parts = [p.strip() for p in gpu_line.split(',')]
                if len(parts) >= 5:
                    name, util, mem_used, mem_total, temp = parts[:5]
                    lines.append(f"  GPU{i}: {name}")
                    lines.append(f"    Util: {util}% | VRAM: {mem_used}/{mem_total} MiB | Temp: {temp}°C")
    except (asyncio.TimeoutError, Exception) as e:
        lines.append(f"  GPU: _(not available)_")

    # LM Studio
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get("http://localhost:1234/v1/models")
            if r.status_code == 200:
                models = [m['id'] for m in r.json().get('data', [])]
                lines.append(f"\n*LM Studio:* ✅ ({len(models)} model(s) loaded)")
                for m in models[:5]:
                    lines.append(f"  • {m}")
            else:
                lines.append(f"\n*LM Studio:* ❌")
    except Exception:
        lines.append(f"\n*LM Studio:* ❌ not running")

    # Key processes
    try:
        import psutil
        check_procs = ["python", "lm studio", "LM Studio"]
        proc_lines = []
        for svc in check_procs:
            is_running = any(
                svc.lower() in p.name().lower()
                for p in psutil.process_iter(['name'])
            )
            icon = "✅" if is_running else "❌"
            proc_lines.append(f"  {svc}: {icon}")
        if proc_lines:
            lines.append(f"\n*Processes:*")
            lines.extend(proc_lines)
    except Exception:
        pass

    return "\n".join(lines)
