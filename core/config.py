"""
Central configuration for the EMA cross short/long strategy dashboard.

All strategy parameters live here so the live engine and the backtest
engine share exactly the same defaults as leverage_sweep_short_only.py.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# On Railway, a persistent volume is mounted (e.g. at /data) so that
# state/*.json survives every redeploy. RAILWAY_VOLUME_MOUNT_PATH is set
# automatically by Railway when a volume is attached. Locally (no volume),
# this env var is unset, so it falls back to BASE_DIR/state as before.
_VOLUME_MOUNT = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
STATE_DIR = Path(_VOLUME_MOUNT) if _VOLUME_MOUNT else (BASE_DIR / "state")
BACKTEST_DIR = BASE_DIR / "data" / "backtest"

DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Symbols (v1 scope: BTC + ETH perpetuals on Bybit)
# ---------------------------------------------------------------------------
SYMBOLS = [
    "BTCUSDT", "ETHUSDT",
    "XRPUSDT",     # Ripple
    "BNBUSDT",
    "SOLUSDT",     # Solana
    "HYPEUSDT",    # Hyperliquid
    "ZECUSDT",     # Zcash
    "LTCUSDT",     # Litecoin
    "HBARUSDT",    # Hedera
    "SUIUSDT",
    "NEARUSDT",
    "OKBUSDT",
    "ASTERUSDT",
    "MNTUSDT",     # Mantle
    "DOTUSDT",     # Polkadot
    "WLDUSDT",     # Worldcoin
]
# NOTE: TRXUSDT / DOGEUSDT removed earlier (poor backtest performance).
# Second cleanup pass removed: LINKUSDT, ADAUSDT, XMRUSDT, XLMUSDT,
# BCHUSDT, AVAXUSDT, 1000SHIBUSDT, UNIUSDT, PUMPUSDT, AAVEUSDT, ONDOUSDT,
# ENAUSDT, 1000PEPEUSDT, ICPUSDT -- kept OKBUSDT and ASTERUSDT explicitly
# per user request. 16 symbols total now.

# Timeframes exposed on the dashboard chart switcher.
# Values are Bybit v5 kline "interval" strings.
TIMEFRAMES = {
    "1m": "1",
    "5m": "5",
    "1h": "60",
}

CANDLES_PER_TIMEFRAME = 50  # how many candles the chart keeps/shows per symbol/timeframe

# ---------------------------------------------------------------------------
# Strategy defaults (identical to leverage_sweep_short_only.py)
# ---------------------------------------------------------------------------
DEFAULT_EMA_FAST = 7
DEFAULT_EMA_SLOW = 133
DEFAULT_TP_PCT = 2.8   # percent
DEFAULT_SL_PCT = 1.2   # percent
DEFAULT_DIRECTION = "both"   # "both" | "long" | "short"

DEFAULT_INITIAL_EQUITY = 100000.0
DEFAULT_MARGIN_PER_TRADE = 1000.0
DEFAULT_LEVERAGE = 10.0
DEFAULT_MAX_OPEN_POSITIONS = 10

DEFAULT_TAKER_FEE_PCT = 0.05   # percent, per side
DEFAULT_REBATE_PCT = 70.0      # percent of fees rebated

# ---------------------------------------------------------------------------
# Bybit public API (no keys required for market data)
# ---------------------------------------------------------------------------
BYBIT_BASE_URL = "https://api.bybit.com"
BYBIT_CATEGORY = "linear"  # USDT perpetuals

# ---------------------------------------------------------------------------
# API keys (for future live order execution) -- NEVER commit this file.
# Keys are loaded from environment variables or from a local, gitignored
# secrets.json placed next to this config. Nothing is ever sent anywhere
# except directly to Bybit's own API from your machine.
# ---------------------------------------------------------------------------
SECRETS_PATH = BASE_DIR / "secrets.json"


def load_bybit_keys():
    """Returns (api_key, api_secret) or (None, None) if not configured yet."""
    api_key = os.environ.get("BYBIT_API_KEY")
    api_secret = os.environ.get("BYBIT_API_SECRET")
    if api_key and api_secret:
        return api_key, api_secret

    if SECRETS_PATH.exists():
        import json
        try:
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("api_key"), data.get("api_secret")
        except Exception:
            pass

    return None, None


# Poll interval for the live engine loop, in seconds.
POLL_INTERVAL_SECONDS = 5

# Engine + trade journal persistence files
ENGINE_STATE_FILE = STATE_DIR / "engine_state.json"
OPEN_POSITIONS_FILE = STATE_DIR / "open_positions.json"
TRADE_LOG_FILE = STATE_DIR / "trade_log.csv"
EQUITY_CURVE_FILE = STATE_DIR / "equity_curve.csv"
