"""
Persistent trade journal + open-position storage.

Everything is stored as plain JSON/CSV on disk so the Flask server, the
live engine process, and the dashboard all agree on the same source of
truth, and so nothing is lost across restarts.
"""

import json
import csv
from pathlib import Path
from datetime import datetime, timezone

from . import config


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Open positions (list of dicts), persisted as JSON
# ---------------------------------------------------------------------------

def load_open_positions():
    path = config.OPEN_POSITIONS_FILE
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_open_positions(positions):
    path = config.OPEN_POSITIONS_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(positions, f, indent=2, ensure_ascii=False)


def add_open_position(position: dict):
    positions = load_open_positions()
    positions.append(position)
    save_open_positions(positions)


def remove_open_position(position_id: str):
    positions = load_open_positions()
    positions = [p for p in positions if p.get("id") != position_id]
    save_open_positions(positions)


# ---------------------------------------------------------------------------
# Closed trade log, persisted as CSV (append-only)
# ---------------------------------------------------------------------------

TRADE_LOG_FIELDS = [
    "id", "symbol", "direction", "entry_time", "exit_time",
    "entry_price", "exit_price", "margin", "leverage", "notional",
    "tp_pct", "sl_pct", "exit_reason", "pnl_pct", "pnl_usdt",
    "fee_gross_usdt", "rebate_usdt", "fee_net_usdt", "pnl_after_fees_usdt",
    "ema_fast", "ema_slow", "closed_at",
]


def _ensure_trade_log_header():
    path = config.TRADE_LOG_FILE
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            writer.writeheader()


def compute_fees(notional: float) -> dict:
    """Taker fee is charged on both entry AND exit (two market fills), then
    a rebate percentage of that gross fee is returned to the account. The
    NET fee (what actually erodes PnL) is the gross fee minus the rebate.
    All percentages come from config so they stay in sync with the sweep
    scripts and can be tuned per-account later without touching this math.
    """
    fee_pct = config.DEFAULT_TAKER_FEE_PCT / 100.0
    rebate_pct = config.DEFAULT_REBATE_PCT / 100.0
    fee_gross = notional * fee_pct * 2.0   # entry + exit, both taker market fills
    rebate = fee_gross * rebate_pct
    fee_net = fee_gross - rebate
    return {"fee_gross_usdt": fee_gross, "rebate_usdt": rebate, "fee_net_usdt": fee_net}


def append_closed_trade(trade: dict):
    _ensure_trade_log_header()
    trade = dict(trade)
    trade.setdefault("closed_at", _now_iso())

    fees = compute_fees(float(trade.get("notional", 0) or 0))
    trade.setdefault("fee_gross_usdt", fees["fee_gross_usdt"])
    trade.setdefault("rebate_usdt", fees["rebate_usdt"])
    trade.setdefault("fee_net_usdt", fees["fee_net_usdt"])
    trade.setdefault("pnl_after_fees_usdt", float(trade.get("pnl_usdt", 0) or 0) - fees["fee_net_usdt"])

    path = config.TRADE_LOG_FILE
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        writer.writerow({k: trade.get(k, "") for k in TRADE_LOG_FIELDS})


def load_trade_log(limit: int = 500):
    path = config.TRADE_LOG_FILE
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows[-limit:]


# ---------------------------------------------------------------------------
# Equity curve, persisted as CSV (append-only, one row per closed trade)
# ---------------------------------------------------------------------------

EQUITY_FIELDS = ["time", "equity", "pnl_usdt", "trade_id"]


def _ensure_equity_header():
    path = config.EQUITY_CURVE_FILE
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=EQUITY_FIELDS)
            writer.writeheader()


def append_equity_point(time_iso: str, equity: float, pnl_usdt: float, trade_id: str):
    _ensure_equity_header()
    with open(config.EQUITY_CURVE_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EQUITY_FIELDS)
        writer.writerow({"time": time_iso, "equity": equity, "pnl_usdt": pnl_usdt, "trade_id": trade_id})


def load_equity_curve(limit: int = 2000):
    path = config.EQUITY_CURVE_FILE
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows[-limit:]


def get_current_equity():
    rows = load_equity_curve(limit=1)
    if rows:
        return float(rows[-1]["equity"])
    return config.DEFAULT_INITIAL_EQUITY


def get_fee_totals():
    """Sums fee_gross_usdt / rebate_usdt / fee_net_usdt across the entire
    closed trade log, so the dashboard can show accumulated commissions
    and accumulated rebate as running totals, not just per-trade."""
    rows = load_trade_log(limit=100000)
    fee_gross = 0.0
    rebate = 0.0
    fee_net = 0.0
    for r in rows:
        fee_gross += float(r.get("fee_gross_usdt") or 0)
        rebate += float(r.get("rebate_usdt") or 0)
        fee_net += float(r.get("fee_net_usdt") or 0)
    return {"fee_gross_usdt": fee_gross, "rebate_usdt": rebate, "fee_net_usdt": fee_net}


def get_net_pnl_after_fees():
    """Sums pnl_after_fees_usdt across the closed trade log (falls back to
    pnl_usdt minus a freshly computed fee for legacy rows that predate the
    fee columns, so old trades don't show as zero)."""
    rows = load_trade_log(limit=100000)
    total = 0.0
    for r in rows:
        raw = r.get("pnl_after_fees_usdt")
        if raw not in (None, ""):
            total += float(raw)
        else:
            pnl = float(r.get("pnl_usdt") or 0)
            fees = compute_fees(float(r.get("notional") or 0))
            total += pnl - fees["fee_net_usdt"]
    return total
