"""
EMA cross signal logic -- ported 1:1 from leverage_sweep_short_only.py,
extended to support long, short, or both directions.

Rule: a signal fires when the fast/slow EMA cross happens on the base
timeframe (1m) AND the same-direction cross is also true on the higher
timeframe (5m) at the same aligned moment, using backward-looking
merge_asof so the 5m value used is always the last *closed* 5m bar
(no lookahead).
"""

import pandas as pd


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


def compute_signals(df_1m: pd.DataFrame, fast: int, slow: int, direction: str = "both") -> pd.DataFrame:
    """
    df_1m: OHLC dataframe indexed by UTC timestamp, 1-minute bars.
    direction: "both" | "long" | "short"

    Returns df_1m with added columns: ema_fast, ema_slow, cross_up,
    cross_down, cross_up_5m, cross_down_5m, long_signal, short_signal.
    """
    m1 = add_cross(add_ema(df_1m, fast, slow))

    df_5m = resample_ohlc_right(df_1m, "5min")
    ema_f5 = df_5m["close"].ewm(span=fast, adjust=False).mean()
    ema_s5 = df_5m["close"].ewm(span=slow, adjust=False).mean()
    diff5 = ema_f5 - ema_s5
    prev5 = diff5.shift(1)

    cu5 = (prev5 <= 0) & (diff5 > 0)
    cd5 = (prev5 >= 0) & (diff5 < 0)

    out = m1.copy()
    out["cross_up_5m"] = map_htf_no_lookahead(df_1m.index, cu5.astype(float)).fillna(0).astype(bool)
    out["cross_down_5m"] = map_htf_no_lookahead(df_1m.index, cd5.astype(float)).fillna(0).astype(bool)

    out["long_signal"] = False
    out["short_signal"] = False

    if direction in ("both", "long"):
        out["long_signal"] = out["cross_up"] & out["cross_up_5m"]
    if direction in ("both", "short"):
        out["short_signal"] = out["cross_down"] & out["cross_down_5m"]

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
