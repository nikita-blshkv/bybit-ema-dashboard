"""
Minimal public Bybit v5 REST client for kline (candle) data.

No API key required for market data endpoints. This module only reads
public data -- it never places orders.
"""

import time
import requests
import pandas as pd

from . import config


def fetch_klines(symbol: str, interval: str, limit: int = 200, end_time_ms: int = None) -> pd.DataFrame:
    """
    Fetch up to `limit` klines from Bybit v5 public market endpoint.

    interval: Bybit interval string, e.g. "1", "5", "60"
    end_time_ms: optional upper bound (ms since epoch) for pagination
    """
    url = f"{config.BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": config.BYBIT_CATEGORY,
        "symbol": symbol,
        "interval": interval,
        "limit": min(limit, 1000),
    }
    if end_time_ms is not None:
        params["end"] = int(end_time_ms)

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit error for {symbol} {interval}: {payload.get('retMsg')}")

    rows = payload["result"]["list"]
    # Bybit returns newest-first: [start, open, high, low, close, volume, turnover]
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["start", "open", "high", "low", "close", "volume", "turnover"])
    df["start"] = pd.to_datetime(pd.to_numeric(df["start"]), unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.set_index("start").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def fetch_klines_range(symbol: str, interval: str, start_time_ms: int, end_time_ms: int) -> pd.DataFrame:
    """
    Fetch all klines between start_time_ms and end_time_ms (inclusive-ish),
    paginating backward from end_time_ms in chunks of 1000.
    Used for backfilling missing history and for backtest downloads.
    """
    all_frames = []
    cursor_end = end_time_ms

    while True:
        chunk = fetch_klines(symbol, interval, limit=1000, end_time_ms=cursor_end)
        if chunk.empty:
            break

        all_frames.append(chunk)
        earliest = int(chunk.index.min().value // 1_000_000)

        if earliest <= start_time_ms:
            break

        cursor_end = earliest - 1
        time.sleep(0.15)  # be gentle with the public rate limit

    if not all_frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    out = pd.concat(all_frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out = out[(out.index >= pd.to_datetime(start_time_ms, unit="ms", utc=True)) &
              (out.index <= pd.to_datetime(end_time_ms, unit="ms", utc=True))]
    return out
