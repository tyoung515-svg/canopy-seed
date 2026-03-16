"""
Workflow profiles — ADR-040 (Session 54).

A profile is a complete mapping of every agent role to a comma-separated
backend chain string.  Selecting a profile sets the default chain for every
role in one shot, replacing the scattered _ROLE_DEFAULTS in ai_backend.py.

Priority in get_role_chain():
  1. Per-role env var override (ROLE_CONTRACT_CHAIN, ROLE_ORCHESTRATOR_CHAIN, …)
  2. Active workflow profile  (WORKFLOW_PROFILE env var or runtime switch)
  3. Hardcoded _ROLE_DEFAULTS in ai_backend.py  (legacy fallback)

Profiles
--------
gemini  — Battle-tested end-to-end Gemini pipeline (default).
          Three-tier: Flash Lite → Flash → Pro 3.1
          All roles validated across Sessions 48-53.

claude  — Claude end-to-end pipeline (UNTESTED — defined for future validation).
          Haiku for mechanical, Sonnet for standard, Opus/Sonnet for escalation.

qwen    — Qwen end-to-end pipeline (UNTESTED — defined for future validation).
          qwen-flash/coder-flash for mechanical, qwen-122b standard, qwen-plus escalation.
"""

from __future__ import annotations
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------

_GEMINI_PROFILE: Dict[str, str] = {
    # Session 58: thinking-level aware profile
    # interview: plain Pro (adaptive thinking) — no schema enforcement needed
    "interview":          "claude,gemini-pro-plain,gemini-flash-high",

    # High thinking / deep reasoning → customtools or flash-high
    "planner":            "gemini-customtools,claude,gemini-flash-high",
    "auditor":            "gemini-customtools,gemini-flash-high,claude",
    "dev_swarm_t3":       "claude,gemini-customtools,gemini-flash-high",
    "test_swarm_t2":      "gemini-customtools,claude,gemini-flash-high",
    "big_fixer":          "gemini-customtools,gemini-flash-high,claude",

    # customtools primary — Flash 3.0 HIGH+schema unreliable (HTTP 400 / non-JSON, S58 run log)
    "orchestrator":       "gemini-customtools,gemini-flash-high,claude-haiku",
    "chunker":            "gemini-customtools,gemini-flash-high,claude-haiku",
    "contract_generator": "gemini-customtools,gemini-flash-high,claude",
    "giant_brain":        "gemini-customtools,gemini-flash-high,claude",

    # Flash MINIMAL — swarm tasks (fast, mechanical, low-stakes)
    "dev_swarm_t1":       "gemini-flash-minimal,gemini-flash,claude-haiku",
    "dev_swarm_t2":       "gemini-flash-minimal,gemini-customtools,claude-haiku",
    "test_swarm_t1":      "gemini-flash-minimal,gemini-customtools,claude-haiku",

    # Mechanical / relay → Flash Lite 3.1
    "mcp":                "gemini-flash-lite,gemini-flash",
    "memory":             "gemini-flash-lite,gemini-flash",
}

_CLAUDE_PROFILE: Dict[str, str] = {
    # NOTE: UNTESTED — validate with a full smoke test before using in production.
    "interview":          "claude,claude-haiku",
    "planner":            "claude,claude-haiku",
    "auditor":            "claude,claude-haiku",
    "dev_swarm_t3":       "claude,claude-haiku",
    "test_swarm_t2":      "claude,claude-haiku",
    "big_fixer":          "claude,claude-haiku",

    "orchestrator":       "claude-haiku,claude",
    "chunker":            "claude-haiku,claude",
    "contract_generator": "claude,claude-haiku",
    "giant_brain":        "claude,claude-haiku",
    "dev_swarm_t2":       "claude,claude-haiku",
    "test_swarm_t1":      "claude,claude-haiku",

    "dev_swarm_t1":       "claude-haiku,claude",
    "mcp":                "claude-haiku,claude",
    "memory":             "claude-haiku,claude",
}

_QWEN_PROFILE: Dict[str, str] = {
    # NOTE: UNTESTED — validate with a full smoke test before using in production.
    "interview":          "qwen-122b,qwen-plus",
    "planner":            "qwen-plus,qwen-122b",
    "auditor":            "qwen-plus,qwen-122b",
    "dev_swarm_t3":       "qwen-plus,qwen-122b",
    "test_swarm_t2":      "qwen-plus,qwen-122b",
    "big_fixer":          "qwen-plus,qwen-122b",

    "orchestrator":       "qwen-122b,qwen-plus",
    "chunker":            "qwen-122b,qwen-plus",
    "contract_generator": "qwen-122b,qwen-plus",
    "giant_brain":        "qwen-122b,qwen-plus",
    "dev_swarm_t2":       "qwen-122b,qwen-plus",
    "test_swarm_t1":      "qwen-122b,qwen-plus",

    "dev_swarm_t1":       "qwen-coder-flash,qwen-122b",
    "mcp":                "qwen-flash,qwen-122b",
    "memory":             "qwen-flash,qwen-122b",
}

# Registry — add new profiles here
PROFILES: Dict[str, Dict[str, str]] = {
    "gemini": _GEMINI_PROFILE,
    "claude": _CLAUDE_PROFILE,
    "qwen":   _QWEN_PROFILE,
}

VALID_PROFILES = list(PROFILES.keys())
DEFAULT_PROFILE = "gemini"

# Keys that require configuration for each profile
PROFILE_REQUIRED_KEYS: Dict[str, list] = {
    "gemini": ["GOOGLE_API_KEY"],
    "claude": ["ANTHROPIC_API_KEY"],
    "qwen":   ["QWEN_API_KEY"],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_profile_chain(profile_name: str, role_key: str) -> Optional[str]:
    """Return the chain string for *role_key* in *profile_name*, or None.

    role_key must already be normalised (lowercase, hyphens → underscores).
    Returns None if the profile or role is not found, so the caller can fall
    back to hardcoded defaults.
    """
    profile = PROFILES.get((profile_name or DEFAULT_PROFILE).lower())
    if profile is None:
        return None
    return profile.get(role_key)


def profile_info(profile_name: str) -> dict:
    """Return a summary dict for display in the UI."""
    name = (profile_name or DEFAULT_PROFILE).lower()
    profile = PROFILES.get(name, {})
    tested = name == "gemini"
    return {
        "name": name,
        "tested": tested,
        "required_keys": PROFILE_REQUIRED_KEYS.get(name, []),
        "chains": dict(profile),
    }
