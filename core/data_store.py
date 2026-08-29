"""
Local candle history storage per symbol/timeframe.

Raw exchange history is stored locally, and synthetic timeframes like 4m/8m
are derived by resampling 1m candles. This avoids relying on Bybit to serve
native 4m/8m klines, which it currently returns as empty lists.
"""

import pandas as pd
from pathlib import Path

from . import config
from . import bybit_client

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
RESAMPLED_FROM_1M = {
    "4m": "4min",
    "8m": "8min",
}
INTERVAL_MS = {
    "1": 60_000,
    "5": 5 * 60_000,
    "60": 60 * 60_000,
}


def _csv_path(symbol: str, tf_label: str) -> Path:
    return config.DATA_DIR / f"{symbol}_{tf_label}.csv"


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV_COLUMNS)


def _resample_ohlcv(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df_1m.empty:
        return _empty_df()

    out = (
        df_1m.sort_index()
        .resample(rule, label="right", closed="right")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
    )
    return out[OHLCV_COLUMNS]


def load_history(symbol: str, tf_label: str) -> pd.DataFrame:
    if tf_label in RESAMPLED_FROM_1M:
        base = load_history(symbol, "1m")
        return _resample_ohlcv(base, RESAMPLED_FROM_1M[tf_label])

    path = _csv_path(symbol, tf_label)
    if not path.exists():
        return _empty_df()

    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    for c in OHLCV_COLUMNS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[c for c in ["open", "high", "low", "close"] if c in df.columns])

    if not all(c in df.columns for c in OHLCV_COLUMNS):
        return _empty_df()

    return df[OHLCV_COLUMNS]


def save_history(symbol: str, tf_label: str, df: pd.DataFrame) -> None:
    if tf_label in RESAMPLED_FROM_1M:
        return
    path = _csv_path(symbol, tf_label)
    df.to_csv(path)


def sync_symbol_timeframe(symbol: str, tf_label: str, interval: str, min_candles: int = 50) -> pd.DataFrame:
    """
    Ensures local history has at least `min_candles` recent candles and is
    caught up to the latest closed candle. Synthetic 4m/8m frames are built
    from synced 1m history; only raw exchange-supported frames are fetched.
    """
    if tf_label in RESAMPLED_FROM_1M:
        needed_1m = max(int(min_candles) * (4 if tf_label == "4m" else 8) + 10, 200)
        sync_symbol_timeframe(symbol, "1m", config.TIMEFRAMES["1m"], min_candles=needed_1m)
        return load_history(symbol, tf_label)

    existing = load_history(symbol, tf_label)
    now = pd.Timestamp.utcnow()
    now_ms = int(now.value // 1_000_000)

    if existing.empty:
        fresh = bybit_client.fetch_klines(symbol, interval, limit=max(int(min_candles), 200))
        save_history(symbol, tf_label, fresh)
        return fresh

    last_ts = existing.index.max()
    last_ms = int(last_ts.value // 1_000_000)
    interval_ms = INTERVAL_MS.get(interval, 60_000)

    if now_ms - last_ms > interval_ms:
        gap = bybit_client.fetch_klines_range(symbol, interval, start_time_ms=last_ms + 1, end_time_ms=now_ms)
    else:
        gap = bybit_client.fetch_klines(symbol, interval, limit=3)

    if not gap.empty:
        merged = pd.concat([existing, gap]).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        save_history(symbol, tf_label, merged)
        return merged

    return existing


def latest_window(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.tail(n).copy()
