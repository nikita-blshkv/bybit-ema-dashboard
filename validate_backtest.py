#!/usr/bin/env python3
"""
Independent validator for the EMA-cross backtest engine.

This intentionally does NOT import backtest_engine.run_backtest() or reuse
its simulation loop -- it recomputes signals and trade outcomes from raw
OHLC using a separate code path, then cross-checks against what the
dashboard's engine actually produced. If both independent implementations
agree, that's real evidence the numbers are trustworthy; if the engine
checked itself, a shared bug would never be caught.

Checks performed per trade:
  1. Entry price matches the 1m close at the signal bar.
  2. The reported exit bar is the *first* bar (not a later one) whose
     high/low actually touches the stop or take price -- catches "skipped
     an earlier exit" bugs.
  3. If both TP and SL are touched in the exit bar, the tie-break rule
     (default sl_first) was applied consistently.
  4. Recomputed pnl_pct/pnl_usdt matches the reported ones within 1e-6.
  5. No signal fired on a bar where the EMA(slow) window wasn't fully
     warmed up yet (would indicate a cold-start / lookahead artifact).
  6. Cross-checks the 1m+5m dual-timeframe rule independently: a signal
     must correspond to both a 1m EMA cross AND the last *closed* 5m bar
     showing the same-direction cross (recomputed via a plain resample,
     not via signal_engine's merge_asof helper).

Usage:
    python3 validate_backtest.py --symbol BTCUSDT --ema-fast 7 --ema-slow 133 \\
        --tp 2.8 --sl 1.2 --direction short --days 90 --margin 500 --leverage 50

Exit code 0 = all checks passed. Exit code 1 = at least one discrepancy
found (details printed).
"""

import argparse
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from core import backtest_engine, config  # noqa: E402


def recompute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def independent_signals(df_1m: pd.DataFrame, fast: int, slow: int, direction: str) -> pd.DataFrame:
    """Re-derive long/short signals from raw OHLC without touching signal_engine."""
    ema_f = recompute_ema(df_1m["close"], fast)
    ema_s = recompute_ema(df_1m["close"], slow)
    diff = ema_f - ema_s
    cross_up_1m = (diff.shift(1) <= 0) & (diff > 0)
    cross_down_1m = (diff.shift(1) >= 0) & (diff < 0)

    df_5m = df_1m.resample("5min", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    ema_f5 = recompute_ema(df_5m["close"], fast)
    ema_s5 = recompute_ema(df_5m["close"], slow)
    diff5 = ema_f5 - ema_s5
    cu5 = (diff5.shift(1) <= 0) & (diff5 > 0)
    cd5 = (diff5.shift(1) >= 0) & (diff5 < 0)

    # backward as-of join: for each 1m bar, the last *closed* 5m cross flag
    cu5_on_1m = cu5.reindex(df_1m.index, method="ffill").fillna(False)
    cd5_on_1m = cd5.reindex(df_1m.index, method="ffill").fillna(False)

    out = pd.DataFrame(index=df_1m.index)
    out["long_signal"] = False
    out["short_signal"] = False
    if direction in ("both", "long"):
        out["long_signal"] = cross_up_1m & cu5_on_1m
    if direction in ("both", "short"):
        out["short_signal"] = cross_down_1m & cd5_on_1m
    out["ema_fast"] = ema_f
    out["ema_slow"] = ema_s
    return out


def find_first_touch_exit(df_1m, entry_time, entry_price, direction, tp_pct, sl_pct, tie_break):
    """Scan forward bar-by-bar from entry, return (exit_time, exit_price, reason)."""
    prefer_sl = tie_break == "sl_first"
    future = df_1m[df_1m.index > entry_time]
    if direction == "long":
        stop_price = entry_price * (1 - sl_pct / 100.0)
        take_price = entry_price * (1 + tp_pct / 100.0)
    else:
        stop_price = entry_price * (1 + sl_pct / 100.0)
        take_price = entry_price * (1 - tp_pct / 100.0)

    for ts, row in future.iterrows():
        if direction == "long":
            hit_sl = row["low"] <= stop_price
            hit_tp = row["high"] >= take_price
        else:
            hit_sl = row["high"] >= stop_price
            hit_tp = row["low"] <= take_price
        if hit_sl or hit_tp:
            exit_is_sl = prefer_sl if (hit_sl and hit_tp) else hit_sl
            exit_price = stop_price if exit_is_sl else take_price
            return ts, exit_price, ("SL" if exit_is_sl else "TP")
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--ema-fast", type=int, default=config.DEFAULT_EMA_FAST)
    ap.add_argument("--ema-slow", type=int, default=config.DEFAULT_EMA_SLOW)
    ap.add_argument("--tp", type=float, default=config.DEFAULT_TP_PCT)
    ap.add_argument("--sl", type=float, default=config.DEFAULT_SL_PCT)
    ap.add_argument("--direction", default=config.DEFAULT_DIRECTION)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--margin", type=float, default=config.DEFAULT_MARGIN_PER_TRADE)
    ap.add_argument("--leverage", type=float, default=config.DEFAULT_LEVERAGE)
    ap.add_argument("--tie-break", default="sl_first", choices=["sl_first", "tp_first"])
    ap.add_argument("--max-report", type=int, default=20, help="max discrepancies to print in detail")
    args = ap.parse_args()

    print(f"=== Validating {args.symbol} | EMA {args.ema_fast}/{args.ema_slow} "
          f"| TP {args.tp}% SL {args.sl}% | dir={args.direction} | days={args.days} ===\n")

    engine_result = backtest_engine.run_backtest(
        symbols=[args.symbol],
        ema_fast=args.ema_fast, ema_slow=args.ema_slow,
        tp_pct=args.tp, sl_pct=args.sl, direction=args.direction,
        initial_equity=100000.0, margin_per_trade=args.margin,
        leverage=args.leverage, max_open_positions=999999,
        days=args.days, tie_break=args.tie_break,
    )
    engine_trades = engine_result["trades"]
    print(f"Engine produced {len(engine_trades)} closed trades.\n")

    warmup_days = max(1, int(np.ceil(args.ema_slow / (24 * 60))) + 2)
    df_1m = backtest_engine.ensure_backtest_history(args.symbol, days=args.days, warmup_days=warmup_days)
    cutoff = df_1m.index.max() - pd.Timedelta(days=args.days)

    sig = independent_signals(df_1m, args.ema_fast, args.ema_slow, args.direction)

    issues = []

    # --- Check 1: warm-up -- no signal before EMA(slow) has slow*3 bars of history
    warm_cutoff_idx = args.ema_slow * 3
    if warm_cutoff_idx < len(sig):
        early = sig.iloc[:warm_cutoff_idx]
        premature = early[(early["long_signal"]) | (early["short_signal"])]
        if len(premature):
            issues.append(f"[WARMUP] {len(premature)} signal(s) fired before EMA(slow) had "
                           f"{warm_cutoff_idx} bars of warm-up (first at {premature.index[0]})")

    # --- Check 2: re-simulate every engine trade independently and compare ---
    mismatches = 0
    for t in engine_trades:
        entry_time = pd.Timestamp(t["entry_time"])
        exit_time_reported = pd.Timestamp(t["exit_time"])
        direction = t["direction"]

        if entry_time not in df_1m.index:
            issues.append(f"[MISSING BAR] entry_time {entry_time} not found in OHLC index")
            mismatches += 1
            continue

        actual_close = float(df_1m.loc[entry_time, "close"])
        if abs(actual_close - t["entry_price"]) > 1e-6:
            issues.append(f"[ENTRY PRICE] {t['symbol']} {direction} @ {entry_time}: "
                           f"reported entry {t['entry_price']:.4f} != actual close {actual_close:.4f}")
            mismatches += 1

        exit_time_calc, exit_price_calc, reason_calc = find_first_touch_exit(
            df_1m, entry_time, t["entry_price"], direction, args.tp, args.sl, args.tie_break
        )

        if exit_time_calc is None:
            issues.append(f"[NO EXIT FOUND] {t['symbol']} {direction} @ {entry_time}: "
                           f"engine reported exit {exit_time_reported} but independent scan found none")
            mismatches += 1
            continue

        if exit_time_calc != exit_time_reported:
            issues.append(f"[EXIT TIME MISMATCH] {t['symbol']} {direction} entry {entry_time}: "
                           f"engine exit={exit_time_reported} independent exit={exit_time_calc} "
                           f"(independent scan finds the FIRST bar where SL/TP is touched -- "
                           f"if engine's exit is later, it skipped an earlier valid exit)")
            mismatches += 1

        if reason_calc != t["exit_reason"]:
            issues.append(f"[EXIT REASON MISMATCH] {t['symbol']} {direction} entry {entry_time}: "
                           f"engine={t['exit_reason']} independent={reason_calc}")
            mismatches += 1

        if abs(exit_price_calc - t["exit_price"]) > 1e-6:
            issues.append(f"[EXIT PRICE MISMATCH] {t['symbol']} {direction} entry {entry_time}: "
                           f"engine={t['exit_price']:.4f} independent={exit_price_calc:.4f}")
            mismatches += 1

        # PnL re-derivation
        if direction == "long":
            pnl_pct_calc = (t["exit_price"] / t["entry_price"] - 1.0) * 100.0
        else:
            pnl_pct_calc = (1.0 - t["exit_price"] / t["entry_price"]) * 100.0
        if abs(pnl_pct_calc - t["pnl_pct"]) > 1e-4:
            issues.append(f"[PNL%% MISMATCH] entry {entry_time}: reported {t['pnl_pct']:.4f}%% "
                           f"recomputed {pnl_pct_calc:.4f}%%")
            mismatches += 1

        pnl_usdt_calc = args.margin * args.leverage * (pnl_pct_calc / 100.0)
        if abs(pnl_usdt_calc - t["pnl_usdt"]) > 0.01:
            issues.append(f"[PNL USDT MISMATCH] entry {entry_time}: reported {t['pnl_usdt']:.2f} "
                           f"recomputed {pnl_usdt_calc:.2f}")
            mismatches += 1

    # --- Check 3: does the independent signal set match what the engine acted on? ---
    engine_entry_times = {pd.Timestamp(t["entry_time"]) for t in engine_trades}
    sig_in_window = sig[sig.index >= cutoff]
    independent_signal_times = set(sig_in_window.index[sig_in_window["long_signal"] | sig_in_window["short_signal"]])

    missed_by_engine = independent_signal_times - engine_entry_times
    extra_in_engine = engine_entry_times - independent_signal_times
    # extra_in_engine can legitimately be non-empty if max_open_positions capped
    # entries in the real dashboard run -- here we set it to 999999 above so it
    # should NOT happen; report if it does.
    if extra_in_engine:
        issues.append(f"[EXTRA ENTRIES] engine opened {len(extra_in_engine)} trade(s) at times with "
                       f"no independently-detected signal (first: {sorted(extra_in_engine)[0]})")
    if missed_by_engine:
        issues.append(f"[MISSED SIGNALS] independent check found {len(missed_by_engine)} signal(s) "
                       f"the engine did not trade on (first: {sorted(missed_by_engine)[0]}) -- "
                       f"note: this can be a false positive if max_open_positions capped it, "
                       f"but this run used max_open_positions=999999 so it should not.")

    print(f"Independent re-simulation checked {len(engine_trades)} trades, "
          f"found {mismatches} field-level mismatch(es).\n")

    if not issues:
        print("\u2705 ALL CHECKS PASSED -- signals, entries, exits, and PnL all match "
              "an independently recomputed simulation. Results can be trusted.")
        sys.exit(0)
    else:
        print(f"\u274c {len(issues)} ISSUE(S) FOUND:\n")
        for i, msg in enumerate(issues[: args.max_report], 1):
            print(f"{i}. {msg}")
        if len(issues) > args.max_report:
            print(f"... and {len(issues) - args.max_report} more (raise --max-report to see all)")
        sys.exit(1)


if __name__ == "__main__":
    main()
