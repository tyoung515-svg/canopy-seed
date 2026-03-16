"""
Session 55 tests — Built-in PBKDF2+AES-GCM key vault.

Covers:
  memory/vault_store.py
    - setup_vault writes a valid file
    - unlock decrypts and returns the correct keys
    - wrong password raises ValueError (not just any error)
    - is_vault_setup() reflects file presence
    - is_unlocked() reflects in-memory state
    - get_unlocked_keys() raises when locked, returns copy when unlocked
    - lock() wipes in-memory state
    - reset_vault() deletes the file
    - setup_vault raises FileExistsError if vault already exists
    - vault file starts with magic bytes b"CSVT"
    - vault file version byte is 0x01
    - two vaults encrypted with same password produce different ciphertext (nonce)

  core/api_server.py — vault endpoints
    - GET /api/vault/status returns setup/unlocked/forge_connected keys
    - POST /api/vault/setup creates vault, returns success + keys_loaded
    - POST /api/vault/setup returns 400 if password missing
    - POST /api/vault/setup returns 409 if vault already exists
    - POST /api/vault/unlock returns success with correct password
    - POST /api/vault/unlock returns 401 with wrong password
    - POST /api/vault/unlock returns 404 if no vault configured
    - POST /api/vault/lock returns success
    - POST /api/vault/reset deletes vault
    - _push_keys_to_settings propagates google, anthropic, qwen, openai keys
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault.enc"


def _fresh_vault_store(tmp_path: Path):
    """Import vault_store and redirect it to a temp path. Returns the module."""
    import importlib
    import memory.vault_store as vs
    importlib.reload(vs)               # reset _UNLOCKED_KEYS and _vault_path
    vs.set_vault_path(tmp_path / "vault.enc")
    return vs


# ---------------------------------------------------------------------------
# memory/vault_store.py — unit tests
# ---------------------------------------------------------------------------

def test_setup_creates_file(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    assert not vs.is_vault_setup()
    vs.setup_vault({"google_api_key": "test-key"}, "pass1234")
    assert vs.is_vault_setup()
    assert (tmp_path / "vault.enc").exists()


def test_vault_file_magic_bytes(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    vs.setup_vault({"google_api_key": "gk"}, "pw")
    with (tmp_path / "vault.enc").open("rb") as f:
        assert f.read(4) == b"CSVT"


def test_vault_file_version_byte(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    vs.setup_vault({"google_api_key": "gk"}, "pw")
    with (tmp_path / "vault.enc").open("rb") as f:
        f.read(4)           # magic
        assert f.read(1) == bytes([0x01])


def test_unlock_returns_correct_keys(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    keys = {"google_api_key": "AIza-test", "anthropic_api_key": "sk-ant-test"}
    vs.setup_vault(keys, "mypassword")
    vs.lock()   # ensure not auto-unlocked from setup
    result = vs.unlock("mypassword")
    assert result == keys


def test_wrong_password_raises_value_error(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    vs.setup_vault({"google_api_key": "gk"}, "correct-pass")
    vs.lock()
    with pytest.raises(ValueError, match="decryption failed|wrong password"):
        vs.unlock("wrong-pass")


def test_is_unlocked_after_unlock(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    vs.setup_vault({"k": "v"}, "pw")
    assert vs.is_unlocked()   # setup auto-unlocks


def test_is_unlocked_false_after_lock(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    vs.setup_vault({"k": "v"}, "pw")
    vs.lock()
    assert not vs.is_unlocked()


def test_get_unlocked_keys_raises_when_locked(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    vs.setup_vault({"k": "v"}, "pw")
    vs.lock()
    with pytest.raises(RuntimeError, match="locked"):
        vs.get_unlocked_keys()


def test_get_unlocked_keys_returns_copy(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    vs.setup_vault({"key": "val"}, "pw")
    keys = vs.get_unlocked_keys()
    keys["key"] = "mutated"
    # Internal state must not be mutated
    assert vs.get_unlocked_keys()["key"] == "val"


def test_reset_deletes_file(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    vs.setup_vault({"k": "v"}, "pw")
    assert vs.is_vault_setup()
    vs.reset_vault()
    assert not vs.is_vault_setup()
    assert not (tmp_path / "vault.enc").exists()


def test_setup_raises_if_already_exists(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    vs.setup_vault({"k": "v"}, "pw")
    with pytest.raises(FileExistsError):
        vs.setup_vault({"k2": "v2"}, "pw2")


def test_setup_raises_on_empty_password(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    with pytest.raises(ValueError, match="password"):
        vs.setup_vault({"k": "v"}, "")


def test_two_vaults_same_password_different_ciphertext(tmp_path):
    """AES-GCM uses a random nonce per encryption — same key, different output."""
    import importlib
    import memory.vault_store as vs1_mod
    import memory.vault_store as vs2_mod

    p1 = tmp_path / "v1.enc"
    p2 = tmp_path / "v2.enc"

    importlib.reload(vs1_mod)
    vs1_mod.set_vault_path(p1)
    vs1_mod.setup_vault({"k": "same-value"}, "samepassword")

    importlib.reload(vs2_mod)
    vs2_mod.set_vault_path(p2)
    vs2_mod.setup_vault({"k": "same-value"}, "samepassword")

    assert p1.read_bytes() != p2.read_bytes(), (
        "Two vaults with identical content + password must differ (random nonce)"
    )


def test_vault_status_dict_keys(tmp_path):
    vs = _fresh_vault_store(tmp_path)
    status = vs.vault_status()
    assert "setup"    in status
    assert "unlocked" in status


def test_is_vault_setup_false_for_corrupt_magic(tmp_path):
    """A file with wrong magic is not considered set up."""
    p = tmp_path / "vault.enc"
    p.write_bytes(b"JUNK" + b"\x00" * 50)
    vs = _fresh_vault_store(tmp_path)
    vs.set_vault_path(p)   # point at the corrupt file directly
    # Re-check after pointing at corrupt file
    import importlib, memory.vault_store as vsm
    vsm.set_vault_path(p)
    assert not vsm.is_vault_setup()


# ---------------------------------------------------------------------------
# /api/vault/* endpoint tests
# ---------------------------------------------------------------------------

def _make_server_with_vault(tmp_path):
    """DashboardAPI instance with its vault redirected to tmp_path."""
    from core.api_server import DashboardAPI
    import memory.vault_store as vs
    import importlib
    importlib.reload(vs)
    vs.set_vault_path(tmp_path / "vault.enc")

    server = object.__new__(DashboardAPI)
    server.settings = MagicMock()
    server.settings.WORKFLOW_PROFILE = "gemini"
    server.ai_backend = MagicMock()
    server.ai_backend._claude_client = None
    return server


@pytest.mark.asyncio
async def test_vault_status_returns_expected_keys(tmp_path):
    import json
    from aiohttp.test_utils import make_mocked_request
    server = _make_server_with_vault(tmp_path)

    with patch.object(server, '_forge_connected', return_value=False):
        request = make_mocked_request("GET", "/api/vault/status")
        resp = await server.handle_vault_status(request)

    data = json.loads(resp.body)
    assert "setup"           in data
    assert "unlocked"        in data
    assert "forge_connected" in data
    assert "active_profile"  in data
    assert data["setup"]    is False
    assert data["unlocked"] is False


@pytest.mark.asyncio
async def test_vault_setup_endpoint_creates_vault(tmp_path):
    import json
    from aiohttp.test_utils import make_mocked_request
    server = _make_server_with_vault(tmp_path)

    async def _json():
        return {
            "keys": {"google_api_key": "AIza-test"},
            "password": "test1234pass",
            "profile": "gemini",
        }

    request = make_mocked_request("POST", "/api/vault/setup")
    request.json = _json
    resp = await server.handle_vault_setup(request)
    data = json.loads(resp.body)
    assert data["success"] is True
    assert "google_api_key" in data["keys_loaded"]
    assert (tmp_path / "vault.enc").exists()


@pytest.mark.asyncio
async def test_vault_setup_returns_400_missing_password(tmp_path):
    import json
    from aiohttp.test_utils import make_mocked_request
    server = _make_server_with_vault(tmp_path)

    async def _json():
        return {"keys": {"google_api_key": "k"}, "password": ""}

    request = make_mocked_request("POST", "/api/vault/setup")
    request.json = _json
    resp = await server.handle_vault_setup(request)
    assert resp.status == 400
    data = json.loads(resp.body)
    assert data["success"] is False


@pytest.mark.asyncio
async def test_vault_setup_returns_409_if_already_exists(tmp_path):
    import json
    from aiohttp.test_utils import make_mocked_request
    server = _make_server_with_vault(tmp_path)

    async def _json():
        return {"keys": {"google_api_key": "k"}, "password": "pass1234x"}

    # First setup
    request = make_mocked_request("POST", "/api/vault/setup")
    request.json = _json
    await server.handle_vault_setup(request)

    # Second setup must return 409
    request2 = make_mocked_request("POST", "/api/vault/setup")
    request2.json = _json
    resp = await server.handle_vault_setup(request2)
    assert resp.status == 409


@pytest.mark.asyncio
async def test_vault_unlock_correct_password(tmp_path):
    import json, importlib, memory.vault_store as vs
    importlib.reload(vs)
    vs.set_vault_path(tmp_path / "vault.enc")
    vs.setup_vault({"google_api_key": "my-key"}, "correct")
    vs.lock()

    from aiohttp.test_utils import make_mocked_request
    server = _make_server_with_vault(tmp_path)

    async def _json():
        return {"password": "correct"}

    request = make_mocked_request("POST", "/api/vault/unlock")
    request.json = _json
    resp = await server.handle_vault_unlock(request)
    data = json.loads(resp.body)
    assert data["success"] is True
    assert "google_api_key" in data["keys_loaded"]


@pytest.mark.asyncio
async def test_vault_unlock_wrong_password_returns_401(tmp_path):
    import json, importlib, memory.vault_store as vs
    importlib.reload(vs)
    vs.set_vault_path(tmp_path / "vault.enc")
    vs.setup_vault({"google_api_key": "k"}, "correct")
    vs.lock()

    from aiohttp.test_utils import make_mocked_request
    server = _make_server_with_vault(tmp_path)

    async def _json():
        return {"password": "wrong-one"}

    request = make_mocked_request("POST", "/api/vault/unlock")
    request.json = _json
    resp = await server.handle_vault_unlock(request)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_vault_unlock_returns_404_if_no_vault(tmp_path):
    import json
    from aiohttp.test_utils import make_mocked_request
    server = _make_server_with_vault(tmp_path)   # no vault created

    async def _json():
        return {"password": "anything"}

    request = make_mocked_request("POST", "/api/vault/unlock")
    request.json = _json
    resp = await server.handle_vault_unlock(request)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_vault_lock_endpoint(tmp_path):
    import json
    from aiohttp.test_utils import make_mocked_request
    server = _make_server_with_vault(tmp_path)

    request = make_mocked_request("POST", "/api/vault/lock")
    resp = await server.handle_vault_lock(request)
    data = json.loads(resp.body)
    assert data["success"] is True


@pytest.mark.asyncio
async def test_vault_reset_endpoint_deletes_file(tmp_path):
    import json, importlib, memory.vault_store as vs
    importlib.reload(vs)
    vs.set_vault_path(tmp_path / "vault.enc")
    vs.setup_vault({"k": "v"}, "pw1234xx")

    from aiohttp.test_utils import make_mocked_request
    server = _make_server_with_vault(tmp_path)

    request = make_mocked_request("POST", "/api/vault/reset")
    resp = await server.handle_vault_reset(request)
    data = json.loads(resp.body)
    assert data["success"] is True
    assert not (tmp_path / "vault.enc").exists()


def test_push_keys_to_settings_propagates_all_fields(tmp_path):
    """_push_keys_to_settings updates google, anthropic, qwen, openai on settings."""
    from core.api_server import DashboardAPI
    server = object.__new__(DashboardAPI)
    server.settings = MagicMock()
    server.settings.GOOGLE_API_KEY = ""
    server.settings.GEMINI_API_KEY = ""
    server.settings.ANTHROPIC_API_KEY = ""
    server.settings.OPENAI_API_KEY = ""
    server.settings.QWEN_API_KEY = ""
    server.ai_backend = MagicMock()
    server.ai_backend._claude_client = MagicMock()

    keys = {
        "google_api_key":    "g-key",
        "anthropic_api_key": "a-key",
        "openai_api_key":    "o-key",
        "qwen_api_key":      "q-key",
    }
    server._push_keys_to_settings(keys)

    assert server.settings.GOOGLE_API_KEY    == "g-key"
    assert server.settings.GEMINI_API_KEY    == "g-key"
    assert server.settings.ANTHROPIC_API_KEY == "a-key"
    assert server.settings.OPENAI_API_KEY    == "o-key"
    assert server.settings.QWEN_API_KEY      == "q-key"
    # Claude client must be reset after new anthropic key
    assert server.ai_backend._claude_client is None


def test_vault_status_forge_connected_false_when_no_forge(tmp_path):
    """_forge_connected() must return False gracefully when proxy is not running."""
    from core.api_server import DashboardAPI
    server = object.__new__(DashboardAPI)
    server.settings = MagicMock()
    server.settings.WORKFLOW_PROFILE = "gemini"
    server.ai_backend = MagicMock()
    # localhost:5151 won't be running in test — must not raise
    result = server._forge_connected()
    assert result is False
