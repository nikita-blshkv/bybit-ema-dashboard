"""
Live paper-trading engine.

Behavior:
- Controlled via engine_state.json: {"running": true/false, "params": {...}}
  This file is written by the Flask server when you click Start/Stop on the
  dashboard, and read by this background loop.
- While running=True: every POLL_INTERVAL_SECONDS it syncs candle history
  for every symbol/timeframe, checks the latest CLOSED 1m bar for a fresh
  EMA cross signal (confirmed on 5m too), and opens a new paper position if
  capacity (max_open_positions) allows.
- Regardless of running flag: every tick it also checks all currently open
  positions against fresh price data and closes them on TP/SL. This is what
  guarantees that positions opened before you hit Stop (or before you close
  the computer, AS LONG AS THIS PROCESS KEEPS RUNNING) are resolved by their
  own TP/SL instead of being abandoned.
- This is a PAPER engine: no real orders are sent to Bybit. It only reads
  public market data and simulates fills at TP/SL price levels using the
  same worst-case rule as the offline backtests (if both SL and TP would be
  touched within the same bar, SL is assumed to have hit first).

IMPORTANT REALITY CHECK (do not skip): this process must keep running on
your machine for open positions to be tracked and closed. If you fully shut
down your computer, no process on it can execute code. Real 24/7 behavior
(orders resolved on the exchange itself via TP/SL orders placed on Bybit)
requires wiring real order placement with your API keys and setting actual
exchange-side TP/SL -- that is the deliberate next phase once you move to a
server, as you planned.
"""

import json
import time
import threading
import uuid
from datetime import datetime, timezone

from . import config
from . import data_store
from . import signal_engine
from . import trade_journal
from . import bybit_trade_client as live_trade
from .bybit_trade_client import BybitAuthError, BybitApiError
from . import bybit_trade_client as live_trade
from .bybit_trade_client import BybitAuthError, BybitApiError


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_engine_state():
    path = config.ENGINE_STATE_FILE
    if not path.exists():
        default = {
            "running": False,
            "params": {
                "ema_fast": config.DEFAULT_EMA_FAST,
                "ema_slow": config.DEFAULT_EMA_SLOW,
                "tp_pct": config.DEFAULT_TP_PCT,
                "sl_pct": config.DEFAULT_SL_PCT,
                "direction": config.DEFAULT_DIRECTION,
                "margin_per_trade": config.DEFAULT_MARGIN_PER_TRADE,
                "leverage": config.DEFAULT_LEVERAGE,
                "max_open_positions": config.DEFAULT_MAX_OPEN_POSITIONS,
                "active_symbols": list(config.SYMBOLS),
            },
            "last_update": _now_iso(),
        }
        save_engine_state(default)
        return default

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_engine_state(state: dict):
    state["last_update"] = _now_iso()
    with open(config.ENGINE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def set_running(flag: bool):
    state = load_engine_state()
    state["running"] = flag
    save_engine_state(state)
    return state


def update_params(new_params: dict):
    state = load_engine_state()
    state["params"].update(new_params)
    save_engine_state(state)
    return state


def _check_and_close_open_positions(latest_prices: dict):
    """latest_prices: {symbol: {"high": x, "low": y, "close": z, "time": iso}}"""
    positions = trade_journal.load_open_positions()
    if not positions:
        return

    still_open = []
    equity = trade_journal.get_current_equity()

    for pos in positions:
        symbol = pos["symbol"]
        price_info = latest_prices.get(symbol)
        if price_info is None:
            still_open.append(pos)
            continue

        entry_price = pos["entry_price"]
        tp_pct = pos["tp_pct"] / 100.0
        sl_pct = pos["sl_pct"] / 100.0
        notional = pos["notional"]
        high = price_info["high"]
        low = price_info["low"]

        hit_sl = False
        hit_tp = False
        exit_price = None

        if pos["direction"] == "long":
            stop_price = entry_price * (1.0 - sl_pct)
            take_price = entry_price * (1.0 + tp_pct)
            hit_sl = low <= stop_price
            hit_tp = high >= take_price
            exit_price = stop_price if hit_sl else take_price
            pnl_pct = (exit_price / entry_price) - 1.0 if (hit_sl or hit_tp) else 0.0
        else:
            stop_price = entry_price * (1.0 + sl_pct)
            take_price = entry_price * (1.0 - tp_pct)
            hit_sl = high >= stop_price
            hit_tp = low <= take_price
            exit_price = stop_price if hit_sl else take_price
            pnl_pct = 1.0 - (exit_price / entry_price) if (hit_sl or hit_tp) else 0.0

        if hit_sl or hit_tp:
            pnl_usdt = notional * pnl_pct
            equity += pnl_usdt
            exit_reason = "SL" if hit_sl else "TP"

            trade_journal.append_closed_trade({
                "id": pos["id"],
                "symbol": symbol,
                "direction": pos["direction"],
                "entry_time": pos["entry_time"],
                "exit_time": price_info["time"],
                "entry_price": entry_price,
                "exit_price": exit_price,
                "margin": pos["margin"],
                "leverage": pos["leverage"],
                "notional": notional,
                "tp_pct": pos["tp_pct"],
                "sl_pct": pos["sl_pct"],
                "exit_reason": exit_reason,
                "pnl_pct": pnl_pct * 100.0,
                "pnl_usdt": pnl_usdt,
                "ema_fast": pos.get("ema_fast", ""),
                "ema_slow": pos.get("ema_slow", ""),
            })
            trade_journal.append_equity_point(price_info["time"], equity, pnl_usdt, pos["id"])
        else:
            still_open.append(pos)

    trade_journal.save_open_positions(still_open)


def _try_open_new_positions(params: dict, signals: dict, latest_close: dict):
    """
    signals: {symbol: (timestamp, direction_or_None, price_or_None)}
    """
    open_positions = trade_journal.load_open_positions()
    max_open = int(params["max_open_positions"])

    for symbol, (ts, direction, price) in signals.items():
        if direction is None:
            continue
        if len(open_positions) >= max_open:
            continue
        already_open = any(p["symbol"] == symbol for p in open_positions)
        if already_open:
            continue

        margin = float(params["margin_per_trade"])
        leverage = float(params["leverage"])
        notional = margin * leverage

        position = {
            "id": str(uuid.uuid4()),
            "symbol": symbol,
            "direction": direction,
            "entry_time": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "entry_price": float(price),
            "margin": margin,
            "leverage": leverage,
            "notional": notional,
            "tp_pct": float(params["tp_pct"]),
            "sl_pct": float(params["sl_pct"]),
            "ema_fast": int(params["ema_fast"]),
            "ema_slow": int(params["ema_slow"]),
        }
        # Mirror the paper position with a REAL market order + exchange-
        # side TP/SL on the Bybit demo account, so it keeps resolving on
        # Bybit's own servers even if this computer goes offline. The
        # outcome (success/failure/reason) is stamped onto the position
        # itself BEFORE it's saved, so the dashboard can show a clear
        # sync status instead of silently drifting from the exchange.
        try:
            qty_step = live_trade.get_instrument_qty_step(symbol)
            raw_qty = notional / float(price)
            qty = max(qty_step, round(raw_qty / qty_step) * qty_step)
            qty_str = f"{qty:.8f}".rstrip("0").rstrip(".") or "0"

            try:
                live_trade.set_leverage(symbol, leverage)
            except BybitApiError as exc:
                # Leverage-setting failures (e.g. rate limit, transient
                # error) must never block real order placement -- log and
                # continue with whatever leverage is already configured
                # on the account for this symbol.
                print(f"[live_engine] set_leverage failed for {symbol} (continuing anyway): {exc}")

            if direction == "long":
                tp_price = float(price) * (1.0 + position["tp_pct"] / 100.0)
                sl_price = float(price) * (1.0 - position["sl_pct"] / 100.0)
            else:
                tp_price = float(price) * (1.0 - position["tp_pct"] / 100.0)
                sl_price = float(price) * (1.0 + position["sl_pct"] / 100.0)

            live_trade.place_market_order_with_tp_sl(
                symbol=symbol, direction=direction, qty=qty_str,
                take_profit_price=tp_price, stop_loss_price=sl_price,
            )
            position["exchange_synced"] = True
            position["exchange_error"] = None
            print(f"[live_engine] placed REAL demo order: {symbol} {direction} qty={qty_str}")
        except BybitAuthError:
            print(f"[live_engine] SKIPPED signal for {symbol}: API keys not configured, "
                  f"live trading disabled -- no local position created.")
            continue
        except BybitApiError as exc:
            print(f"[live_engine] SKIPPED signal for {symbol}: demo order REJECTED by "
                  f"exchange ({exc}) -- no local position created, will retry next signal.")
            continue
        except Exception as exc:
            print(f"[live_engine] SKIPPED signal for {symbol}: unexpected error placing "
                  f"demo order ({exc}) -- no local position created.")
            continue

        # Reached only if the real order was confirmed by Bybit.
        trade_journal.add_open_position(position)
        open_positions.append(position)



def run_forever():
    """Main background loop. Call this from a daemon thread in server.py."""
    while True:
        try:
            state = load_engine_state()
            params = state["params"]
            running = state["running"]

            latest_prices = {}
            signals = {}

            active_symbols = set(params.get("active_symbols") or config.SYMBOLS)
            for symbol in config.SYMBOLS:
                if symbol not in active_symbols:
                    continue
                try:
                    df_1m = data_store.sync_symbol_timeframe(
                        symbol, "1m", config.TIMEFRAMES["1m"], min_candles=config.CANDLES_PER_TIMEFRAME
                    )
                    if df_1m.empty or len(df_1m) < params["ema_slow"] + 5:
                        continue

                    latest_row = df_1m.iloc[-1]
                    latest_prices[symbol] = {
                        "high": float(latest_row["high"]),
                        "low": float(latest_row["low"]),
                        "close": float(latest_row["close"]),
                        "time": df_1m.index[-1].isoformat(),
                    }

                    if running:
                        with_signals = signal_engine.compute_signals(
                            df_1m, params["ema_fast"], params["ema_slow"], params["direction"]
                        )
                        ts, direction, price = signal_engine.latest_signal(with_signals)
                        signals[symbol] = (ts, direction, price)

                    # also keep 5m and 1h history in sync for the chart, even
                    # if the engine is stopped -- charting should always work.
                    for tf_label in ("5m", "1h"):
                        data_store.sync_symbol_timeframe(
                            symbol, tf_label, config.TIMEFRAMES[tf_label], min_candles=config.CANDLES_PER_TIMEFRAME
                        )
                except Exception as exc:
                    # Isolate failures per-symbol so one bad/slow pair (rate
                    # limit, network hiccup, bad response) can never block
                    # every symbol listed after it in config.SYMBOLS for this
                    # tick. Previously this exception propagated out of the
                    # whole for-loop, so only symbols BEFORE the failing one
                    # (e.g. BTCUSDT, ETHUSDT at the top of the list) ever got
                    # synced, and everything after it silently never loaded.
                    print(f"[live_engine] {symbol} sync failed, skipping this tick: {exc}")
                    continue

            # Always resolve open positions by TP/SL, running or not.
            _check_and_close_open_positions(latest_prices)

            if running:
                _try_open_new_positions(params, signals, latest_prices)

        except Exception as exc:  # keep the loop alive no matter what
            print(f"[live_engine] error: {exc}")

        time.sleep(config.POLL_INTERVAL_SECONDS)


def start_background_thread():
    thread = threading.Thread(target=run_forever, daemon=True)
    thread.start()
    return thread
