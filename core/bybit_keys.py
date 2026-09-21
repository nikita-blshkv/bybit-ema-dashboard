"""
Bybit demo API key/secret storage.

Simplest possible approach: keys live in a small JSON file inside the
app's persistent STATE_DIR (same folder as trade_log.csv / secrets.json --
already resolved correctly whether running from source or from a frozen
PyInstaller build on Mac or Windows). Env vars still work and always win,
for anyone who prefers that -- but the normal path is just filling in the
key/secret on the dashboard's Settings panel, no file editing required.
"""
import os
import json

from . import config

_KEYS_FILE = config.STATE_DIR / "bybit_demo_keys.json"


def _load_from_file():
    if _KEYS_FILE.exists():
        try:
            with open(_KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("api_key", ""), data.get("api_secret", "")
        except Exception:
            pass
    return "", ""


_file_key, _file_secret = _load_from_file()

BYBIT_DEMO_API_KEY = os.environ.get("BYBIT_DEMO_API_KEY", "") or _file_key
BYBIT_DEMO_API_SECRET = os.environ.get("BYBIT_DEMO_API_SECRET", "") or _file_secret
BYBIT_DEMO_BASE_URL = "https://api-demo.bybit.com"


def set_demo_keys(api_key: str, api_secret: str):
    """Called from the dashboard's Settings form (POST /api/bybit_keys).
    Updates this module's globals immediately (every call site reads
    bybit_keys.BYBIT_DEMO_API_KEY fresh at call time, so no restart is
    needed) and persists to disk so it survives app restarts."""
    global BYBIT_DEMO_API_KEY, BYBIT_DEMO_API_SECRET
    BYBIT_DEMO_API_KEY = (api_key or "").strip()
    BYBIT_DEMO_API_SECRET = (api_secret or "").strip()
    with open(_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump({"api_key": BYBIT_DEMO_API_KEY, "api_secret": BYBIT_DEMO_API_SECRET}, f)


def keys_configured() -> bool:
    return bool(BYBIT_DEMO_API_KEY) and bool(BYBIT_DEMO_API_SECRET)
