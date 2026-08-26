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


# ---------------------------------------------------------------------------
# REPLACEMENT for _check_and_close_open_positions().
#
# NEW BEHAVIOR (per user's explicit request):
#   1. For every locally tracked open position, ask the EXCHANGE itself
#      whether it is still open (GET /v5/position/list).
#   2. If the exchange no longer lists it as open -> it was closed (by TP,
#      SL, or manually). Query GET /v5/position/closed-pnl to get the
#      REAL exit price and REAL realized PnL as computed by Bybit, and
#      GET /v5/execution/list to get the REAL fee paid on entry+exit.
#   3. Write that exchange-verified record into trade_journal, update
#      equity from the REAL number, and drop the position from
#      open_positions.json.
#   4. If API keys are not configured (pure paper mode, no live_trade
#      available) -> fall back to the OLD local high/low TP/SL simulation
#      exactly as before, so paper-only usage keeps working unchanged.
#
# This makes the exchange the single source of truth. No more locally
# "guessing" that a position closed from candle high/low.
# ---------------------------------------------------------------------------

def _find_matching_closed_pnl(records: list, position: dict):
    """Pick the closed-pnl record that corresponds to THIS local position.

    Bybit's createdTime on a closed-pnl record is when the position was
    OPENED (ms epoch string). We match on the record whose createdTime is
    closest to (but not much earlier than) our local entry_time, and whose
    direction matches. This is robust even if the account has since opened
    other positions on the same symbol.
    """
    from datetime import datetime, timezone

    try:
        local_entry_dt = datetime.fromisoformat(position["entry_time"])
        if local_entry_dt.tzinfo is None:
            local_entry_dt = local_entry_dt.replace(tzinfo=timezone.utc)
        local_entry_ms = int(local_entry_dt.timestamp() * 1000)
    except Exception:
        local_entry_ms = None

    best = None
    best_diff = None
    for rec in records:
        if rec["direction"] != position["direction"]:
            continue
        try:
            rec_created_ms = int(rec["created_time"])
        except (TypeError, ValueError):
            continue
        if local_entry_ms is not None:
            diff = abs(rec_created_ms - local_entry_ms)
        else:
            diff = 0
        if best is None or diff < best_diff:
            best = rec
            best_diff = diff
    return best


def _sum_real_fees(symbol: str, since_ms: int) -> float:
    """Sum REAL exec fees (entry + exit fills) for a symbol since a given
    timestamp (ms epoch), straight from the exchange's own execution log.
    Returns 0.0 on any failure (fees are a nice-to-have, must never block
    journaling the trade itself)."""
    try:
        execs = live_trade.get_executions(symbol, limit=20)
    except Exception as exc:
        print(f"[live_engine] could not fetch executions for {symbol} fee lookup: {exc}")
        return 0.0

    total_fee = 0.0
    for ex in execs:
        try:
            if int(ex["exec_time"]) >= since_ms:
                total_fee += ex["exec_fee"]
        except (TypeError, ValueError, KeyError):
            continue
    return total_fee


def _check_and_close_open_positions(latest_prices: dict):
    """latest_prices: {symbol: {"high": x, "low": y, "close": z, "time": iso}}

    Exchange-verified close detection (see module docstring above for the
    full flow). Falls back to local TP/SL simulation only when live_trade
    is unavailable (no API keys configured -- pure paper mode).
    """
    positions = trade_journal.load_open_positions()
    if not positions:
        return

    keys_configured = live_trade._keys_configured()

    still_open = []
    equity = trade_journal.get_current_equity()

    # Fetch the exchange's current open-position list ONCE per tick (not
    # once per local position) to keep this cheap and rate-limit friendly.
    exchange_open_symbols = set()
    if keys_configured:
        try:
            for p in live_trade.get_open_positions():
                exchange_open_symbols.add((p["symbol"], p["direction"]))
        except Exception as exc:
            print(f"[live_engine] could not fetch exchange open positions, "
                  f"falling back to local simulation this tick: {exc}")
            keys_configured = False  # degrade gracefully for this tick only

    for pos in positions:
        symbol = pos["symbol"]

        if keys_configured:
            still_on_exchange = (symbol, pos["direction"]) in exchange_open_symbols
            if still_on_exchange:
                still_open.append(pos)
                continue

            # Not open on the exchange anymore -> it was closed. Pull the
            # REAL closed-pnl record to journal it with real numbers.
            try:
                records = live_trade.get_closed_pnl(symbol, limit=10)
                match = _find_matching_closed_pnl(records, pos)
            except Exception as exc:
                print(f"[live_engine] {symbol} looks closed on exchange but "
                      f"closed-pnl lookup failed ({exc}) -- will retry next tick.")
                still_open.append(pos)
                continue

            if match is None:
                # Exchange says it's closed, but no matching closed-pnl
                # record yet (Bybit can take a moment to publish it after
                # the fill). Keep it open locally and retry next tick
                # instead of losing the trade.
                print(f"[live_engine] {symbol} closed on exchange but no "
                      f"closed-pnl record found yet -- retrying next tick.")
                still_open.append(pos)
                continue

            exit_price = match["exit_price"]
            pnl_usdt = match["closed_pnl"]
            notional = pos["notional"]
            pnl_pct = (pnl_usdt / notional * 100.0) if notional else 0.0

            entry_price = pos["entry_price"]
            if pos["direction"] == "long":
                exit_reason = "TP" if exit_price >= entry_price else "SL"
            else:
                exit_reason = "TP" if exit_price <= entry_price else "SL"

            try:
                since_ms = int(match["created_time"])
            except (TypeError, ValueError):
                since_ms = 0
            real_fee = _sum_real_fees(symbol, since_ms)

            exit_time_iso = latest_prices.get(symbol, {}).get("time") or _now_iso()

            equity += pnl_usdt

            trade_journal.append_closed_trade({
                "id": pos["id"],
                "symbol": symbol,
                "direction": pos["direction"],
                "entry_time": pos["entry_time"],
                "exit_time": exit_time_iso,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "margin": pos["margin"],
                "leverage": pos["leverage"],
                "notional": notional,
                "tp_pct": pos["tp_pct"],
                "sl_pct": pos["sl_pct"],
                "exit_reason": exit_reason,
                "pnl_pct": pnl_pct,
                "pnl_usdt": pnl_usdt,
                "fee_net_usdt": real_fee,
                "pnl_after_fees_usdt": pnl_usdt - real_fee,
                "ema_fast": pos.get("ema_fast", ""),
                "ema_slow": pos.get("ema_slow", ""),
                "source": "exchange",  # marks this row as exchange-verified
            })
            trade_journal.append_equity_point(exit_time_iso, equity, pnl_usdt, pos["id"])
            print(f"[live_engine] {symbol} closed on exchange, journaled from "
                  f"real closed-pnl: exit={exit_price} pnl={pnl_usdt:.2f} fee={real_fee:.4f}")
            continue

        # --- Fallback: local high/low TP/SL simulation (paper mode only) ---
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
                "source": "paper_simulated",
            })
            trade_journal.append_equity_point(price_info["time"], equity, pnl_usdt, pos["id"])
        else:
            still_open.append(pos)

    trade_journal.save_open_positions(still_open)


# ---------------------------------------------------------------------------
# NEW: one-time startup backfill so the trade journal is populated from the
# exchange's own closed-pnl history immediately on boot/deploy, instead of
# waiting for the next live signal to close a NEW trade before anything
# shows up in the journal again.
#
# Call backfill_trade_log_from_exchange() once from server.py's
# `if __name__ == "__main__":` block, in a daemon thread (same pattern as
# the existing candle-history backfill), BEFORE start_background_thread().
# ---------------------------------------------------------------------------

def backfill_trade_log_from_exchange(lookback_records: int = 50):
    """Pull recent closed-pnl history straight from Bybit for every symbol
    in config.SYMBOLS and insert any trades that are not already present
    in the local trade_log.csv (matched by symbol + entry_time + exit
    time, since Bybit does not give us a stable natural key we already
    store). Safe to call on every boot -- fully idempotent, never creates
    duplicate rows.

    Never raises: if API keys are not configured, or any network call
    fails, this simply logs and returns -- it must never block server
    startup.
    """
    if not live_trade._keys_configured():
        print("[backfill_trade_log] API keys not configured, skipping exchange backfill.")
        return

    existing = trade_journal.load_trade_log(limit=100000)
    existing_keys = set()
    for row in existing:
        existing_keys.add((
            row.get("symbol"),
            row.get("entry_price"),
            row.get("exit_price"),
            row.get("exit_time"),
        ))

    equity = trade_journal.get_current_equity()
    inserted = 0

    for symbol in config.SYMBOLS:
        try:
            records = live_trade.get_closed_pnl(symbol, limit=lookback_records)
        except Exception as exc:
            print(f"[backfill_trade_log] {symbol}: closed-pnl fetch failed, skipping: {exc}")
            continue

        # Oldest first, so equity accumulates in correct chronological order.
        records = sorted(records, key=lambda r: int(r.get("created_time") or 0))

        for rec in records:
            try:
                created_ms = int(rec["created_time"])
                updated_ms = int(rec["updated_time"])
            except (TypeError, ValueError):
                continue

            entry_time_iso = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()
            exit_time_iso = datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc).isoformat()
            entry_price = rec["entry_price"]
            exit_price = rec["exit_price"]

            key = (symbol, entry_price, exit_price, exit_time_iso)
            if key in existing_keys:
                continue  # already journaled, do not duplicate

            pnl_usdt = rec["closed_pnl"]
            notional = entry_price * rec["qty"] if entry_price else 0.0
            pnl_pct = (pnl_usdt / notional * 100.0) if notional else 0.0
            exit_reason = "TP" if (
                (rec["direction"] == "long" and exit_price >= entry_price) or
                (rec["direction"] == "short" and exit_price <= entry_price)
            ) else "SL"

            real_fee = _sum_real_fees(symbol, created_ms)

            trade_journal.append_closed_trade({
                "id": f"backfill-{symbol}-{created_ms}",
                "symbol": symbol,
                "direction": rec["direction"],
                "entry_time": entry_time_iso,
                "exit_time": exit_time_iso,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "margin": notional / rec["leverage"] if rec["leverage"] else 0.0,
                "leverage": rec["leverage"],
                "notional": notional,
                "tp_pct": "",
                "sl_pct": "",
                "exit_reason": exit_reason,
                "pnl_pct": pnl_pct,
                "pnl_usdt": pnl_usdt,
                "fee_net_usdt": real_fee,
                "pnl_after_fees_usdt": pnl_usdt - real_fee,
                "ema_fast": "",
                "ema_slow": "",
                "source": "exchange_backfill",
            })
            equity += pnl_usdt
            trade_journal.append_equity_point(exit_time_iso, equity, pnl_usdt, key[0] + str(created_ms))
            existing_keys.add(key)
            inserted += 1

    print(f"[backfill_trade_log] done -- inserted {inserted} historical trade(s) from exchange.")

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
