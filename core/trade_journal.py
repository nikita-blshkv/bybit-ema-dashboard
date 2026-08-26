"""
Persistent trade journal + open-position storage.

Everything is stored as plain JSON/CSV on disk so the Flask server, the
live engine process, and the dashboard all agree on the same source of
truth, and so nothing is lost across restarts.

RELIABILITY NOTE (added after a real production incident): all writes
to trade_log.csv and equity_curve.csv now go through an exclusive file
lock (fcntl.flock) so the startup backfill thread and the live-engine
background thread can never interleave writes and corrupt a row (which
previously caused a column-count mismatch and crashed /api/trade_log
with a TypeError when Flask tried to JSON-encode the malformed rows).
load_trade_log() is also now defensive: any row that doesn't match the
expected column count is skipped (and logged) instead of propagating a
broken dict all the way to the JSON encoder and crashing the endpoint.
"""

import json
import csv
import fcntl
import os
from pathlib import Path
from datetime import datetime, timezone

from . import config


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class _locked_file:
    """Context manager: opens a file and holds an exclusive OS-level lock
    (fcntl.flock) for the duration of the block, so concurrent threads/
    processes writing to the same CSV can never interleave partial rows.
    Blocks (waits) rather than failing if another writer currently holds
    the lock -- writes here are tiny and infrequent, so this never stalls
    noticeably."""

    def __init__(self, path, mode):
        self.path = path
        self.mode = mode
        self.f = None

    def __enter__(self):
        self.f = open(self.path, self.mode, newline="", encoding="utf-8")
        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        return self.f

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.f.flush()
            os.fsync(self.f.fileno())
        finally:
            fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            self.f.close()
        return False


# ---------------------------------------------------------------------------
# Open positions (list of dicts), persisted as JSON
# ---------------------------------------------------------------------------

def load_open_positions():
    path = config.OPEN_POSITIONS_FILE
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            return json.load(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def save_open_positions(positions):
    path = config.OPEN_POSITIONS_FILE
    with open(path, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(positions, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


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
    "ema_fast", "ema_slow", "closed_at", "source",
]


def _ensure_trade_log_header():
    path = config.TRADE_LOG_FILE
    if not path.exists():
        with _locked_file(path, "w") as f:
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
    with _locked_file(path, "a") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        writer.writerow({k: trade.get(k, "") for k in TRADE_LOG_FIELDS})


def load_trade_log(limit: int = 500):
    """Defensive read: any row whose column count doesn't match the header
    (e.g. a historical row written before the "source" column existed, or
    any other on-disk corruption) is skipped instead of being returned as
    a broken dict with a None key -- which previously crashed the JSON
    encoder in /api/trade_log with 'TypeError: ... NoneType and str'.
    Skipped rows are counted and logged so corruption is visible, not
    silent."""
    path = config.TRADE_LOG_FILE
    if not path.exists():
        return []

    rows = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return []
            expected_len = len(header)
            for raw_row in reader:
                if len(raw_row) != expected_len:
                    skipped += 1
                    continue
                row = dict(zip(header, raw_row))
                if None in row:
                    skipped += 1
                    continue
                rows.append(row)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    if skipped:
        print(f"[trade_journal] load_trade_log: skipped {skipped} malformed row(s) in {path}")

    return rows[-limit:]


# ---------------------------------------------------------------------------
# Equity curve, persisted as CSV (append-only, one row per closed trade)
# ---------------------------------------------------------------------------

EQUITY_FIELDS = ["time", "equity", "pnl_usdt", "trade_id"]


def _ensure_equity_header():
    path = config.EQUITY_CURVE_FILE
    if not path.exists():
        with _locked_file(path, "w") as f:
            writer = csv.DictWriter(f, fieldnames=EQUITY_FIELDS)
            writer.writeheader()


def append_equity_point(time_iso: str, equity: float, pnl_usdt: float, trade_id: str):
    _ensure_equity_header()
    with _locked_file(config.EQUITY_CURVE_FILE, "a") as f:
        writer = csv.DictWriter(f, fieldnames=EQUITY_FIELDS)
        writer.writerow({"time": time_iso, "equity": equity, "pnl_usdt": pnl_usdt, "trade_id": trade_id})


def load_equity_curve(limit: int = 2000):
    """Same defensive-read treatment as load_trade_log (see above)."""
    path = config.EQUITY_CURVE_FILE
    if not path.exists():
        return []

    rows = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return []
            expected_len = len(header)
            for raw_row in reader:
                if len(raw_row) != expected_len:
                    skipped += 1
                    continue
                rows.append(dict(zip(header, raw_row)))
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    if skipped:
        print(f"[trade_journal] load_equity_curve: skipped {skipped} malformed row(s) in {path}")

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
    pnl_usdt minus a freshly computed fee if pnl_after_fees_usdt is blank
    on an older row)."""
    rows = load_trade_log(limit=100000)
    total = 0.0
    for r in rows:
        val = r.get("pnl_after_fees_usdt")
        if val not in (None, ""):
            total += float(val)
        else:
            pnl = float(r.get("pnl_usdt") or 0)
            fees = compute_fees(float(r.get("notional") or 0))
            total += pnl - fees["fee_net_usdt"]
    return total
