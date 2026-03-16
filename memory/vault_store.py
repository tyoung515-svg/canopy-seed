"""
Canopy Seed Built-in Key Vault (ADR-040 / Session 55)
------------------------------------------------------
PBKDF2-SHA256 key derivation + AES-256-GCM authenticated encryption.

Vault file layout (memory/vault.enc):
    Bytes  0 –  3 : magic   b"CSVT"   (Canopy Seed Vault Token)
    Byte       4  : version  0x01
    Bytes  5 – 20 : salt     (16 bytes, random)
    Bytes 21 – 32 : nonce    (12 bytes, random)
    Bytes 33 –    : ciphertext + 16-byte GCM tag

KDF parameters:
    algorithm : PBKDF2-HMAC-SHA256
    iterations: 600_000   (OWASP 2023 recommendation for SHA-256)
    key length: 32 bytes  (AES-256)

Unlock lifecycle:
    - unlock() decrypts the vault and stores keys in _UNLOCKED_KEYS (in-memory only).
    - is_unlocked() returns True as long as the process is alive and unlock() succeeded.
    - On shutdown the in-memory dict is garbage-collected — nothing persists to disk
      except the encrypted vault file.

Forge bridge takes precedence:
    The Forge proxy (localhost:5151) pushes keys directly into settings via
    /vault/push.  When Forge is connected, the startup modal shows a "bypass"
    button so users can skip entering their local vault password.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_MAGIC   = b"CSVT"
_VERSION = 0x01
_SALT_LEN  = 16
_NONCE_LEN = 12
_KDF_ITER  = 600_000

# Path relative to vault_store.py — always resolves correctly regardless of CWD
_vault_path: Path = Path(__file__).resolve().parent / "vault.enc"

# In-memory unlocked state — cleared on process exit
_UNLOCKED_KEYS: Optional[dict] = None

# Canonical mapping from vault key name → environment variable name.
# Kept in one place so both _push_to_env() and any key-provider client
# can use the same translation table without duplication.
_KEY_ENV_MAP: dict = {
    "google_api_key":    "GOOGLE_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key":    "OPENAI_API_KEY",
    "qwen_api_key":      "QWEN_API_KEY",
    "dashscope_api_key": "DASHSCOPE_API_KEY",
}


# ── Env helpers ───────────────────────────────────────────────────────────────

def _push_to_env(keys: dict) -> None:
    """Write vault keys into os.environ using the canonical name mapping.

    This ensures that tools using os.getenv() (swarm tools, subprocesses,
    exported apps spawned by canopy-seed) see the keys without requiring any
    import of vault_store.
    """
    for vault_key, env_var in _KEY_ENV_MAP.items():
        val = keys.get(vault_key, "")
        if isinstance(val, str) and val.strip():
            os.environ[env_var] = val.strip()
    # GEMINI_API_KEY is always an alias for GOOGLE_API_KEY
    if "GOOGLE_API_KEY" in os.environ:
        os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]


def _wipe_from_env() -> None:
    """Remove vault-sourced keys from os.environ on lock.

    Only removes keys that were likely vault-sourced (non-empty env vars
    matching our known set).  Does not touch keys that were set externally
    (e.g. CI env vars) — but in practice if the vault was the source they
    will be gone, which is the safe default.
    """
    for env_var in list(_KEY_ENV_MAP.values()) + ["GEMINI_API_KEY"]:
        os.environ.pop(env_var, None)


# ── Public path helper ────────────────────────────────────────────────────────

def set_vault_path(path: Path | str) -> None:
    """Override the default vault file location (useful for tests)."""
    global _vault_path
    _vault_path = Path(path)


def get_vault_path() -> Path:
    return _vault_path


# ── Crypto helpers ─────────────────────────────────────────────────────────────

def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from password + salt using PBKDF2-HMAC-SHA256."""
    import hashlib
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _KDF_ITER,
        dklen=32,
    )


def _encrypt(plaintext: bytes, key: bytes) -> tuple[bytes, bytes]:
    """AES-256-GCM encrypt.  Returns (nonce, ciphertext_with_tag)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce, ct


def _decrypt(nonce: bytes, ciphertext: bytes, key: bytes) -> bytes:
    """AES-256-GCM decrypt.  Raises ValueError on tag mismatch (wrong password)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ValueError("Vault decryption failed — wrong password or corrupted file") from exc


# ── Public API ────────────────────────────────────────────────────────────────

def is_vault_setup() -> bool:
    """Return True if the vault file exists and starts with the correct magic."""
    p = _vault_path
    if not p.exists():
        return False
    try:
        with p.open("rb") as f:
            return f.read(4) == _MAGIC
    except OSError:
        return False


def is_unlocked() -> bool:
    """Return True if the vault has been unlocked this process lifetime."""
    return _UNLOCKED_KEYS is not None


def get_unlocked_keys() -> dict:
    """Return the in-memory key dict.  Raises RuntimeError if not yet unlocked."""
    if _UNLOCKED_KEYS is None:
        raise RuntimeError("Vault is locked — call unlock() first")
    return dict(_UNLOCKED_KEYS)


def setup_vault(keys: dict, password: str) -> None:
    """Encrypt *keys* with *password* and write to disk.

    *keys* should be a flat dict of string → string, e.g.:
        {"google_api_key": "AIza...", "anthropic_api_key": "sk-ant-..."}

    Raises ValueError if password is empty.
    Raises FileExistsError if vault already exists (use reset_vault() first).
    """
    if not password:
        raise ValueError("Vault password must not be empty")
    if is_vault_setup():
        raise FileExistsError(
            "Vault already exists. Call reset_vault() before re-setup."
        )

    plaintext = json.dumps(keys, ensure_ascii=False).encode("utf-8")
    salt = os.urandom(_SALT_LEN)
    key  = _derive_key(password, salt)
    nonce, ct = _encrypt(plaintext, key)

    _vault_path.parent.mkdir(parents=True, exist_ok=True)
    with _vault_path.open("wb") as f:
        f.write(_MAGIC)
        f.write(bytes([_VERSION]))
        f.write(salt)
        f.write(nonce)
        f.write(ct)

    logger.info("Vault created at %s (%d keys)", _vault_path, len(keys))

    # Auto-unlock immediately after creation so callers don't have to
    # call unlock() explicitly on first launch.
    global _UNLOCKED_KEYS
    _UNLOCKED_KEYS = dict(keys)
    _push_to_env(keys)   # make keys visible to os.getenv() everywhere


def unlock(password: str) -> dict:
    """Decrypt the vault with *password*, cache keys in-memory, and return them.

    Returns the decrypted key dict.
    Raises FileNotFoundError if no vault exists.
    Raises ValueError on wrong password / corrupted file.
    """
    global _UNLOCKED_KEYS

    if not is_vault_setup():
        raise FileNotFoundError(f"No vault found at {_vault_path}. Call setup_vault() first.")

    with _vault_path.open("rb") as f:
        magic   = f.read(4)
        version = f.read(1)[0]
        salt    = f.read(_SALT_LEN)
        nonce   = f.read(_NONCE_LEN)
        ct      = f.read()

    if magic != _MAGIC:
        raise ValueError(f"Invalid vault magic: {magic!r}")
    if version != _VERSION:
        raise ValueError(f"Unsupported vault version: {version}")

    key   = _derive_key(password, salt)
    plain = _decrypt(nonce, ct, key)
    keys  = json.loads(plain.decode("utf-8"))

    _UNLOCKED_KEYS = keys
    _push_to_env(keys)   # make keys visible to os.getenv() everywhere
    logger.info("Vault unlocked (%d keys)", len(keys))
    return dict(keys)


def lock() -> None:
    """Wipe the in-memory key cache and os.environ entries (lock the vault)."""
    global _UNLOCKED_KEYS
    _UNLOCKED_KEYS = None
    _wipe_from_env()     # remove keys from os.environ so subprocesses lose access
    logger.info("Vault locked (in-memory keys and env vars wiped)")


def update_vault(password: str, new_keys: dict) -> dict:
    """Verify *password*, merge *new_keys* into the vault, re-encrypt, and save.

    Existing keys not present in *new_keys* are preserved — so passing
    {"google_api_key": "new-key"} only replaces that one key.

    Returns the full merged key dict.
    Raises FileNotFoundError if no vault exists.
    Raises ValueError on wrong password or corrupted file.
    """
    if not new_keys:
        raise ValueError("new_keys must not be empty")

    # unlock() verifies the password and raises ValueError on mismatch
    current_keys = unlock(password)

    # Merge — new values win, old keys not in new_keys are kept
    merged = {**current_keys, **new_keys}

    # Re-encrypt with a fresh salt + nonce (same password)
    plaintext = json.dumps(merged, ensure_ascii=False).encode("utf-8")
    salt  = os.urandom(_SALT_LEN)
    key   = _derive_key(password, salt)
    nonce, ct = _encrypt(plaintext, key)

    _vault_path.parent.mkdir(parents=True, exist_ok=True)
    with _vault_path.open("wb") as f:
        f.write(_MAGIC)
        f.write(bytes([_VERSION]))
        f.write(salt)
        f.write(nonce)
        f.write(ct)

    global _UNLOCKED_KEYS
    _UNLOCKED_KEYS = merged
    _push_to_env(merged)   # refresh os.environ with any newly added keys
    logger.info("Vault updated (%d keys)", len(merged))
    return dict(merged)


def reset_vault() -> None:
    """Delete the vault file from disk (full wipe — requires new setup_vault call).

    This does NOT require a password — it's intended for 'forgot password' recovery.
    After reset, call setup_vault() again.
    """
    global _UNLOCKED_KEYS
    _UNLOCKED_KEYS = None
    if _vault_path.exists():
        _vault_path.unlink()
        logger.warning("Vault file deleted: %s", _vault_path)
    else:
        logger.info("reset_vault: no vault file to delete")


def vault_status() -> dict:
    """Return a status dict suitable for the /api/vault/status endpoint."""
    return {
        "setup":    is_vault_setup(),
        "unlocked": is_unlocked(),
    }
