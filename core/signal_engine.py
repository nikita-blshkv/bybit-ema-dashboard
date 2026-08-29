"""
EMA cross signal logic -- ported 1:1 from leverage_sweep_short_only.py,
extended to support long, short, or both directions.

Rule (updated Aug 2026 per 90-day sweep): a signal fires when the fast/
slow EMA cross happens on HEIKIN-ASHI 4m base candles AND the same-
direction cross is also true on HEIKIN-ASHI 8m candles at the same
aligned moment, using backward-looking merge_asof so the 8m value used
is always the last *closed* 8m bar (no lookahead). Heikin-Ashi
smoothing is applied on top of the raw OHLC before computing EMAs, so
noise from a single wick cannot trigger a false cross.
"""

import pandas as pd


def to_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a raw OHLC dataframe into Heikin-Ashi candles.

    ha_close = (open+high+low+close)/4
    ha_open  = (prev_ha_open + prev_ha_close) / 2  (first bar: (open+close)/2)
    ha_high  = max(high, ha_open, ha_close)
    ha_low   = min(low, ha_open, ha_close)

    Only open/high/low/close are recomputed; index and any extra columns
    (e.g. volume) are preserved as-is.
    """
    out = df.copy()
    ha_close = (out["open"] + out["high"] + out["low"] + out["close"]) / 4.0

    ha_open = pd.Series(index=out.index, dtype="float64")
    if len(out) > 0:
        ha_open.iloc[0] = (out["open"].iloc[0] + out["close"].iloc[0]) / 2.0
        prev_open = ha_open.iloc[0]
        prev_close = ha_close.iloc[0]
        for i in range(1, len(out)):
            v = (prev_open + prev_close) / 2.0
            ha_open.iloc[i] = v
            prev_open = v
            prev_close = ha_close.iloc[i]

    ha_high = pd.concat([out["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([out["low"], ha_open, ha_close], axis=1).min(axis=1)

    out["open"] = ha_open
    out["high"] = ha_high
    out["low"] = ha_low
    out["close"] = ha_close
    return out


def add_ema(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=slow, adjust=False).mean()
    return out


def add_cross(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    diff = out["ema_fast"] - out["ema_slow"]
    prev = diff.shift(1)
    out["cross_up"] = (prev <= 0) & (diff > 0)
    out["cross_down"] = (prev >= 0) & (diff < 0)
    return out


def resample_ohlc_right(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule, label="right", closed="right").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum" if "volume" in df.columns else "first",
    }).dropna()


def map_htf_no_lookahead(base_index: pd.DatetimeIndex, htf_series: pd.Series) -> pd.Series:
    htf_df = htf_series.rename("val").reset_index()
    htf_df.columns = ["htf_time", "val"]

    base = pd.DataFrame({"base_time": base_index})

    merged = pd.merge_asof(
        base.sort_values("base_time"),
        htf_df.sort_values("htf_time"),
        left_on="base_time",
        right_on="htf_time",
        direction="backward",
    )
    return merged.set_index("base_time")["val"].reindex(base_index)


def compute_signals(df_base: pd.DataFrame, fast: int, slow: int, direction: str = "both") -> pd.DataFrame:
    """
    df_base: raw OHLC dataframe indexed by UTC timestamp, 4-minute bars
    (the caller is responsible for syncing 4m candle history).
    direction: "both" | "long" | "short"

    Internally converts both the 4m base series and a resampled 8m
    series to Heikin-Ashi candles before computing EMAs/crosses, per
    the 90-day sweep that found HA 4m+8m confirmation outperforms the
    old raw-OHLC 1m+5m setup.

    Returns df_base with added columns: ema_fast, ema_slow, cross_up,
    cross_down, cross_up_8m, cross_down_8m, long_signal, short_signal.
    """
    ha_base = to_heikin_ashi(df_base)
    ha_signals = add_cross(add_ema(ha_base, fast, slow))

    df_8m_raw = resample_ohlc_right(df_base, "8min")
    ha_8m = to_heikin_ashi(df_8m_raw)
    ema_f8 = ha_8m["close"].ewm(span=fast, adjust=False).mean()
    ema_s8 = ha_8m["close"].ewm(span=slow, adjust=False).mean()
    diff8 = ema_f8 - ema_s8
    prev8 = diff8.shift(1)

    cu8 = (prev8 <= 0) & (diff8 > 0)
    cd8 = (prev8 >= 0) & (diff8 < 0)

    # IMPORTANT: keep the REAL (non-Heikin-Ashi) open/high/low/close from
    # df_base for trade simulation and entry/exit pricing. Heikin-Ashi
    # values are smoothed/synthetic and must only drive the EMA cross
    # decision (ema_fast, ema_slow, cross_up, cross_down columns below),
    # never the actual traded price -- otherwise every backtest and every
    # live paper fill would be priced off a fictitious smoothed candle
    # instead of the real market price.
    out = df_base.copy()
    out["ema_fast"] = ha_signals["ema_fast"]
    out["ema_slow"] = ha_signals["ema_slow"]
    out["cross_up"] = ha_signals["cross_up"]
    out["cross_down"] = ha_signals["cross_down"]
    out["cross_up_8m"] = map_htf_no_lookahead(df_base.index, cu8.astype(float)).fillna(0).astype(bool)
    out["cross_down_8m"] = map_htf_no_lookahead(df_base.index, cd8.astype(float)).fillna(0).astype(bool)

    out["long_signal"] = False
    out["short_signal"] = False

    if direction in ("both", "long"):
        out["long_signal"] = out["cross_up"] & out["cross_up_8m"]
    if direction in ("both", "short"):
        out["short_signal"] = out["cross_down"] & out["cross_down_8m"]

    return out


def latest_signal(df_with_signals: pd.DataFrame):
    """
    Returns (timestamp, direction, price) for the most recent CLOSED bar
    signal, or (None, None, None) if the last closed bar has no signal.
    Assumes the last row in df_with_signals is the most recently closed bar.
    """
    if df_with_signals.empty:
        return None, None, None

    last = df_with_signals.iloc[-1]
    ts = df_with_signals.index[-1]

    if bool(last.get("long_signal", False)):
        return ts, "long", float(last["close"])
    if bool(last.get("short_signal", False)):
        return ts, "short", float(last["close"])
    return None, None, None
