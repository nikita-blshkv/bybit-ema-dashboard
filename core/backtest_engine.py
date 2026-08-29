"""
Offline backtest engine -- reuses signal_engine.compute_signals() so the
backtest logic is guaranteed identical to the live engine's signal logic.

Runs over locally stored 1m history (data/backtest/<SYMBOL>_1m.csv), lets
you override EMA fast/slow, TP/SL, direction, leverage, margin and
max_open_positions, and returns per-trade rows plus summary metrics
(winrate, profit factor, max drawdown, net PnL, trade count) -- same
metric set as leverage_sweep_short_only.py / grid_search_short_only.py.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from . import signal_engine
from . import bybit_client


def _backtest_csv_path(symbol: str) -> Path:
    return config.BACKTEST_DIR / f"{symbol}_1m.csv"


def ensure_backtest_history(symbol: str, days: int = 90, warmup_days: int = 0,
                             max_stale_minutes: int = 15) -> pd.DataFrame:
    """
    Downloads (or reuses cached) 1m history for the backtest tab.
    days=90 matches the "3 months" default; days=365 for the future
    1-year backtest mode. warmup_days extends the fetched/cached window
    further into the past so EMA(slow) has enough bars to stabilize
    before the reporting window begins.

    Gap-fill: if the cached CSV's last bar is older than
    max_stale_minutes (e.g. the computer was off overnight, or asleep),
    fetch only the missing tail range and append it, instead of either
    re-downloading everything or silently running the backtest on a
    stale/incomplete window.
    """
    total_days = days + warmup_days
    path = _backtest_csv_path(symbol)
    now = pd.Timestamp.utcnow()

    if path.exists():
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()

        span_days = (df.index.max() - df.index.min()).days if len(df) else 0
        oldest_needed = now - pd.Timedelta(days=total_days)

        needs_backfill = len(df) == 0 or df.index.min() > oldest_needed
        stale_minutes = (now - df.index.max()).total_seconds() / 60.0 if len(df) else float("inf")
        needs_gap_fill = stale_minutes > max_stale_minutes

        if not needs_backfill and not needs_gap_fill:
            return df

        frames = [df] if len(df) else []

        if needs_backfill:
            back_start = oldest_needed
            back_end = df.index.min() - pd.Timedelta(minutes=1) if len(df) else now
            if back_end > back_start:
                older = bybit_client.fetch_klines_range(
                    symbol, "1",
                    start_time_ms=int(back_start.value // 1_000_000),
                    end_time_ms=int(back_end.value // 1_000_000),
                )
                if not older.empty:
                    frames.insert(0, older)

        if needs_gap_fill and len(df):
            gap_start = df.index.max() + pd.Timedelta(minutes=1)
            gap_end = now
            newer = bybit_client.fetch_klines_range(
                symbol, "1",
                start_time_ms=int(gap_start.value // 1_000_000),
                end_time_ms=int(gap_end.value // 1_000_000),
            )
            if not newer.empty:
                frames.append(newer)
        elif needs_gap_fill and not len(df):
            pass  # handled by the full fetch below since df was empty

        if frames:
            merged = pd.concat(frames).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
            merged.to_csv(path)
            return merged

    end = now
    start = end - pd.Timedelta(days=total_days)
    df = bybit_client.fetch_klines_range(
        symbol, "1",
        start_time_ms=int(start.value // 1_000_000),
        end_time_ms=int(end.value // 1_000_000),
    )
    df.to_csv(path)
    return df


def _max_drawdown_pct(equity_curve: np.ndarray) -> float:
    if len(equity_curve) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity_curve)
    dd = np.where(peak > 0, (peak - equity_curve) / peak * 100.0, 0.0)
    return float(dd.max())


MAX_EQUITY_POINTS = 2000


def _downsample_equity_curve(rows: list, max_points: int = MAX_EQUITY_POINTS) -> list:
    """
    Evenly downsamples the equity curve across its full length instead of
    slicing only the tail (which silently dropped earlier history and
    could make the series look artificially flat). Always keeps the
    first and last point so chart start/end anchors are preserved.
    """
    n = len(rows)
    if n <= max_points:
        return rows
    step = n / max_points
    idx = sorted(set(int(i * step) for i in range(max_points)))
    if idx[-1] != n - 1:
        idx[-1] = n - 1
    if idx[0] != 0:
        idx[0] = 0
    return [rows[i] for i in idx]


_progress_lock = threading.Lock()
_backtest_progress = {"total": 0, "done": 0, "current": "", "stage": "idle"}


def get_backtest_progress():
    """Snapshot of the in-flight backtest's history-download progress, for
    the frontend to poll via GET /api/backtest_progress while the heavy
    POST /api/backtest request is still running."""
    with _progress_lock:
        return dict(_backtest_progress)


def _prefetch_history_parallel(symbols, days, warmup_days):
    """Downloads/gap-fills 1m history for every requested symbol at once
    using a thread pool, instead of one-symbol-at-a-time. This is purely
    I/O-bound (waiting on Bybit's HTTP API), so threads give a near-linear
    speedup here even with Python's GIL -- 9 symbols that each took ~60-90s
    sequentially now mostly overlap and finish in roughly the time of the
    single slowest symbol.
    """
    with _progress_lock:
        _backtest_progress["total"] = len(symbols)
        _backtest_progress["done"] = 0
        _backtest_progress["current"] = ""
        _backtest_progress["stage"] = "downloading"

    results = {}
    max_workers = min(6, max(1, len(symbols)))  # capped to be gentle with Bybit's public rate limit
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(ensure_backtest_history, sym, days, warmup_days): sym
            for sym in symbols
        }
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                results[sym] = fut.result()
            except Exception as exc:
                print(f"[backtest] history fetch failed for {sym}: {exc}")
                results[sym] = pd.DataFrame()
            with _progress_lock:
                _backtest_progress["done"] += 1
                _backtest_progress["current"] = sym

    with _progress_lock:
        _backtest_progress["stage"] = "computing"
    return results


def run_backtest(
    symbols,
    ema_fast: int,
    ema_slow: int,
    tp_pct: float,
    sl_pct: float,
    direction: str,
    initial_equity: float,
    margin_per_trade: float,
    leverage: float,
    max_open_positions: int,
    days: int = 90,
    tie_break: str = "sl_first",
):
    """
    tie_break: "sl_first" (default, conservative) | "tp_first".
    Controls which level wins when both SL and TP are touched within the
    same 1m bar.
    """
    if tie_break not in ("sl_first", "tp_first"):
        tie_break = "sl_first"
    prefer_sl_on_tie = tie_break == "sl_first"

    with _progress_lock:
        _backtest_progress["total"] = 0
        _backtest_progress["done"] = 0
        _backtest_progress["current"] = ""
        _backtest_progress["stage"] = "starting"

    # Warm-up buffer so EMA(slow) is stable before the reporting window
    # starts.
    warmup_days = max(1, int(np.ceil(ema_slow / (24 * 60))) + 2)

    market_data = {}
    signal_rows = []
    report_start_ts = None

    prefetched = _prefetch_history_parallel(symbols, days, warmup_days)

    for symbol in symbols:
        df_1m = prefetched.get(symbol)
        if df_1m is None or df_1m.empty or len(df_1m) < ema_slow + 10:
            continue

        # Strategy runs on 4m base candles (Heikin-Ashi), confirmed on 8m --
        # resample the raw cached 1m history up to 4m here so the backtest
        # uses the exact same granularity as the live engine.
        df_base = signal_engine.resample_ohlc_right(df_1m, "4min")
        if df_base.empty or len(df_base) < ema_slow + 10:
            continue

        cutoff = df_base.index.max() - pd.Timedelta(days=days)
        report_start_ts = cutoff if report_start_ts is None else max(report_start_ts, cutoff)

        with_signals = signal_engine.compute_signals(df_base, ema_fast, ema_slow, direction)
        market_data[symbol] = with_signals[["open", "high", "low", "close"]].copy()

        if direction in ("both", "long"):
            longs = with_signals.index[with_signals["long_signal"].fillna(False) & (with_signals.index >= cutoff)]
            signal_rows.extend(
                {"time": ts, "symbol": symbol, "direction": "long",
                 "entry_price": float(with_signals.at[ts, "close"])}
                for ts in longs
            )
        if direction in ("both", "short"):
            shorts = with_signals.index[with_signals["short_signal"].fillna(False) & (with_signals.index >= cutoff)]
            signal_rows.extend(
                {"time": ts, "symbol": symbol, "direction": "short",
                 "entry_price": float(with_signals.at[ts, "close"])}
                for ts in shorts
            )

    if not market_data:
        return {"trades": [], "summary": _empty_summary(initial_equity), "equity_curve": []}

    signal_table = pd.DataFrame(signal_rows)
    if signal_table.empty:
        return {"trades": [], "summary": _empty_summary(initial_equity), "equity_curve": []}

    signal_table = signal_table.sort_values(["time", "symbol", "direction"]).reset_index(drop=True)
    signal_grouped = {ts: g for ts, g in signal_table.groupby("time")}

    all_times = pd.Index(sorted(set().union(*[
        df.index[df.index >= report_start_ts] for df in market_data.values()
    ])))

    open_positions = []
    equity = initial_equity
    peak_equity = initial_equity
    max_dd = 0.0
    trade_rows = []
    equity_curve = []

    for ts in all_times:
        still_open = []
        for pos in open_positions:
            df = market_data[pos["symbol"]]
            if ts not in df.index:
                still_open.append(pos)
                continue

            row = df.loc[ts]
            entry_price = pos["entry_price"]

            if pos["direction"] == "long":
                stop_price = entry_price * (1.0 - sl_pct / 100.0)
                take_price = entry_price * (1.0 + tp_pct / 100.0)
                hit_sl = row["low"] <= stop_price
                hit_tp = row["high"] >= take_price
                if hit_sl and hit_tp:
                    exit_is_sl = prefer_sl_on_tie
                else:
                    exit_is_sl = hit_sl
                exit_price = stop_price if exit_is_sl else take_price
                pnl_pct = (exit_price / entry_price) - 1.0
            else:
                stop_price = entry_price * (1.0 + sl_pct / 100.0)
                take_price = entry_price * (1.0 - tp_pct / 100.0)
                hit_sl = row["high"] >= stop_price
                hit_tp = row["low"] <= take_price
                if hit_sl and hit_tp:
                    exit_is_sl = prefer_sl_on_tie
                else:
                    exit_is_sl = hit_sl
                exit_price = stop_price if exit_is_sl else take_price
                pnl_pct = 1.0 - (exit_price / entry_price)

            if hit_sl or hit_tp:
                pnl_usdt = pos["notional"] * pnl_pct
                equity += pnl_usdt
                trade_rows.append({
                    "id": pos["id"],
                    "symbol": pos["symbol"],
                    "direction": pos["direction"],
                    "entry_time": pos["entry_time"].isoformat(),
                    "exit_time": ts.isoformat(),
                    "entry_price": entry_price,
                    "exit_price": float(exit_price),
                    "exit_reason": "SL" if exit_is_sl else "TP",
                    "pnl_pct": pnl_pct * 100.0,
                    "pnl_usdt": pnl_usdt,
                })
            else:
                still_open.append(pos)

        open_positions = still_open

        if ts in signal_grouped:
            for _, sig in signal_grouped[ts].iterrows():
                if len(open_positions) >= max_open_positions:
                    continue
                open_positions.append({
                    "id": str(uuid.uuid4()),
                    "symbol": sig["symbol"],
                    "direction": sig["direction"],
                    "entry_time": ts,
                    "entry_price": float(sig["entry_price"]),
                    "notional": margin_per_trade * leverage,
                })

        peak_equity = max(peak_equity, equity)
        dd_pct = ((peak_equity - equity) / peak_equity) * 100 if peak_equity > 0 else 0.0
        max_dd = max(max_dd, dd_pct)
        equity_curve.append({"time": ts.isoformat(), "equity": equity})

    trades_df = pd.DataFrame(trade_rows)
    summary = _summarize(trades_df, initial_equity, equity, max_dd)
    summary["data_start"] = all_times[0].isoformat() if len(all_times) else None
    summary["data_end"] = all_times[-1].isoformat() if len(all_times) else None
    summary["tie_break"] = tie_break

    return {
        "trades": trade_rows,
        "summary": summary,
        "equity_curve": _downsample_equity_curve(equity_curve),
    }


def backtest_chart_data(symbol: str, ema_fast: int, ema_slow: int, direction: str,
                         tp_pct: float, sl_pct: float, days: int = 90,
                         tie_break: str = "sl_first"):
    """
    Returns candles (4m/8m/1h) + EMA series for the price-chart tab:
      - 4m candles with ema_fast / ema_slow (Heikin-Ashi based)
      - 8m candles with ema_fast_8m / ema_slow_8m confirmation series
      - 1h candles (no EMA overlay, context only)
      - the full list of simulated trades (entry/exit time+price+reason)
        for this symbol, so the frontend can draw markers on any
        timeframe and let the user scroll back 50 candles around any
        trade to verify it actually started/closed where the strategy
        logic says it should.

    Re-runs the same signal_engine.compute_signals() + TP/SL simulation
    as run_backtest() but scoped to a single symbol, so entries/exits
    are guaranteed identical to what the Backtest tab reports.
    """
    if tie_break not in ("sl_first", "tp_first"):
        tie_break = "sl_first"
    prefer_sl_on_tie = tie_break == "sl_first"

    warmup_days = max(1, int(np.ceil(ema_slow / (24 * 60))) + 2)
    df_1m = ensure_backtest_history(symbol, days=days, warmup_days=warmup_days)
    df_base = signal_engine.resample_ohlc_right(df_1m, "4min") if not df_1m.empty else df_1m
    if df_base.empty or len(df_base) < ema_slow + 10:
        return {
            "symbol": symbol, "candles_4m": [], "candles_8m": [], "candles_1h": [],
            "trades": [], "data_start": None, "data_end": None,
        }

    cutoff = df_base.index.max() - pd.Timedelta(days=days)

    with_signals = signal_engine.compute_signals(df_base, ema_fast, ema_slow, direction)

    df_8m_raw = signal_engine.resample_ohlc_right(df_base, "8min")
    ha_8m = signal_engine.to_heikin_ashi(df_8m_raw)
    ema_f5 = ha_8m["close"].ewm(span=ema_fast, adjust=False).mean()
    ema_s5 = ha_8m["close"].ewm(span=ema_slow, adjust=False).mean()
    df_5m = df_8m_raw  # keep raw (non-HA) OHLC for chart candle bodies

    df_1h = signal_engine.resample_ohlc_right(df_base, "1h")

    # Project the 5m EMAs onto the 1m timeline the same no-lookahead way
    # signal_engine uses for cross detection, so the "4 moving averages"
    # shown on the 1m chart (ema_fast_1m, ema_slow_1m, ema_fast_8m,
    # ema_slow_8m) are exactly the values the strategy itself used to
    # decide entries -- not an approximation.
    ema_f8_on_base = signal_engine.map_htf_no_lookahead(with_signals.index, ema_f5)
    ema_s8_on_base = signal_engine.map_htf_no_lookahead(with_signals.index, ema_s5)

    view_base = with_signals[with_signals.index >= cutoff]
    view_8m = df_5m[df_5m.index >= cutoff]
    view_1h = df_1h[df_1h.index >= cutoff]
    ema_f5_view = ema_f5[ema_f5.index >= cutoff]
    ema_s5_view = ema_s5[ema_s5.index >= cutoff]
    ema_f8_on_base_view = ema_f8_on_base[ema_f8_on_base.index >= cutoff]
    ema_s8_on_base_view = ema_s8_on_base[ema_s8_on_base.index >= cutoff]

    candles_4m = [
        {
            "time": ts.isoformat(),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "ema_fast": float(r["ema_fast"]) if pd.notna(r["ema_fast"]) else None,
            "ema_slow": float(r["ema_slow"]) if pd.notna(r["ema_slow"]) else None,
            "ema_fast_8m": float(ema_f8_on_base_view.loc[ts]) if ts in ema_f8_on_base_view.index and pd.notna(ema_f8_on_base_view.loc[ts]) else None,
            "ema_slow_8m": float(ema_s8_on_base_view.loc[ts]) if ts in ema_s8_on_base_view.index and pd.notna(ema_s8_on_base_view.loc[ts]) else None,
        }
        for ts, r in view_base.iterrows()
    ]

    candles_8m = [
        {
            "time": ts.isoformat(),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "ema_fast": float(ema_f5_view.loc[ts]) if ts in ema_f5_view.index and pd.notna(ema_f5_view.loc[ts]) else None,
            "ema_slow": float(ema_s5_view.loc[ts]) if ts in ema_s5_view.index and pd.notna(ema_s5_view.loc[ts]) else None,
        }
        for ts, r in view_8m.iterrows()
    ]

    candles_1h = [
        {
            "time": ts.isoformat(),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
        }
        for ts, r in view_1h.iterrows()
    ]

    # --- simulate trades for this symbol only, same logic as run_backtest ---
    signal_rows = []
    if direction in ("both", "long"):
        longs = with_signals.index[with_signals["long_signal"].fillna(False) & (with_signals.index >= cutoff)]
        signal_rows.extend(
            {"time": ts, "direction": "long", "entry_price": float(with_signals.at[ts, "close"])}
            for ts in longs
        )
    if direction in ("both", "short"):
        shorts = with_signals.index[with_signals["short_signal"].fillna(False) & (with_signals.index >= cutoff)]
        signal_rows.extend(
            {"time": ts, "direction": "short", "entry_price": float(with_signals.at[ts, "close"])}
            for ts in shorts
        )

    trade_rows = []
    if signal_rows:
        signal_rows.sort(key=lambda r: r["time"])
        signal_by_ts = {}
        for r in signal_rows:
            signal_by_ts.setdefault(r["time"], []).append(r)

        market = with_signals[["open", "high", "low", "close"]]
        open_positions = []

        for ts in view_base.index:
            still_open = []
            row = market.loc[ts]
            for pos in open_positions:
                entry_price = pos["entry_price"]
                if pos["direction"] == "long":
                    stop_price = entry_price * (1.0 - sl_pct / 100.0)
                    take_price = entry_price * (1.0 + tp_pct / 100.0)
                    hit_sl = row["low"] <= stop_price
                    hit_tp = row["high"] >= take_price
                    exit_is_sl = prefer_sl_on_tie if (hit_sl and hit_tp) else hit_sl
                    exit_price = stop_price if exit_is_sl else take_price
                else:
                    stop_price = entry_price * (1.0 + sl_pct / 100.0)
                    take_price = entry_price * (1.0 - tp_pct / 100.0)
                    hit_sl = row["high"] >= stop_price
                    hit_tp = row["low"] <= take_price
                    exit_is_sl = prefer_sl_on_tie if (hit_sl and hit_tp) else hit_sl
                    exit_price = stop_price if exit_is_sl else take_price

                if hit_sl or hit_tp:
                    if pos["direction"] == "long":
                        pnl_pct = (exit_price / entry_price) - 1.0
                    else:
                        pnl_pct = 1.0 - (exit_price / entry_price)
                    trade_rows.append({
                        "direction": pos["direction"],
                        "entry_time": pos["entry_time"].isoformat(),
                        "exit_time": ts.isoformat(),
                        "entry_price": entry_price,
                        "exit_price": float(exit_price),
                        "exit_reason": "SL" if exit_is_sl else "TP",
                        "stop_price": float(stop_price),
                        "take_price": float(take_price),
                        "pnl_pct": pnl_pct * 100.0,
                    })
                else:
                    still_open.append(pos)
            open_positions = still_open

            for sig in signal_by_ts.get(ts, []):
                open_positions.append({
                    "direction": sig["direction"],
                    "entry_time": ts,
                    "entry_price": sig["entry_price"],
                })

    return {
        "symbol": symbol,
        "candles_4m": candles_4m,
        "candles_8m": candles_8m,
        "candles_1h": candles_1h,
        "trades": trade_rows,
        "data_start": view_base.index[0].isoformat() if len(view_base) else None,
        "data_end": view_base.index[-1].isoformat() if len(view_base) else None,
    }


def _empty_summary(initial_equity):
    return {
        "closed_trades": 0, "wins": 0, "losses": 0, "winrate_pct": 0.0,
        "net_pnl_usdt": 0.0, "return_pct": 0.0, "profit_factor": 0.0,
        "max_drawdown_pct": 0.0, "final_equity": initial_equity,
    }


def _summarize(trades_df, initial_equity, final_equity, max_dd):
    if trades_df.empty:
        return _empty_summary(initial_equity)

    wins = int((trades_df["pnl_usdt"] > 0).sum())
    losses = int((trades_df["pnl_usdt"] < 0).sum())
    gross_win = float(trades_df.loc[trades_df["pnl_usdt"] > 0, "pnl_usdt"].sum())
    gross_loss = float(-trades_df.loc[trades_df["pnl_usdt"] < 0, "pnl_usdt"].sum())

    return {
        "closed_trades": int(len(trades_df)),
        "wins": wins,
        "losses": losses,
        "winrate_pct": (wins / len(trades_df)) * 100.0,
        "net_pnl_usdt": float(final_equity - initial_equity),
        "return_pct": ((final_equity / initial_equity) - 1.0) * 100.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_drawdown_pct": float(max_dd),
        "final_equity": float(final_equity),
    }
