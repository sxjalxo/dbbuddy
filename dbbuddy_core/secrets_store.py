"""Storage for provider API keys.

Persistence prefers the OS keyring (Windows Credential Manager, macOS Keychain,
Secret Service on Linux) so keys are encrypted at rest — suitable for production.
When no usable keyring backend is available (common in headless/local dev), it
falls back to a JSON file under the user's home directory.

The active key is mirrored into the process environment so the existing
``os.getenv()`` lookups in ai.py keep working. The key value is never returned
to callers — only whether one is configured — so it can never be displayed back.
"""

import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import keyring
    _KEYRING_IMPORTED = True
except Exception:  # pragma: no cover - keyring optional
    keyring = None
    _KEYRING_IMPORTED = False

# Service name under which keys are stored in the OS keyring.
KEYRING_SERVICE = "dbbuddy"

# Only providers that actually require a key are managed here.
PROVIDER_ENV_VARS = {
    "nemotron": "NEMOTRON_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# Plaintext fallback, used only when no OS keyring backend is available.
SECRETS_FILE = Path.home() / ".dbbuddy" / "secrets.json"


def _keyring_usable() -> bool:
    """True only when keyring is importable and has a real (non-fail) backend."""
    if not _KEYRING_IMPORTED:
        return False
    try:
        backend = type(keyring.get_keyring()).__name__.lower()
        return "fail" not in backend and "null" not in backend
    except Exception:
        return False


def backend_name() -> str:
    """Which storage backend is active: 'keyring' or 'file'."""
    return "keyring" if _keyring_usable() else "file"


# --- keyring backend ---------------------------------------------------------

def _kr_get(provider: str):
    if not _KEYRING_IMPORTED:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, provider)
    except Exception:
        return None


def _kr_set(provider: str, key: str) -> bool:
    if not _keyring_usable():
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, provider, key)
        return True
    except Exception as exc:
        logger.warning(f"Keyring write failed, falling back to file storage: {exc}")
        return False


def _kr_delete(provider: str) -> None:
    if not _KEYRING_IMPORTED:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, provider)
    except Exception:
        pass  # not present / backend unavailable — nothing to remove


# --- file fallback backend ---------------------------------------------------

def _file_load() -> dict:
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _file_save(data: dict) -> None:
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    # Best-effort: restrict to owner read/write on POSIX (no-op on Windows).
    try:
        os.chmod(SECRETS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _file_get(provider: str):
    return _file_load().get(provider)


def _file_set(provider: str, key: str) -> None:
    data = _file_load()
    data[provider] = key
    _file_save(data)


def _file_delete(provider: str) -> None:
    data = _file_load()
    if provider in data:
        del data[provider]
        _file_save(data)


# --- public API --------------------------------------------------------------

def _require_supported(provider: str) -> str:
    env_var = PROVIDER_ENV_VARS.get(provider)
    if not env_var:
        raise ValueError(
            f"Unsupported provider '{provider}'. Managed providers: {', '.join(PROVIDER_ENV_VARS)}."
        )
    return env_var


def set_api_key(provider: str, key: str) -> None:
    """Persist and activate an API key. Raises ValueError on bad input."""
    env_var = _require_supported(provider)
    key = (key or "").strip()
    if not key:
        raise ValueError("API key must not be empty.")

    if _kr_set(provider, key):
        # Stored encrypted in the keyring — drop any stale plaintext copy.
        _file_delete(provider)
        backend = "keyring"
    else:
        _file_set(provider, key)
        backend = "file"

    os.environ[env_var] = key

    logger.info(f"API key for '{provider}' stored using backend: {backend}")
    if backend == "file":
        logger.warning(
            "No OS keyring backend available — API key stored in PLAINTEXT at "
            f"{SECRETS_FILE}. Install/configure a keyring backend for encrypted storage."
        )


def delete_api_key(provider: str) -> None:
    """Remove a stored key from every backend and the process environment."""
    env_var = _require_supported(provider)
    _kr_delete(provider)
    _file_delete(provider)
    os.environ.pop(env_var, None)


def get_api_key(provider: str):
    """Return the configured API key for a provider, or None.

    Prefer this over reading the provider's environment variable directly — it
    is per-provider and resolves from every backend, avoiding env-var collisions
    as more providers are added. Resolution order: environment (covers an
    externally set var and the value applied on startup) -> keyring -> file.
    """
    env_var = _require_supported(provider)
    return os.environ.get(env_var) or _kr_get(provider) or _file_get(provider)


def has_api_key(provider: str) -> bool:
    """Whether a key is configured in any backend or the environment."""
    return bool(get_api_key(provider))


def apply_to_env() -> None:
    """Load persisted keys into the process environment.

    Call once on startup. An externally set environment variable takes
    precedence and is not overwritten.
    """
    for provider, env_var in PROVIDER_ENV_VARS.items():
        if os.environ.get(env_var):
            continue
        value = _kr_get(provider) or _file_get(provider)
        if value:
            os.environ[env_var] = value
