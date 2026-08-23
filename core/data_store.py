"""
Local candle history storage per symbol/timeframe.

On startup, loads existing CSV history (if any) and backfills only the
missing candles since the last stored timestamp, up to "now". This keeps
you from re-downloading everything every time you start the engine.
"""

import pandas as pd
from pathlib import Path

from . import config
from . import bybit_client


def _csv_path(symbol: str, tf_label: str) -> Path:
    return config.DATA_DIR / f"{symbol}_{tf_label}.csv"


def load_history(symbol: str, tf_label: str) -> pd.DataFrame:
    path = _csv_path(symbol, tf_label)
    if not path.exists():
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def save_history(symbol: str, tf_label: str, df: pd.DataFrame) -> None:
    path = _csv_path(symbol, tf_label)
    df.to_csv(path)


def sync_symbol_timeframe(symbol: str, tf_label: str, interval: str, min_candles: int = 50) -> pd.DataFrame:
    """
    Ensures local history has at least `min_candles` recent candles and is
    caught up to the latest closed candle. Returns the full local dataframe
    (not truncated) so older history keeps accumulating on disk.
    """
    existing = load_history(symbol, tf_label)

    now = pd.Timestamp.utcnow()
    now_ms = int(now.value // 1_000_000)

    if existing.empty:
        fresh = bybit_client.fetch_klines(symbol, interval, limit=max(min_candles, 200))
        save_history(symbol, tf_label, fresh)
        return fresh

    last_ts = existing.index.max()
    last_ms = int(last_ts.value // 1_000_000)

    interval_ms = {
        "1": 60_000,
        "5": 5 * 60_000,
        "60": 60 * 60_000,
    }.get(interval, 60_000)

    if now_ms - last_ms > interval_ms:
        # We are missing more than one bar's worth of time (e.g. the app
        # was closed for a while) -- do a real backfill of the gap.
        gap = bybit_client.fetch_klines_range(symbol, interval, start_time_ms=last_ms + 1, end_time_ms=now_ms)
    else:
        # Steady state (polling every few seconds): cheaply re-fetch just
        # the last couple of candles with a single lightweight request
        # (no pagination) so the CURRENTLY FORMING candle's high/low/close
        # update live every poll instead of freezing until it closes.
        gap = bybit_client.fetch_klines(symbol, interval, limit=3)

    if not gap.empty:
        merged = pd.concat([existing, gap]).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        save_history(symbol, tf_label, merged)
        return merged

    return existing


def latest_window(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.tail(n).copy()
