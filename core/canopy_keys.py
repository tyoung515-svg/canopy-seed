"""
Canopy Key Client (ADR-041)
──────────────────────────
Single-file API key provider for exported Canopy apps.

Resolution order (highest priority first):
  1. Canopy vault API  — GET http://localhost:7822/api/vault/keys
                         Served by the running Canopy dashboard (vault must be unlocked).
  2. os.environ       — Standard environment variables; set by Canopy when vault is
                         unlocked, or manually in headless/CI environments.
  3. Return ""        — Caller can decide to raise or use a fallback.

Why a single file?
  Exported apps should never maintain their own key stores.  Copy this module into
  the app package root and call get_key() / load_keys_into_env() at startup.

Usage
─────
  # In any exported app — e.g. battery_datasheet_scanner/main.py:
  from canopy_keys import load_keys_into_env, get_key
  load_keys_into_env()   # call once at startup; silently no-ops if Canopy not running

  api_key = get_key("GOOGLE_API_KEY")   # returns "" if not found

  # Or directly in a one-liner check:
  import canopy_keys
  if not canopy_keys.get_key("GOOGLE_API_KEY"):
      raise EnvironmentError("GOOGLE_API_KEY not set — unlock the Canopy vault first")
"""

from __future__ import annotations

import logging
import os
import urllib.request
import json
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Port where the Canopy dashboard API server runs.
# Override via CANOPY_API_PORT env var if you changed the default.
_CANOPY_PORT: int = int(os.getenv("CANOPY_API_PORT", "7822"))
_CANOPY_URL: str = f"http://localhost:{_CANOPY_PORT}/api/vault/keys"

# Module-level cache so we only hit the Canopy API once per process.
_cached_keys: Optional[Dict[str, str]] = None


def _fetch_from_canopy(timeout: float = 1.0) -> Dict[str, str]:
    """Try to fetch live keys from the Canopy vault API.

    Returns the key dict on success, or {} on any error (Canopy not running,
    vault locked, etc.).  Never raises.
    """
    try:
        req = urllib.request.Request(_CANOPY_URL)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            keys: Dict[str, str] = data.get("keys") or {}
            if data.get("unlocked") and keys:
                logger.debug(
                    "canopy_keys: loaded %d keys from Canopy vault (%s)",
                    len(keys),
                    list(keys.keys()),
                )
                return keys
            if not data.get("unlocked"):
                logger.debug("canopy_keys: Canopy vault is locked — using env fallback")
    except Exception as exc:
        logger.debug("canopy_keys: Canopy API not reachable (%s) — using env fallback", exc)
    return {}


def load_keys_into_env(force: bool = False) -> Dict[str, str]:
    """Fetch keys from the Canopy vault and push them into os.environ.

    Call once at app startup.  Subsequent calls are no-ops (cached) unless
    force=True.

    Returns the dict of keys that were pushed (may be empty if Canopy is not
    running or the vault is locked; os.environ values already in place are
    unaffected).
    """
    global _cached_keys

    if _cached_keys is not None and not force:
        return dict(_cached_keys)

    keys = _fetch_from_canopy()

    if keys:
        for env_var, val in keys.items():
            if isinstance(val, str) and val.strip():
                os.environ.setdefault(env_var, val.strip())

    _cached_keys = keys
    return dict(keys)


def get_key(env_var: str, fallback: str = "") -> str:
    """Return the value of *env_var*, trying the Canopy vault first.

    Calls load_keys_into_env() on first use (cached thereafter).
    Falls back to *fallback* (default "") if the key is not found anywhere.

    Example::
        api_key = get_key("GOOGLE_API_KEY")
    """
    # Ensure the Canopy vault has been consulted at least once
    load_keys_into_env()
    return os.environ.get(env_var, fallback)


def require_key(env_var: str) -> str:
    """Like get_key() but raises EnvironmentError if the key is empty.

    Use this for keys that are genuinely required for the app to function::
        api_key = require_key("GOOGLE_API_KEY")
    """
    val = get_key(env_var)
    if not val:
        raise EnvironmentError(
            f"{env_var} is not set.  "
            f"Either unlock the Canopy vault (http://localhost:{_CANOPY_PORT}) "
            f"or set the environment variable directly."
        )
    return val
