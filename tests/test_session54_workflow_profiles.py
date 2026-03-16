"""
Session 54 tests — Workflow profile system (ADR-040).

Covers:
  config/profiles.py
    - get_profile_chain returns correct chain for known profile + role
    - get_profile_chain returns None for unknown profile
    - get_profile_chain returns None for unknown role key
    - DEFAULT_PROFILE is "gemini"
    - All three profiles contain identical role key sets

  core/ai_backend.py — get_role_chain priority tiers
    - Priority 1: per-role ROLE_*_CHAIN env var override wins over profile
    - Priority 2: active profile chain used when no env override
    - Priority 3: hardcoded _ROLE_DEFAULTS used when profile lookup misses
    - Switching settings.WORKFLOW_PROFILE to "claude" changes returned chains
    - Switching to "gemini" (default) returns gemini chains

  core/api_server.py — /api/settings/profile endpoints
    - GET returns active profile name + valid_profiles list
    - POST with valid profile name switches active profile + returns info
    - POST with unknown profile name returns 400 error
    - POST with malformed body returns 400 error
"""

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(profile: str = "gemini", role_chain_override: dict | None = None):
    """Return a minimal settings-like object for ai_backend tests."""
    s = MagicMock()
    s.WORKFLOW_PROFILE = profile
    # Default: no per-role overrides (empty string → treated as unset)
    _role_attrs = {
        "ROLE_INTERVIEW_CHAIN": "",
        "ROLE_PLANNER_CHAIN": "",
        "ROLE_ORCHESTRATOR_CHAIN": "",
        "ROLE_CHUNKER_CHAIN": "",
        "ROLE_AUDITOR_CHAIN": "",
        "ROLE_CONTRACT_CHAIN": "",
        "ROLE_GIANT_BRAIN_CHAIN": "",
        "ROLE_DEV_SWARM_T1_CHAIN": "",
        "ROLE_DEV_SWARM_T2_CHAIN": "",
        "ROLE_DEV_SWARM_T3_CHAIN": "",
        "ROLE_TEST_SWARM_T1_CHAIN": "",
        "ROLE_TEST_SWARM_T2_CHAIN": "",
        "ROLE_BIG_FIXER_CHAIN": "",
        "ROLE_MCP_CHAIN": "",
        "ROLE_MEMORY_CHAIN": "",
    }
    if role_chain_override:
        _role_attrs.update(role_chain_override)
    for attr, val in _role_attrs.items():
        setattr(s, attr, val)
    return s


def _make_backend(profile: str = "gemini", role_chain_override: dict | None = None):
    """Return a real AIBackend instance with a mocked settings object."""
    from core.ai_backend import AIBackend
    backend = object.__new__(AIBackend)
    backend.settings = _make_settings(profile, role_chain_override)
    return backend


# ---------------------------------------------------------------------------
# config/profiles.py — unit tests
# ---------------------------------------------------------------------------

def test_get_profile_chain_gemini_interview():
    """get_profile_chain('gemini', 'interview') must return a non-empty string."""
    from config.profiles import get_profile_chain
    result = get_profile_chain("gemini", "interview")
    assert result is not None
    assert "gemini" in result.lower()


def test_get_profile_chain_gemini_dev_swarm_t1_is_flash_minimal():
    """Gemini profile dev_swarm_t1 must use flash-minimal as primary (Session 58: swarm = MINIMAL thinking)."""
    from config.profiles import get_profile_chain
    result = get_profile_chain("gemini", "dev_swarm_t1")
    assert result is not None
    first_backend = result.split(",")[0].strip()
    assert first_backend == "gemini-flash-minimal", (
        f"dev_swarm_t1 primary must be gemini-flash-minimal, got: {first_backend}"
    )


def test_get_profile_chain_unknown_profile_returns_none():
    """get_profile_chain returns None for an unrecognised profile name."""
    from config.profiles import get_profile_chain
    assert get_profile_chain("turbo_mode", "interview") is None


def test_get_profile_chain_unknown_role_returns_none():
    """get_profile_chain returns None when the role is not in the profile."""
    from config.profiles import get_profile_chain
    assert get_profile_chain("gemini", "nonexistent_role_xyz") is None


def test_default_profile_is_gemini():
    """DEFAULT_PROFILE must be 'gemini'."""
    from config.profiles import DEFAULT_PROFILE
    assert DEFAULT_PROFILE == "gemini"


def test_all_profiles_have_same_role_keys():
    """All three profiles must define exactly the same set of role keys."""
    from config.profiles import PROFILES
    key_sets = {name: frozenset(profile.keys()) for name, profile in PROFILES.items()}
    all_equal = len(set(key_sets.values())) == 1
    assert all_equal, (
        "Profile role key sets differ: "
        + str({name: sorted(ks) for name, ks in key_sets.items()})
    )


def test_profile_info_gemini_is_tested():
    """profile_info('gemini') must report tested=True."""
    from config.profiles import profile_info
    info = profile_info("gemini")
    assert info["tested"] is True
    assert info["name"] == "gemini"


def test_profile_info_claude_is_untested():
    """profile_info('claude') must report tested=False."""
    from config.profiles import profile_info
    info = profile_info("claude")
    assert info["tested"] is False


def test_profile_info_qwen_is_untested():
    """profile_info('qwen') must report tested=False."""
    from config.profiles import profile_info
    info = profile_info("qwen")
    assert info["tested"] is False


def test_profile_required_keys_gemini():
    """Gemini profile requires GOOGLE_API_KEY."""
    from config.profiles import PROFILE_REQUIRED_KEYS
    assert "GOOGLE_API_KEY" in PROFILE_REQUIRED_KEYS["gemini"]


def test_profile_required_keys_claude():
    """Claude profile requires ANTHROPIC_API_KEY."""
    from config.profiles import PROFILE_REQUIRED_KEYS
    assert "ANTHROPIC_API_KEY" in PROFILE_REQUIRED_KEYS["claude"]


def test_profile_required_keys_qwen():
    """Qwen profile requires QWEN_API_KEY."""
    from config.profiles import PROFILE_REQUIRED_KEYS
    assert "QWEN_API_KEY" in PROFILE_REQUIRED_KEYS["qwen"]


# ---------------------------------------------------------------------------
# get_role_chain — priority tier tests
# ---------------------------------------------------------------------------

def test_priority1_env_override_wins_over_profile():
    """When ROLE_INTERVIEW_CHAIN is set explicitly, it wins over the profile chain."""
    backend = _make_backend(
        profile="gemini",
        role_chain_override={"ROLE_INTERVIEW_CHAIN": "claude-haiku,qwen-flash"},
    )
    chain = backend.get_role_chain("interview")
    assert chain == ["claude-haiku", "qwen-flash"], (
        f"Env override must win; got: {chain}"
    )


def test_priority2_profile_used_when_no_env_override():
    """When no env override, active profile chain is returned."""
    backend = _make_backend(profile="gemini")
    chain = backend.get_role_chain("dev_swarm_t1")
    # Session 58: Gemini profile dev_swarm_t1 = "gemini-flash-minimal,gemini-flash,claude-haiku"
    assert chain[0] == "gemini-flash-minimal", (
        f"Profile chain primary must be gemini-flash-minimal; got: {chain}"
    )


def test_priority3_hardcoded_default_when_profile_missing_role():
    """When profile has no entry for the role, hardcoded default is used."""
    backend = _make_backend(profile="gemini")
    # Patch get_profile_chain to return None (simulates missing role in profile)
    with patch("config.profiles.get_profile_chain", return_value=None):
        chain = backend.get_role_chain("giant_brain")
    # Session 58 post-run: _ROLE_DEFAULTS["giant_brain"] = "gemini-customtools,gemini-flash-high,claude"
    assert "gemini-customtools" in chain, (
        f"Hardcoded default for giant_brain must include gemini-customtools; got: {chain}"
    )


def test_switching_to_claude_profile_returns_claude_chains():
    """After switching WORKFLOW_PROFILE to 'claude', chains start with claude backends."""
    backend = _make_backend(profile="claude")
    chain = backend.get_role_chain("planner")
    # Claude profile planner = "claude,claude-haiku"
    assert chain[0].startswith("claude"), (
        f"Claude profile planner primary must start with 'claude'; got: {chain}"
    )


def test_switching_to_gemini_profile_returns_gemini_chains():
    """With WORKFLOW_PROFILE='gemini', primary backend uses a gemini model."""
    backend = _make_backend(profile="gemini")
    chain = backend.get_role_chain("orchestrator")
    # Session 58: chain is gemini-flash-high,gemini-customtools,claude-haiku — primary must be gemini
    assert "gemini" in chain[0], (
        f"Gemini profile orchestrator primary must be a gemini backend; got: {chain}"
    )


def test_get_role_chain_unknown_role_returns_fallback():
    """Unknown role key falls through to ['claude'] default."""
    backend = _make_backend(profile="gemini")
    chain = backend.get_role_chain("totally_unknown_role")
    assert chain == ["claude"], f"Unknown role must return ['claude']; got: {chain}"


def test_get_role_chain_normalises_hyphens():
    """Role name with hyphens is normalised to underscores (dev-swarm-t1 == dev_swarm_t1)."""
    backend = _make_backend(profile="gemini")
    chain_hyphen = backend.get_role_chain("dev-swarm-t1")
    chain_under = backend.get_role_chain("dev_swarm_t1")
    assert chain_hyphen == chain_under, (
        f"Hyphenated and underscored role names must produce the same chain; "
        f"got: {chain_hyphen} vs {chain_under}"
    )


def test_get_role_chain_case_insensitive():
    """Role lookup is case-insensitive."""
    backend = _make_backend(profile="gemini")
    chain_upper = backend.get_role_chain("PLANNER")
    chain_lower = backend.get_role_chain("planner")
    assert chain_upper == chain_lower


def test_profiles_import_does_not_raise_on_invalid_profile():
    """get_profile_chain with None profile falls back to default profile gracefully."""
    from config.profiles import get_profile_chain
    # Should not raise; returns the default profile (gemini) chain for None input
    result = get_profile_chain(None, "mcp")
    assert result is not None  # falls back to DEFAULT_PROFILE


# ---------------------------------------------------------------------------
# /api/settings/profile — API endpoint tests
# ---------------------------------------------------------------------------

def _make_server():
    """Return a minimal APIServer with mocked internals."""
    from core.api_server import DashboardAPI
    server = object.__new__(DashboardAPI)
    server.settings = _make_settings(profile="gemini")
    server.ai_backend = MagicMock()
    return server


@pytest.mark.asyncio
async def test_get_profile_returns_active_profile():
    """GET /api/settings/profile returns active_profile and valid_profiles."""
    import json
    from aiohttp.test_utils import make_mocked_request
    server = _make_server()
    request = make_mocked_request("GET", "/api/settings/profile")
    response = await server.handle_get_profile(request)
    data = json.loads(response.body)
    assert data["active_profile"] == "gemini"
    assert "valid_profiles" in data
    assert "gemini" in data["valid_profiles"]
    assert "profile_info" in data


@pytest.mark.asyncio
async def test_post_profile_switches_to_claude():
    """POST /api/settings/profile with valid 'claude' body mutates settings."""
    import json
    from unittest.mock import AsyncMock
    from aiohttp.test_utils import make_mocked_request
    server = _make_server()

    async def _fake_json():
        return {"profile": "claude"}

    request = make_mocked_request("POST", "/api/settings/profile")
    request.json = _fake_json
    response = await server.handle_post_profile(request)
    data = json.loads(response.body)
    assert data["success"] is True
    assert data["active_profile"] == "claude"
    assert data["previous_profile"] == "gemini"
    # settings must be mutated
    assert server.settings.WORKFLOW_PROFILE == "claude"


@pytest.mark.asyncio
async def test_post_profile_rejects_unknown_profile():
    """POST /api/settings/profile with unknown profile name returns 400."""
    import json
    from aiohttp.test_utils import make_mocked_request
    server = _make_server()

    async def _fake_json():
        return {"profile": "ultra_mode"}

    request = make_mocked_request("POST", "/api/settings/profile")
    request.json = _fake_json
    response = await server.handle_post_profile(request)
    assert response.status == 400
    data = json.loads(response.body)
    assert data["success"] is False
    assert "ultra_mode" in data["error"]


@pytest.mark.asyncio
async def test_post_profile_rejects_missing_profile_key():
    """POST /api/settings/profile with no 'profile' key returns 400."""
    import json
    from aiohttp.test_utils import make_mocked_request
    server = _make_server()

    async def _fake_json():
        return {"name": "gemini"}  # wrong key

    request = make_mocked_request("POST", "/api/settings/profile")
    request.json = _fake_json
    response = await server.handle_post_profile(request)
    assert response.status == 400
    data = json.loads(response.body)
    assert data["success"] is False


def test_workflow_profile_setting_exists_on_settings():
    """Settings object must expose WORKFLOW_PROFILE attribute defaulting to 'gemini'."""
    from config.settings import Settings
    with patch.dict("os.environ", {}, clear=False):
        s = Settings()
    assert hasattr(s, "WORKFLOW_PROFILE"), "Settings must have WORKFLOW_PROFILE"
    # default when WORKFLOW_PROFILE not in env
    assert s.WORKFLOW_PROFILE in ("gemini", ""), (
        f"WORKFLOW_PROFILE should default to 'gemini'; got: {s.WORKFLOW_PROFILE!r}"
    )
