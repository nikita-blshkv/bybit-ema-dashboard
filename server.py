#!/usr/bin/env python3
"""
Flask server for the EMA cross strategy dashboard.

Run:
    python3 server.py

Then open http://127.0.0.1:5000 in Chrome.

Endpoints:
    GET  /                          -> dashboard HTML
    GET  /api/status                -> engine running state + params
    POST /api/start                 -> start the live engine (new positions allowed)
    POST /api/stop                  -> stop the live engine (open trades still resolve)
    POST /api/params                -> update EMA/TP/SL/leverage/etc params
    GET  /api/candles?symbol=&tf=   -> latest N candles for chart
    GET  /api/open_positions        -> current open paper positions
    GET  /api/trade_log             -> closed trade journal
    GET  /api/equity_curve          -> equity curve points
    POST /api/backtest              -> run a backtest with given params, returns trades+summary
"""

import threading
import os
from flask import Flask, jsonify, request, send_from_directory

from core import config
from core import data_store
from core import bybit_client
from core import live_engine
from core import trade_journal
from core import backtest_engine
from core import bybit_trade_client
from core.bybit_trade_client import BybitAuthError, BybitApiError

app = Flask(__name__, static_folder="dashboard", static_url_path="")


@app.route("/")
def index():
    return send_from_directory("dashboard", "index.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    state = live_engine.load_engine_state()
    state = dict(state)
    fee_totals = trade_journal.get_fee_totals()
    state["fee_gross_usdt"] = fee_totals["fee_gross_usdt"]
    state["rebate_usdt"] = fee_totals["rebate_usdt"]
    state["fee_net_usdt"] = fee_totals["fee_net_usdt"]
    state["net_pnl_after_fees_usdt"] = trade_journal.get_net_pnl_after_fees()
    return jsonify(state)


@app.route("/api/start", methods=["POST"])
def api_start():
    state = live_engine.set_running(True)
    return jsonify(state)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    state = live_engine.set_running(False)
    return jsonify(state)


@app.route("/api/params", methods=["POST"])
def api_params():
    new_params = request.get_json(force=True) or {}
    state = live_engine.update_params(new_params)
    return jsonify(state)


@app.route("/api/candles", methods=["GET"])
def api_candles():
    symbol = request.args.get("symbol", config.SYMBOLS[0])
    tf_label = request.args.get("tf", "1m")
    n = int(request.args.get("n", config.CHART_BARS_PER_TF.get(tf_label, config.CANDLES_PER_TIMEFRAME)))

    if symbol not in config.SYMBOLS:
        return jsonify({"error": f"unknown symbol {symbol}"}), 400
    if tf_label not in config.TIMEFRAMES:
        return jsonify({"error": f"unknown timeframe {tf_label}"}), 400

    df = data_store.load_history(symbol, tf_label)
    window = data_store.latest_window(df, n)

    candles = [
        {
            "time": ts.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for ts, row in window.iterrows()
    ]
    return jsonify({"symbol": symbol, "timeframe": tf_label, "candles": candles})


@app.route("/api/open_positions", methods=["GET"])
def api_open_positions():
    return jsonify(trade_journal.load_open_positions())


@app.route("/api/live_positions", methods=["GET"])
def api_live_positions():
    """Live open positions + wallet balance pulled directly from the
    Bybit demo account (not the local paper journal). Returns an
    'enabled': false flag with an explanatory message if demo API keys
    are not yet configured in core/bybit_keys.py, instead of erroring."""
    try:
        positions = bybit_trade_client.get_open_positions()
        balance = bybit_trade_client.get_wallet_balance()
        return jsonify({"enabled": True, "positions": positions, "balance": balance})
    except BybitAuthError as exc:
        return jsonify({"enabled": False, "positions": [], "balance": None, "message": str(exc)})
    except BybitApiError as exc:
        return jsonify({"enabled": True, "positions": [], "balance": None, "message": str(exc)}), 200


@app.route("/api/trade_log", methods=["GET"])
def api_trade_log():
    limit = int(request.args.get("limit", 500))
    return jsonify(trade_journal.load_trade_log(limit=limit))


@app.route("/api/equity_curve", methods=["GET"])
def api_equity_curve():
    limit = int(request.args.get("limit", 2000))
    return jsonify(trade_journal.load_equity_curve(limit=limit))


@app.route("/api/symbols", methods=["GET"])
def api_symbols():
    return jsonify({"symbols": config.SYMBOLS, "timeframes": list(config.TIMEFRAMES.keys())})


@app.route("/api/backtest_chart", methods=["GET"])
def api_backtest_chart():
    """
    Candles (1m/5m/1h) + EMA overlays + simulated trades for one symbol,
    used by the price-chart tab so trade entries/exits can be verified
    visually against the actual OHLC data.
    """
    symbol = request.args.get("symbol", config.SYMBOLS[0])
    if symbol not in config.SYMBOLS:
        return jsonify({"error": f"unknown symbol {symbol}"}), 400

    ema_fast = int(request.args.get("ema_fast", config.DEFAULT_EMA_FAST))
    ema_slow = int(request.args.get("ema_slow", config.DEFAULT_EMA_SLOW))
    tp_pct = float(request.args.get("tp_pct", config.DEFAULT_TP_PCT))
    sl_pct = float(request.args.get("sl_pct", config.DEFAULT_SL_PCT))
    direction = request.args.get("direction", config.DEFAULT_DIRECTION)
    days = int(request.args.get("days", 90))
    tie_break = request.args.get("tie_break", "sl_first")
    if tie_break not in ("sl_first", "tp_first"):
        tie_break = "sl_first"

    result = backtest_engine.backtest_chart_data(
        symbol=symbol,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        direction=direction,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        days=days,
        tie_break=tie_break,
    )
    return jsonify(result)


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    body = request.get_json(force=True) or {}

    symbols = body.get("symbols", config.SYMBOLS)
    ema_fast = int(body.get("ema_fast", config.DEFAULT_EMA_FAST))
    ema_slow = int(body.get("ema_slow", config.DEFAULT_EMA_SLOW))
    tp_pct = float(body.get("tp_pct", config.DEFAULT_TP_PCT))
    sl_pct = float(body.get("sl_pct", config.DEFAULT_SL_PCT))
    direction = body.get("direction", config.DEFAULT_DIRECTION)
    initial_equity = float(body.get("initial_equity", config.DEFAULT_INITIAL_EQUITY))
    margin_per_trade = float(body.get("margin_per_trade", config.DEFAULT_MARGIN_PER_TRADE))
    leverage = float(body.get("leverage", config.DEFAULT_LEVERAGE))
    max_open_positions = int(body.get("max_open_positions", config.DEFAULT_MAX_OPEN_POSITIONS))
    days = int(body.get("days", 90))
    tie_break = body.get("tie_break", "sl_first")
    if tie_break not in ("sl_first", "tp_first"):
        tie_break = "sl_first"

    result = backtest_engine.run_backtest(
        symbols=symbols,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        direction=direction,
        initial_equity=initial_equity,
        margin_per_trade=margin_per_trade,
        leverage=leverage,
        max_open_positions=max_open_positions,
        days=days,
        tie_break=tie_break,
    )
    # summary already carries data_start / data_end / tie_break
    # (set inside backtest_engine.run_backtest), returned as-is.
    return jsonify(result)


@app.route("/api/backtest_progress")
def api_backtest_progress():
    return jsonify(backtest_engine.get_backtest_progress())


def _bootstrap_history():
    """Make sure every symbol/timeframe has at least CANDLES_PER_TIMEFRAME
    candles before the dashboard's first paint, so the chart is never empty
    on first load. The deeper 100-hour backfill runs separately afterward
    in a background thread so it never blocks/delays server startup."""
    for symbol in config.SYMBOLS:
        for tf_label, interval in config.TIMEFRAMES.items():
            try:
                data_store.sync_symbol_timeframe(symbol, tf_label, interval, min_candles=config.CANDLES_PER_TIMEFRAME)
            except Exception as exc:
                print(f"[bootstrap] failed for {symbol} {tf_label}: {exc}")


def _backfill_full_lookback():
    """Runs in a background thread (non-blocking) to deepen every
    symbol/timeframe's local history up to the full 100-hour lookback,
    so chart + EMA overlay have consistent context on every timeframe."""
    import time as _time
    import pandas as _pd

    for symbol in config.SYMBOLS:
        for tf_label, interval in config.TIMEFRAMES.items():
            min_bars = config.CHART_BARS_PER_TF.get(tf_label, config.CANDLES_PER_TIMEFRAME)
            try:
                df = data_store.load_history(symbol, tf_label)
                if len(df) >= min_bars:
                    continue
                now_ms = int(_pd.Timestamp.utcnow().value // 1_000_000)
                interval_ms = {"1": 60_000, "5": 5 * 60_000, "60": 60 * 60_000}.get(interval, 60_000)
                start_ms = now_ms - min_bars * interval_ms
                print(f"[backfill] {symbol} {tf_label}: {len(df)} -> target {min_bars} bars...")
                backfilled = bybit_client.fetch_klines_range(symbol, interval, start_time_ms=start_ms, end_time_ms=now_ms)
                if not backfilled.empty:
                    merged = _pd.concat([df, backfilled]).sort_index()
                    merged = merged[~merged.index.duplicated(keep="last")]
                    data_store.save_history(symbol, tf_label, merged)
                    print(f"[backfill] {symbol} {tf_label}: now {len(merged)} bars")
                _time.sleep(0.2)
            except Exception as exc:
                print(f"[backfill] failed for {symbol} {tf_label}: {exc}")
    print("[backfill] 100-hour lookback backfill complete for all symbols.")


if __name__ == "__main__":
    print("Bootstrapping candle history (all symbols / 1m, 5m, 1h)...")
    _bootstrap_history()

    print("Starting live engine background thread...")
    live_engine.start_background_thread()

    print("Starting 100-hour lookback backfill in background thread...")
    threading.Thread(target=_backfill_full_lookback, daemon=True).start()

    print("Dashboard: http://127.0.0.1:5000")
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

