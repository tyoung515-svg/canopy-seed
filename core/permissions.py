"""
Permission Tiers — controls which actions require explicit Travis approval.
"""

from __future__ import annotations

from enum import Enum


class PermissionTier(Enum):
    STANDARD = "standard"    # Always available, no approval needed
    ELEVATED = "elevated"    # Requires explicit "yes"


# Actions and their tier assignments
ACTION_TIERS: dict[str, PermissionTier] = {
    # Standard — existing behavior unchanged
    "shell_allowlist": PermissionTier.STANDARD,
    "file_read": PermissionTier.STANDARD,
    "fetch_safe_domain": PermissionTier.STANDARD,

    # Elevated — expanded capabilities that always gate on approval
    "file_write_any_path": PermissionTier.ELEVATED,
    "shell_any_command": PermissionTier.ELEVATED,     # commands not on allowlist
    "fetch_any_domain": PermissionTier.ELEVATED,      # domains not on safe list
    "process_kill": PermissionTier.ELEVATED,
    "service_control": PermissionTier.ELEVATED,
    "python_exec": PermissionTier.ELEVATED,
}

# Human-readable descriptions for !unlock display
ACTION_DESCRIPTIONS: dict[str, str] = {
    "file_write_any_path": "Write to any file path (bypasses ALLOWED_WRITE_PATHS gate)",
    "shell_any_command": "Run any shell command not on the standard allowlist",
    "fetch_any_domain": "Fetch from any URL/domain not on the safe-domains list",
    "process_kill": "Kill any running process by name or PID",
    "service_control": "Start, stop, or restart Windows services",
    "python_exec": "Execute arbitrary Python code via the pyshell skill",
}


def requires_approval(action_key: str) -> bool:
    """Return True when the action is ELEVATED and must be confirmed before execution."""
    return ACTION_TIERS.get(action_key, PermissionTier.ELEVATED) == PermissionTier.ELEVATED


def unlock_status_message() -> str:
    """Return a formatted status block for the !unlock command."""
    lines = ["🔓 *Elevated Access Tiers*\n"]
    lines.append("All elevated actions require your explicit `yes` before executing.\n")

    for action, tier in ACTION_TIERS.items():
        if tier == PermissionTier.ELEVATED:
            desc = ACTION_DESCRIPTIONS.get(action, "")
            lines.append(f"• `{action}` — {desc}")

    lines.append("\nStandard actions (no confirmation needed):")
    for action, tier in ACTION_TIERS.items():
        if tier == PermissionTier.STANDARD:
            lines.append(f"• `{action}`")

    return "\n".join(lines)
