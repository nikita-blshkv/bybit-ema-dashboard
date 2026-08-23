"""
Authenticated Bybit v5 REST client for DEMO-account trading.

Everything here talks to BYBIT_DEMO_BASE_URL (api-demo.bybit.com) using the
demo API key/secret from core/bybit_keys.py. It places REAL orders on your
Bybit DEMO account (fake money, real exchange matching engine) with the
TP/SL attached directly to the order on Bybit's side -- so once an order is
placed, the exchange itself will close it at TP or SL even if this script
or your computer is completely offline afterward. Only NEW order placement
requires this process to be running; already-open positions are resolved
by Bybit itself.

If BYBIT_DEMO_API_KEY / BYBIT_DEMO_API_SECRET are empty, every function
here raises BybitAuthError immediately -- callers (live_engine.py) must
catch this and simply skip live trading (paper-only fallback) rather than
crash the whole background loop.
"""

import hashlib
import hmac
import json
import time

import requests

from . import bybit_keys


class BybitAuthError(RuntimeError):
    pass


class BybitApiError(RuntimeError):
    pass


def _keys_configured() -> bool:
    return bool(bybit_keys.BYBIT_DEMO_API_KEY) and bool(bybit_keys.BYBIT_DEMO_API_SECRET)


def _sign(payload_str: str, timestamp: str, recv_window: str) -> str:
    api_key = bybit_keys.BYBIT_DEMO_API_KEY
    to_sign = f"{timestamp}{api_key}{recv_window}{payload_str}"
    return hmac.new(
        bybit_keys.BYBIT_DEMO_API_SECRET.encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _request(method: str, path: str, params: dict = None, body: dict = None):
    if not _keys_configured():
        raise BybitAuthError(
            "Bybit demo API key/secret not set in core/bybit_keys.py -- "
            "live trading is disabled until you fill them in."
        )

    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    url = bybit_keys.BYBIT_DEMO_BASE_URL + path

    if method == "GET":
        params = params or {}
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = _sign(query, timestamp, recv_window)
        headers = {
            "X-BAPI-API-KEY": bybit_keys.BYBIT_DEMO_API_KEY,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
        }
        resp = requests.get(url, params=params, headers=headers, timeout=15)
    else:
        body = body or {}
        body_str = json.dumps(body)
        signature = _sign(body_str, timestamp, recv_window)
        headers = {
            "X-BAPI-API-KEY": bybit_keys.BYBIT_DEMO_API_KEY,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
            "Content-Type": "application/json",
        }
        resp = requests.post(url, data=body_str, headers=headers, timeout=15)

    resp.raise_for_status()
    payload = resp.json()
    if payload.get("retCode") != 0:
        raise BybitApiError("{0} -> {1}: {2}".format(path, payload.get("retCode"), payload.get("retMsg")))
    return payload["result"]


def get_wallet_balance() -> dict:
    """Returns the demo account's UNIFIED wallet balance summary."""
    result = _request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"})
    accounts = result.get("list", [])
    if not accounts:
        return {"total_equity": None, "available_balance": None}
    acc = accounts[0]
    return {
        "total_equity": float(acc.get("totalEquity", 0) or 0),
        "available_balance": float(acc.get("totalAvailableBalance", 0) or 0),
    }


def get_open_positions(category: str = "linear", settle_coin: str = "USDT") -> list:
    """
    Returns Bybit's own live view of open positions on the demo account:
    symbol, side, size, leverage, entry price, TP, SL, position value
    (notional), unrealized PnL -- pulled straight from the exchange, not
    simulated locally.
    """
    result = _request("GET", "/v5/position/list", params={"category": category, "settleCoin": settle_coin})
    rows = result.get("list", [])
    positions = []
    for r in rows:
        size = float(r.get("size", 0) or 0)
        if size == 0:
            continue
        positions.append({
            "symbol": r.get("symbol"),
            "direction": "long" if r.get("side") == "Buy" else "short",
            "size": size,
            "leverage": float(r.get("leverage", 0) or 0),
            "entry_price": float(r.get("avgPrice", 0) or 0),
            "mark_price": float(r.get("markPrice", 0) or 0),
            "take_profit": float(r.get("takeProfit", 0) or 0) or None,
            "stop_loss": float(r.get("stopLoss", 0) or 0) or None,
            "notional": float(r.get("positionValue", 0) or 0),
            "margin": float(r.get("positionIM", 0) or 0),
            "unrealized_pnl": float(r.get("unrealisedPnl", 0) or 0),
            "created_time": r.get("createdTime"),
        })
    return positions


def set_isolated_margin(symbol: str, leverage: float, category: str = "linear"):
    """Switch the symbol to ISOLATED margin mode (tradeMode=1) on Bybit,
    instead of the account's default CROSS mode. Errors are swallowed if
    the symbol is already isolated (retCode 110026) or if it can't be
    changed while a position/order is open (retCode 110020) -- in that
    case we keep whatever mode the existing position already has rather
    than crashing the whole order flow."""
    lev_str = str(leverage)
    try:
        _request("POST", "/v5/position/switch-isolated", body={
            "category": category,
            "symbol": symbol,
            "tradeMode": 1,
            "buyLeverage": lev_str,
            "sellLeverage": lev_str,
        })
    except BybitApiError as exc:
        if "110026" not in str(exc) and "110020" not in str(exc):
            raise


def set_leverage(symbol: str, leverage: float, category: str = "linear"):
    """Bybit requires leverage to be set (per symbol) before/with an order
    if it differs from what is already set. Errors here are swallowed by
    the caller if leverage is already at the requested value (Bybit
    returns retCode 110043 'leverage not modified' in that case).

    Also switches the symbol to ISOLATED margin mode first, so every new
    position opens isolated instead of the account's default CROSS mode."""
    set_isolated_margin(symbol, leverage, category=category)

    lev_str = str(leverage)
    try:
        _request("POST", "/v5/position/set-leverage", body={
            "category": category,
            "symbol": symbol,
            "buyLeverage": lev_str,
            "sellLeverage": lev_str,
        })
    except BybitApiError as exc:
        if "110043" not in str(exc):
            raise


def place_market_order_with_tp_sl(symbol: str, direction: str, qty: str,
                                   take_profit_price: float, stop_loss_price: float,
                                   category: str = "linear") -> dict:
    """
    Places a MARKET order on the Bybit demo account with TP/SL attached
    directly to the order (tpslMode=Full), so the exchange itself enforces
    the exit even if this process stops running afterward.

    qty must already be formatted as a string respecting the symbol's lot
    size step (see round_qty_to_step in live_engine.py).
    """
    side = "Buy" if direction == "long" else "Sell"
    body = {
        "category": category,
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": qty,
        "takeProfit": f"{take_profit_price:.6f}",
        "stopLoss": f"{stop_loss_price:.6f}",
        "tpslMode": "Full",
        "tpOrderType": "Market",
        "slOrderType": "Market",
    }
    return _request("POST", "/v5/order/create", body=body)


def get_instrument_qty_step(symbol: str, category: str = "linear") -> float:
    """Fetches the minimum order-quantity step for a symbol so we can round
    the computed position size to a value Bybit will accept."""
    result = _request("GET", "/v5/market/instruments-info", params={"category": category, "symbol": symbol})
    rows = result.get("list", [])
    if not rows:
        return 0.001
    step = rows[0].get("lotSizeFilter", {}).get("qtyStep", "0.001")
    return float(step)
