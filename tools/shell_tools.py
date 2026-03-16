"""
Shell Tools - Execute commands with safety controls
"""

import asyncio
import logging
import shlex
from typing import Tuple

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30

# Category -> command pattern mapping
# Each category maps to "allow" and/or "confirm" patterns.
# If a category is enabled (True in config), its "allow" patterns auto-pass
# and its "confirm" patterns require user confirmation.
# If disabled (False), those commands fall through to the default (blocked).
SHELL_CATEGORIES = {
    "diagnostics": {
        "allow": ["systeminfo", "hostname", "whoami", "ver", "wmic os"],
    },
    "python_read": {
        "allow": ["python ", "python3 ", "pip list", "pip show", "pip freeze", "pip --version"],
    },
    "python_write": {
        "confirm": ["pip install", "pip uninstall"],
    },
    "git_read": {
        "allow": ["git status", "git log", "git diff", "git branch", "git remote", "git show", "git tag"],
    },
    "git_write": {
        "confirm": ["git push", "git commit", "git checkout", "git reset", "git rebase", "git merge", "git stash"],
    },
    "network_diag": {
        "allow": ["ping ", "tracert ", "nslookup ", "arp ", "netstat", "ipconfig", "curl http://localhost", "curl http://127.0.0.1"],
    },
    "service_read": {
        "allow": ["powershell -Command Get-Service", "sc query", "sc qc"],
    },
    "service_write": {
        "confirm": ["net stop", "net start", "sc stop", "sc start", "Restart-Service", "Stop-Service", "Start-Service"],
    },
    "file_read": {
        "allow": ["dir ", "type ", "where ", "tree ", "attrib ", "more "],
    },
    "file_write": {
        "confirm": ["copy ", "move ", "del ", "mkdir ", "rmdir ", "rename ", "ren "],
    },
    "process_read": {
        "allow": ["tasklist", "wmic process", "powershell -Command Get-Process"],
    },
    "process_write": {
        "confirm": ["taskkill", "Stop-Process", "kill "],
    },
    "power_mgmt": {
        "confirm": ["powercfg", "shutdown", "restart", "Restart-Computer", "Stop-Computer"],
    },
    "ollama": {
        "allow": ["ollama list", "ollama ps", "ollama show", "ollama run"],
    },
    "registry": {
        "allow": ["reg query"],
        "confirm": ["reg add", "reg delete"],
    },
}


class ShellTool:
    def __init__(self, settings):
        self.settings = settings

    async def execute(self, cmd: str, timeout: int = TIMEOUT_SECONDS) -> str:
        """Execute shell command and return output"""
        logger.info(f"Executing: {cmd}")
        
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024  # 1MB output limit
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), 
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                return f"[Timeout after {timeout}s]"

            output = stdout.decode('utf-8', errors='replace').strip()
            errors = stderr.decode('utf-8', errors='replace').strip()
            
            result_parts = []
            if output:
                result_parts.append(output)
            if errors:
                result_parts.append(f"[stderr]\n{errors}")
            if proc.returncode != 0:
                result_parts.append(f"[exit code: {proc.returncode}]")
                
            return "\n".join(result_parts) if result_parts else "[No output]"

        except Exception as e:
            logger.error(f"Shell execution error: {e}")
            return f"[Error: {e}]"

    def is_safe(self, cmd: str) -> Tuple[bool, str]:
        """
        Check if command is safe to run.

        Priority order:
        1. Flat shell_allowlist (explicit allow wins)
        2. Flat shell_confirm_list (explicit confirm wins)
        3. Enabled categories - "allow" patterns -> safe
        4. Enabled categories - "confirm" patterns -> needs_confirmation
        5. Default: not_allowed
        """
        cmd_stripped = cmd.strip()

        # 1. Flat allowlist (highest priority - explicit allow)
        for allowed in self.settings.SHELL_ALLOWLIST:
            if cmd_stripped.startswith(allowed):
                return True, "allowed"

        # 2. Flat confirm list (explicit confirm overrides categories)
        for confirm in self.settings.SHELL_CONFIRM_LIST:
            if cmd_stripped.startswith(confirm):
                return False, "needs_confirmation"

        # 3-4. Category-based checks
        categories = getattr(self.settings, 'SHELL_CATEGORIES', {})

        for cat_name, patterns in SHELL_CATEGORIES.items():
            if not categories.get(cat_name, False):
                continue  # Category disabled, skip

            # Check "allow" patterns
            for pattern in patterns.get("allow", []):
                if cmd_stripped.startswith(pattern):
                    return True, "allowed"

            # Check "confirm" patterns
            for pattern in patterns.get("confirm", []):
                if cmd_stripped.startswith(pattern):
                    return False, "needs_confirmation"

        # 5. Default: not on allowlist/confirm/blocked — offer as elevated (requires approval)
        return False, "elevated_pending"
