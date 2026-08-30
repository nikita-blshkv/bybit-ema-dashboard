/*
 * app.js -- frontend logic for the EMA Cross Dashboard.
 *
 * Talks only to the local Flask server (server.py) via fetch() calls to
 * /api/* endpoints. No API keys, no direct calls to Bybit from the browser.
 *
 * Sections:
 *   1. Small helpers (fetch wrapper, toast, formatting)
 *   2. Tab switching (Live / Backtest)
 *   3. Live tab: status polling, params form, candle chart, open positions,
 *      trade log, equity stat cards
 *   4. Backtest tab: run backtest, render summary + equity chart + trades
 */

const API = "";

// ---------------------------------------------------------------------
// 1. Helpers
// ---------------------------------------------------------------------

async function apiGet(path) {
  const res = await fetch(API + path);
  if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`);
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(`POST ${path} failed: ${res.status}`);
  return res.json();
}

function showToast(msg) {
  const toast = document.getElementById("toast");
  toast.textContent = msg;
  toast.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.remove("show"), 2600);
}

function fmtUsd(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  return (v >= 0 ? "+" : "") + v.toFixed(2);
}

function fmtPct(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
}

function pnlClass(n) {
  return Number(n) >= 0 ? "pnl-pos" : "pnl-neg";
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("ru-RU", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    return iso;
  }
}

// ---------------------------------------------------------------------
// 2. Tab switching
// ---------------------------------------------------------------------

document.querySelectorAll("nav.tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav.tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");

    const tab = btn.dataset.tab;
    document.getElementById("tab-live").classList.toggle("active", tab === "live");
    document.getElementById("tab-backtest").classList.toggle("active", tab === "backtest");
    document.getElementById("tab-chart").classList.toggle("active", tab === "chart");

    if (tab === "live" && liveChart) {
      setTimeout(() => liveChart.timeScale().fitContent(), 50);
    }
    if (tab === "chart") {
      ctTabActive = true;
      if (!ctChart) initChartTab();
      if (!ctLastData) {
        loadChartTabData();
      } else {
        refreshChartTabData(true); // catch up on any gap since last visit
      }
    } else {
      ctTabActive = false;
    }
  });
});

// ---------------------------------------------------------------------
// 3. LIVE TAB
// ---------------------------------------------------------------------

let currentSymbol = "BTCUSDT";
let currentTf = "4m";
let liveChart = null;
let liveCandleSeries = null;
let liveEmaFast1m = null;
let liveEmaSlow1m = null;
let liveEmaFast5m = null;
let liveEmaSlow5m = null;
let liveLastCandlesByTf = { "4m": [], "8m": [], "1h": [] };

function initLiveChart() {
  const container = document.getElementById("chart-container");
  liveChart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: "#161b24" },
      textColor: "#8b93a3",
    },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.04)" },
      horzLines: { color: "rgba(255,255,255,0.04)" },
    },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
    timeScale: { borderColor: "rgba(255,255,255,0.08)", timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    width: container.clientWidth,
    height: 420,
  });

  liveCandleSeries = liveChart.addCandlestickSeries({
    upColor: "#26a269",
    downColor: "#d64545",
    borderVisible: false,
    wickUpColor: "#26a269",
    wickDownColor: "#d64545",
  });

  liveEmaFast1m = liveChart.addLineSeries({ color: "#4f9dff", lineWidth: 1, title: "EMA fast 4m HA", priceLineVisible: false, lastValueVisible: false });
  liveEmaSlow1m = liveChart.addLineSeries({ color: "#a259ff", lineWidth: 1, title: "EMA slow 4m HA", priceLineVisible: false, lastValueVisible: false });
  liveEmaFast5m = liveChart.addLineSeries({ color: "#ffb84f", lineWidth: 2, title: "EMA fast 8m HA", priceLineVisible: false, lastValueVisible: false });
  liveEmaSlow5m = liveChart.addLineSeries({ color: "#ff5f8f", lineWidth: 2, title: "EMA slow 8m HA", priceLineVisible: false, lastValueVisible: false });

  const legend = document.getElementById("live-chart-legend");
  if (legend) {
    legend.innerHTML = `
      <span style="color:#4f9dff;">─ EMA fast 4m HA</span>
      <span style="color:#a259ff;">─ EMA slow 4m HA</span>
      <span style="color:#ffb84f;">─ EMA fast 8m HA</span>
      <span style="color:#ff5f8f;">─ EMA slow 8m HA</span>`;
  }

  window.addEventListener("resize", () => {
    liveChart.applyOptions({ width: container.clientWidth });
  });
}

function computeEma(values, period) {
  if (values.length === 0) return [];
  const k = 2 / (period + 1);
  const out = new Array(values.length);
  out[0] = values[0];
  for (let i = 1; i < values.length; i++) {
    out[i] = values[i] * k + out[i - 1] * (1 - k);
  }
  return out;
}

function buildHeikinAshiBars(bars) {
  if (!bars || !bars.length) return [];
  const out = [];
  bars.forEach((bar, i) => {
    const haClose = (Number(bar.open) + Number(bar.high) + Number(bar.low) + Number(bar.close)) / 4;
    const haOpen = i === 0
      ? (Number(bar.open) + Number(bar.close)) / 2
      : (out[i - 1].open + out[i - 1].close) / 2;
    const haHigh = Math.max(Number(bar.high), haOpen, haClose);
    const haLow = Math.min(Number(bar.low), haOpen, haClose);
    out.push({
      time: bar.time,
      open: haOpen,
      high: haHigh,
      low: haLow,
      close: haClose,
    });
  });
  return out;
}

function computeSeriesEmaFromBars(bars, period) {
  const values = bars.map((b) => Number(b.close));
  const ema = computeEma(values, period);
  return bars.map((b, i) => ({ time: b.time, value: ema[i] }));
}

function normalizeBarsForCandleType(bars, candleType) {
  return candleType === "heikin" ? buildHeikinAshiBars(bars) : bars;
}

function getLiveCandleType() {
  const checked = document.querySelector("#live-candle-type label.checked");
  return checked ? checked.dataset.val : "japanese";
}

function getCtCandleType() {
  const checked = document.querySelector("#ct-candle-type label.checked");
  return checked ? checked.dataset.val : "japanese";
}

function getStrategyEmaPeriods() {
  return {
    fast: parseInt(document.getElementById("p-ema-fast").value, 10) || 7,
    slow: parseInt(document.getElementById("p-ema-slow").value, 10) || 133,
  };
}

function chartBarsForTf(tf) {
  const hours = 100;
  if (tf === "4m") return Math.round(hours * 15);
  if (tf === "8m") return Math.round(hours * 7.5);
  if (tf === "1h") return hours;
  return 300;
}

async function refreshLiveEmaOverlay() {
  try {
    const { fast, slow } = getStrategyEmaPeriods();
    const candleType = getLiveCandleType();

    const ensureTf = async (tf) => {
      if (liveLastCandlesByTf[tf] && liveLastCandlesByTf[tf].length) return liveLastCandlesByTf[tf];
      const data = await apiGet(`/api/candles?symbol=${currentSymbol}&tf=${tf}&n=${chartBarsForTf(tf)}`);
      const bars = data.candles.map((c) => ({
        time: toUnixTime(c.time),
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }));
      liveLastCandlesByTf[tf] = bars;
      return bars;
    };

    const bars4m = normalizeBarsForCandleType(await ensureTf("4m"), candleType);
    const bars8m = normalizeBarsForCandleType(await ensureTf("8m"), candleType);

    liveEmaFast1m.setData(computeSeriesEmaFromBars(bars4m, fast));
    liveEmaSlow1m.setData(computeSeriesEmaFromBars(bars4m, slow));
    liveEmaFast5m.setData(computeSeriesEmaFromBars(bars8m, fast));
    liveEmaSlow5m.setData(computeSeriesEmaFromBars(bars8m, slow));
  } catch (e) {
    console.error("refreshLiveEmaOverlay failed", e);
  }
}

async function refreshCandles() {
  try {
    const data = await apiGet(`/api/candles?symbol=${currentSymbol}&tf=${currentTf}&n=${chartBarsForTf(currentTf)}`);
    const rawBars = data.candles.map((c) => ({
      time: toUnixTime(c.time),
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    liveLastCandlesByTf[currentTf] = rawBars;
    const bars = normalizeBarsForCandleType(rawBars, getLiveCandleType());
    liveCandleSeries.setData(bars);

    const lastClose = bars.length ? bars[bars.length - 1].close : null;
    const precision = (lastClose !== null && lastClose < 1) ? 4 : 2;
    const minMove = precision === 4 ? 0.0001 : 0.01;
    liveCandleSeries.applyOptions({ priceFormat: { type: "price", precision, minMove } });

    refreshLiveEmaOverlay();
    refreshLiveTradeMarkers();
  } catch (e) {
    console.error("refreshCandles failed", e);
  }
}

async function refreshLiveTradeMarkers() {
  try {
    const [allTrades, openPositions] = await Promise.all([
      apiGet("/api/trade_log?limit=1000"),
      apiGet("/api/open_positions"),
    ]);

    const closedForSymbol = allTrades.filter((t) => t.symbol === currentSymbol);
    const openForSymbol = openPositions
      .filter((p) => p.symbol === currentSymbol)
      .map((p) => ({
        symbol: p.symbol,
        direction: p.direction,
        entry_time: p.entry_time,
        entry_price: p.entry_price,
        exit_time: null,
        exit_price: null,
        pnl_pct: null,
        exit_reason: null,
      }));

    const combined = [...closedForSymbol, ...openForSymbol];
    if (!combined.length) {
      liveCandleSeries.setMarkers([]);
      return;
    }

    const numbered = numberTrades(combined);
    const markers = [];
    numbered.forEach((t) => {
      markers.push({
        time: toUnixTime(t.entry_time),
        position: t.direction === "long" ? "belowBar" : "aboveBar",
        color: t.direction === "long" ? "#26a269" : "#d64545",
        shape: t.direction === "long" ? "arrowUp" : "arrowDown",
        text: `#${t.tradeNum} ${t.direction === "long" ? "LONG" : "SHORT"} @ ${Number(t.entry_price).toFixed(2)}`,
      });

      if (!t.exit_time) return;

      const pnlPct = Number(t.pnl_pct);
      const isProfit = Number.isFinite(pnlPct) ? pnlPct >= 0 : (t.exit_reason === "TP");
      const pnlColor = isProfit ? "#26a269" : "#d64545";
      const pnlText = Number.isFinite(pnlPct)
        ? `#${t.tradeNum} ${t.exit_reason} ${isProfit ? "+" : ""}${pnlPct.toFixed(2)}%`
        : `#${t.tradeNum} ${t.exit_reason} @ ${Number(t.exit_price).toFixed(2)}`;

      markers.push({
        time: toUnixTime(t.exit_time),
        position: t.direction === "long" ? "aboveBar" : "belowBar",
        color: pnlColor,
        shape: "circle",
        text: pnlText,
      });
    });
    markers.sort((a, b) => a.time - b.time);
    liveCandleSeries.setMarkers(markers);
  } catch (e) {
    console.error("refreshLiveTradeMarkers failed", e);
  }
}

document.getElementById("symbol-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-symbol]");
  if (!btn) return;
  document.querySelectorAll("#symbol-tabs button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  currentSymbol = btn.dataset.symbol;
  refreshCandles();
});

document.getElementById("tf-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tf]");
  if (!btn) return;
  document.querySelectorAll("#tf-tabs button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  currentTf = btn.dataset.tf;
  refreshCandles();
});

// --- direction radio for live params ---
document.getElementById("p-direction").addEventListener("click", (e) => {
  const label = e.target.closest("label[data-val]");
  if (!label) return;
  document.querySelectorAll("#p-direction label").forEach((l) => l.classList.remove("checked"));
  label.classList.add("checked");
});

function getLiveDirection() {
  const checked = document.querySelector("#p-direction label.checked");
  return checked ? checked.dataset.val : "both";
}

// --- Start / Stop ---
document.getElementById("btn-start").addEventListener("click", async () => {
  await apiPost("/api/start", {});
  showToast("Движок запущен. Новые сделки будут открываться.");
  refreshStatus();
});

document.getElementById("btn-stop").addEventListener("click", async () => {
  await apiPost("/api/stop", {});
  showToast("Движок остановлен. Открытые позиции всё равно будут закрыты по TP/SL.");
  refreshStatus();
});

// --- Save params ---
document.getElementById("btn-save-params").addEventListener("click", async () => {
  const params = {
    ema_fast: parseInt(document.getElementById("p-ema-fast").value, 10),
    ema_slow: parseInt(document.getElementById("p-ema-slow").value, 10),
    tp_pct: parseFloat(document.getElementById("p-tp").value),
    sl_pct: parseFloat(document.getElementById("p-sl").value),
    direction: getLiveDirection(),
    margin_per_trade: parseFloat(document.getElementById("p-margin").value),
    leverage: parseFloat(document.getElementById("p-leverage").value),
    max_open_positions: parseInt(document.getElementById("p-max-open").value, 10),
  };
  await apiPost("/api/params", params);
  showToast("Параметры сохранены.");
});

// --- Status polling ---
async function refreshStatus() {
  try {
    const state = await apiGet("/api/status");
    const pill = document.getElementById("status-pill");
    const text = document.getElementById("status-text");

    if (state.running) {
      pill.classList.remove("stopped");
      pill.classList.add("running");
      text.textContent = "Работает";
      document.getElementById("btn-start").disabled = true;
      document.getElementById("btn-stop").disabled = false;
    } else {
      pill.classList.remove("running");
      pill.classList.add("stopped");
      text.textContent = "Остановлено";
      document.getElementById("btn-start").disabled = false;
      document.getElementById("btn-stop").disabled = true;
    }

    const p = state.params || {};
    if (p.ema_fast !== undefined) document.getElementById("p-ema-fast").value = p.ema_fast;
    if (p.ema_slow !== undefined) document.getElementById("p-ema-slow").value = p.ema_slow;
    if (p.tp_pct !== undefined) document.getElementById("p-tp").value = p.tp_pct;
    if (p.sl_pct !== undefined) document.getElementById("p-sl").value = p.sl_pct;
    if (p.margin_per_trade !== undefined) document.getElementById("p-margin").value = p.margin_per_trade;
    if (p.leverage !== undefined) document.getElementById("p-leverage").value = p.leverage;
    if (p.max_open_positions !== undefined) document.getElementById("p-max-open").value = p.max_open_positions;
    if (p.direction) {
      document.querySelectorAll("#p-direction label").forEach((l) => {
        l.classList.toggle("checked", l.dataset.val === p.direction);
      });
    }

    const pnlEl = document.getElementById("stat-pnl");
    if (pnlEl && state.net_pnl_after_fees_usdt !== undefined) {
      pnlEl.textContent = fmtUsd(state.net_pnl_after_fees_usdt);
      pnlEl.className = "value " + (state.net_pnl_after_fees_usdt >= 0 ? "pos" : "neg");
    }
    const feeGrossEl = document.getElementById("stat-fee-gross");
    if (feeGrossEl && state.fee_gross_usdt !== undefined) {
      feeGrossEl.textContent = fmtUsd(state.fee_gross_usdt);
    }
    const rebateEl = document.getElementById("stat-rebate");
    if (rebateEl && state.rebate_usdt !== undefined) {
      rebateEl.textContent = fmtUsd(state.rebate_usdt);
    }
  } catch (e) {
    console.error("refreshStatus failed", e);
  }
}

// --- Live exchange positions table (real Bybit demo account data) ---
async function refreshLivePositions() {
  try {
    const data = await apiGet("/api/live_positions");
    const body = document.getElementById("live-positions-body");
    const badge = document.getElementById("live-balance-badge");

    if (!data.enabled) {
      badge.textContent = "Не подключено";
      badge.className = "balance-badge balance-off";
      body.innerHTML = `<tr class="empty-row"><td colspan="11">Bybit demo API-ключи не настроены (core/bybit_keys.py)</td></tr>`;
      return;
    }

    const equityEl = document.getElementById("stat-equity");
    if (data.balance && data.balance.total_equity !== null) {
      badge.textContent = `Equity: ${Number(data.balance.total_equity).toFixed(2)} USDT`;
      badge.className = "balance-badge balance-on";
      if (equityEl) equityEl.textContent = fmtUsd(data.balance.total_equity);
    } else {
      badge.textContent = "Подключено";
      badge.className = "balance-badge balance-on";
      if (equityEl) equityEl.textContent = "—";
    }

    if (!data.positions || !data.positions.length) {
      body.innerHTML = `<tr class="empty-row"><td colspan="11">Нет открытых позиций на бирже</td></tr>`;
      return;
    }

    body.innerHTML = data.positions.map((p) => `
      <tr>
        <td>${p.symbol}</td>
        <td class="${p.direction === 'long' ? 'pos-long' : 'pos-short'}">${p.direction.toUpperCase()}</td>
        <td>${Number(p.size).toFixed(4)}</td>
        <td>${Number(p.leverage).toFixed(0)}x</td>
        <td>${Number(p.margin).toFixed(2)}</td>
        <td>${Number(p.notional).toFixed(2)}</td>
        <td>${Number(p.entry_price).toFixed(4)}</td>
        <td>${Number(p.mark_price).toFixed(4)}</td>
        <td>${p.take_profit ? Number(p.take_profit).toFixed(4) : '-'}</td>
        <td>${p.stop_loss ? Number(p.stop_loss).toFixed(4) : '-'}</td>
        <td class="${Number(p.unrealized_pnl) >= 0 ? 'pos-long' : 'pos-short'}">${Number(p.unrealized_pnl).toFixed(2)}</td>
      </tr>`).join("");
  } catch (e) {
    console.error("refreshLivePositions failed", e);
  }
}

// --- Open positions table ---
async function refreshOpenPositions() {
  try {
    const positions = await apiGet("/api/open_positions");
    const body = document.getElementById("open-positions-body");

    if (!positions.length) {
      body.innerHTML = `<tr class="empty-row"><td colspan="8">Нет открытых позиций</td></tr>`;
      return;
    }

    body.innerHTML = positions.map((p) => {
      let syncCell;
      if (p.exchange_synced === true) {
        syncCell = `<span style="color:#4caf50;" title="Ордер подтверждён на бирже">✓ на бирже</span>`;
      } else if (p.exchange_synced === false) {
        const errText = (p.exchange_error || "ошибка").replace(/"/g, "&quot;");
        syncCell = `<span style="color:#e05252; cursor:help;" title="${errText}">✗ только в дашборде</span>`;
      } else {
        syncCell = `<span style="color:#8b93a3;">—</span>`;
      }
      return `
      <tr>
        <td>${p.symbol}</td>
        <td class="${p.direction === 'long' ? 'pos-long' : 'pos-short'}">${p.direction.toUpperCase()}</td>
        <td>${fmtTime(p.entry_time)}</td>
        <td>${Number(p.entry_price).toFixed(4)}</td>
        <td>${Number(p.tp_pct).toFixed(2)}%</td>
        <td>${Number(p.sl_pct).toFixed(2)}%</td>
        <td>${Number(p.notional).toFixed(0)}</td>
        <td>${syncCell}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    console.error("refreshOpenPositions failed", e);
  }
}

// --- Trade log table ---
async function refreshTradeLog() {
  try {
    const trades = await apiGet("/api/trade_log?limit=300");
    const body = document.getElementById("trade-log-body");

    if (!trades.length) {
      body.innerHTML = `<tr class="empty-row"><td colspan="9">Пока нет закрытых сделок</td></tr>`;
      return;
    }

    const rows = trades.slice().reverse().map((t) => `
      <tr>
        <td>${t.symbol}</td>
        <td class="${t.direction === 'long' ? 'pos-long' : 'pos-short'}">${(t.direction || '').toUpperCase()}</td>
        <td>${fmtTime(t.exit_time)}</td>
        <td>${Number(t.entry_price).toFixed(4)}</td>
        <td>${Number(t.exit_price).toFixed(4)}</td>
        <td><span class="badge ${t.exit_reason === 'TP' ? 'tp' : 'sl'}">${t.exit_reason}</span></td>
        <td class="${pnlClass(t.pnl_pct)}">${fmtPct(t.pnl_pct)}</td>
        <td class="${pnlClass(t.pnl_usdt)}">${fmtUsd(t.pnl_usdt)}</td>
        <td>${fmtTime(t.closed_at)}</td>
      </tr>`).join("");
    body.innerHTML = rows;

    // Equity and Net PnL must have one source of truth only:
    // - stat-equity is set from Bybit totalEquity in refreshLivePositions()
    // - stat-pnl is set from fee-aware /api/status in refreshStatus().
    // Do not overwrite either with local paper equity-curve data here.
  } catch (e) {
    console.error("refreshTradeLog failed", e);
  }
}

// --- polling loop ---
function startLivePolling() {
  refreshStatus();
  refreshCandles();
  refreshOpenPositions();
  refreshTradeLog();
  refreshLivePositions();

  setInterval(refreshStatus, 5000);
  setInterval(refreshCandles, 5000);
  setInterval(refreshOpenPositions, 5000);
  setInterval(refreshLivePositions, 60000); // exchange data -- once a minute is enough
  setInterval(refreshTradeLog, 8000);
}

// ---------------------------------------------------------------------
// 4. BACKTEST TAB
// ---------------------------------------------------------------------

let btChart = null;
let btLineSeries = null;

function initBacktestChart() {
  const container = document.getElementById("bt-chart-container");
  btChart = LightweightCharts.createChart(container, {
    layout: { background: { color: "#161b24" }, textColor: "#8b93a3" },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.04)" },
      horzLines: { color: "rgba(255,255,255,0.04)" },
    },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
    timeScale: { borderColor: "rgba(255,255,255,0.08)", timeVisible: true, secondsVisible: false },
    width: container.clientWidth,
    height: 300,
  });
  btLineSeries = btChart.addAreaSeries({
    lineColor: "#4f9dff",
    topColor: "rgba(79,157,255,0.25)",
    bottomColor: "rgba(79,157,255,0.02)",
    lineWidth: 2,
  });

  window.addEventListener("resize", () => {
    btChart.applyOptions({ width: container.clientWidth });
  });
}

["bt-symbols", "bt-direction"].forEach((id) => {
  document.getElementById(id).addEventListener("click", (e) => {
    const label = e.target.closest("label[data-val]");
    if (!label) return;

    if (id === "bt-symbols") {
      // multi-select toggle
      label.classList.toggle("checked");
    } else {
      // single-select radio
      document.querySelectorAll(`#${id} label`).forEach((l) => l.classList.remove("checked"));
      label.classList.add("checked");
    }
  });
});

function getBacktestSymbols() {
  return Array.from(document.querySelectorAll("#bt-symbols label.checked")).map((l) => l.dataset.val);
}

function getBacktestDirection() {
  const checked = document.querySelector("#bt-direction label.checked");
  return checked ? checked.dataset.val : "both";
}

document.getElementById("backtest-run-btn").addEventListener("click", async () => {
  const symbols = getBacktestSymbols();
  if (!symbols.length) {
    showToast("Выберите хотя бы один символ.");
    return;
  }

  const body = {
    symbols,
    ema_fast: parseInt(document.getElementById("bt-ema-fast").value, 10),
    ema_slow: parseInt(document.getElementById("bt-ema-slow").value, 10),
    tp_pct: parseFloat(document.getElementById("bt-tp").value),
    sl_pct: parseFloat(document.getElementById("bt-sl").value),
    direction: getBacktestDirection(),
    initial_equity: parseFloat(document.getElementById("bt-initial-equity").value),
    margin_per_trade: parseFloat(document.getElementById("bt-margin").value),
    leverage: parseFloat(document.getElementById("bt-leverage").value),
    max_open_positions: parseInt(document.getElementById("bt-max-open").value, 10),
    days: parseInt(document.getElementById("bt-days").value, 10),
  };

  const btn = document.getElementById("backtest-run-btn");
  btn.disabled = true;
  document.getElementById("bt-loading").style.display = "flex";
  document.getElementById("bt-results").style.display = "none";

  const loadingText = document.getElementById("bt-loading-text");
  loadingText.textContent = "Считаю бэктест...";
  const progressTimer = setInterval(async () => {
    try {
      const p = await apiGet("/api/backtest_progress");
      if (p.stage === "downloading" && p.total > 0) {
        loadingText.textContent = `Загружаю историю: ${p.done}/${p.total} (${p.current})`;
      } else if (p.stage === "computing") {
        loadingText.textContent = "Считаю сигналы и сделки...";
      }
    } catch (e) { /* тихо игнорируем сбои опроса */ }
  }, 400);

  try {
    const result = await apiPost("/api/backtest", body);
    renderBacktestResult(result, body.initial_equity);
    showToast("Бэктест завершён.");
  } catch (e) {
    console.error(e);
    showToast("Ошибка бэктеста. См. консоль.");
  } finally {
    clearInterval(progressTimer);
    btn.disabled = false;
    document.getElementById("bt-loading").style.display = "none";
  }
});

function renderBacktestResult(result, initialEquity) {
  const s = result.summary;
  document.getElementById("bt-results").style.display = "block";

  const pnlEl = document.getElementById("bt-stat-pnl");
  pnlEl.textContent = fmtUsd(s.net_pnl_usdt);
  pnlEl.className = "value " + (s.net_pnl_usdt >= 0 ? "pos" : "neg");

  const retEl = document.getElementById("bt-stat-return");
  retEl.textContent = fmtPct(s.return_pct);
  retEl.className = "value " + (s.return_pct >= 0 ? "pos" : "neg");

  document.getElementById("bt-stat-winrate").textContent = s.winrate_pct.toFixed(1) + "%";
  document.getElementById("bt-stat-pf").textContent = Number.isFinite(s.profit_factor) ? s.profit_factor.toFixed(2) : "∞";

  const ddEl = document.getElementById("bt-stat-dd");
  ddEl.textContent = s.max_drawdown_pct.toFixed(2) + "%";
  ddEl.className = "value neg";

  document.getElementById("bt-stat-trades").textContent = s.closed_trades;

  if (!btChart) initBacktestChart();
  const points = result.equity_curve.map((p) => ({
    time: Math.floor(new Date(p.time).getTime() / 1000),
    value: p.equity,
  }));
  // Lightweight Charts requires strictly increasing, unique, ascending
  // timestamps. Sort explicitly first -- the server's equity_curve is
  // normally ordered already, but the downsampled array must still be
  // defensively sorted before dedup so a single out-of-order point
  // (e.g. from a resumed/backfilled history fetch) can't make the
  // series render as a flat/garbled line.
  const sortedPoints = [...points].sort((a, b) => a.time - b.time);
  const seen = new Set();
  const cleanPoints = sortedPoints.filter((p) => {
    if (seen.has(p.time)) return false;
    seen.add(p.time);
    return true;
  });
  btLineSeries.setData(cleanPoints);
  btChart.timeScale().fitContent();
  // Force the price (Y) scale to recompute against the real equity
  // range. Without this, a narrow-range series loaded after a
  // wide-range one can keep the old scale and look flat.
  btChart.priceScale("right").applyOptions({ autoScale: true });

  if (s.data_start && s.data_end) {
    const rangeEl = document.getElementById("bt-data-range");
    if (rangeEl) {
      rangeEl.textContent = `Данные: ${fmtTime(s.data_start)} — ${fmtTime(s.data_end)}`;
    }
  }

  const tradesBody = document.getElementById("bt-trades-body");
  const lastTrades = result.trades.slice(-200).reverse();
  if (!lastTrades.length) {
    tradesBody.innerHTML = `<tr class="empty-row"><td colspan="7">Сделок нет</td></tr>`;
  } else {
    tradesBody.innerHTML = lastTrades.map((t) => `
      <tr>
        <td>${t.symbol}</td>
        <td class="${t.direction === 'long' ? 'pos-long' : 'pos-short'}">${t.direction.toUpperCase()}</td>
        <td>${fmtTime(t.entry_time)}</td>
        <td>${fmtTime(t.exit_time)}</td>
        <td><span class="badge ${t.exit_reason === 'TP' ? 'tp' : 'sl'}">${t.exit_reason}</span></td>
        <td class="${pnlClass(t.pnl_pct)}">${fmtPct(t.pnl_pct)}</td>
        <td class="${pnlClass(t.pnl_usdt)}">${fmtUsd(t.pnl_usdt)}</td>
      </tr>`).join("");
  }
}

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------

// ---------------------------------------------------------------------
// 5. CHART TAB (candles + 4 EMAs on 1m / 2 EMAs on 5m + trade markers)
// ---------------------------------------------------------------------

let ctChart = null;
let ctCandleSeries = null;
let ctEmaFast1m = null;
let ctEmaSlow1m = null;
let ctEmaFast5m = null;
let ctEmaSlow5m = null;
let ctLastData = null; // last /api/backtest_chart response, cached for scroll nav
let ctBarSpacing = 6;
let ctTabActive = false;
let ctAutoRefreshTimer = null;
let ctTradeLevelSeries = [];
let ctSelectedTradeEntryIso = null; // short entry/exit price-level segments, redrawn per trade batch
const TRADE_LEVEL_BARS = 6;  // width of each price-level segment, in bars either side of the marker time
const CT_TF_SECONDS = { "4m": 240, "8m": 480, "1h": 3600 };

function initChartTab() {
  const container = document.getElementById("ct-chart-container");
  ctChart = LightweightCharts.createChart(container, {
    layout: { background: { color: "#161b24" }, textColor: "#8b93a3" },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.04)" },
      horzLines: { color: "rgba(255,255,255,0.04)" },
    },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
    timeScale: { borderColor: "rgba(255,255,255,0.08)", timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    width: container.clientWidth,
    height: 520,
  });

  ctCandleSeries = ctChart.addCandlestickSeries({
    upColor: "#26a269", downColor: "#d64545", borderVisible: false,
    wickUpColor: "#26a269", wickDownColor: "#d64545",
  });

  ctEmaFast1m = ctChart.addLineSeries({ color: "#4f9dff", lineWidth: 1, title: "EMA fast (4m HA)" });
  ctEmaSlow1m = ctChart.addLineSeries({ color: "#a259ff", lineWidth: 1, title: "EMA slow (4m HA)" });
  ctEmaFast5m = ctChart.addLineSeries({ color: "#ffb84f", lineWidth: 2, title: "EMA fast (8m HA)" });
  ctEmaSlow5m = ctChart.addLineSeries({ color: "#ff5f8f", lineWidth: 2, title: "EMA slow (8m HA)" });

  document.getElementById("ct-legend").innerHTML = `
    <span style="color:#4f9dff;">─ EMA fast 4m HA</span>
    <span style="color:#a259ff;">─ EMA slow 4m HA</span>
    <span style="color:#ffb84f;">─ EMA fast 8m HA</span>
    <span style="color:#ff5f8f;">─ EMA slow 8m HA</span>
    <span style="color:#26a269;">▲ вход</span>
    <span style="color:#d64545;">▼ выход</span>`;

  window.addEventListener("resize", () => {
    ctChart.applyOptions({ width: container.clientWidth });
  });
}

["ct-symbols", "ct-tf", "ct-direction", "ct-candle-type"].forEach((id) => {
  document.getElementById(id).addEventListener("click", (e) => {
    const label = e.target.closest("label[data-val]");
    if (!label) return;
    document.querySelectorAll(`#${id} label`).forEach((l) => l.classList.remove("checked"));
    label.classList.add("checked");
    if ((id === "ct-tf" || id === "ct-candle-type") && ctLastData) renderChartTabTimeframe();
  });
});

function getCtSymbol() {
  const checked = document.querySelector("#ct-symbols label.checked");
  return checked ? checked.dataset.val : "BTCUSDT";
}

// Keep the chart's right price scale readable and close to exchange quoting:
// majors use cents; altcoins show up to four decimal places.
function getCtPriceFormat() {
  const symbol = getCtSymbol();
  const precision = (symbol === "BTCUSDT" || symbol === "ETHUSDT") ? 2 : 4;
  return {
    type: "price",
    precision,
    minMove: 1 / (10 ** precision),
  };
}

function formatCtPrice(price) {
  return Number(price).toFixed(getCtPriceFormat().precision);
}

function applyCtPriceFormat() {
  const priceFormat = getCtPriceFormat();

  [ctCandleSeries, ctEmaFast1m, ctEmaSlow1m, ctEmaFast5m, ctEmaSlow5m]
    .filter(Boolean)
    .forEach((series) => series.applyOptions({ priceFormat }));

  ctTradeLevelSeries.forEach((series) => series.applyOptions({ priceFormat }));
}
function getCtTf() {
  const checked = document.querySelector("#ct-tf label.checked");
  return checked ? checked.dataset.val : "4m";
}
function getCtDirection() {
  const checked = document.querySelector("#ct-direction label.checked");
  return checked ? checked.dataset.val : "both";
}

document.querySelectorAll("#live-candle-type label").forEach((label) => {
  label.addEventListener("click", () => {
    document.querySelectorAll("#live-candle-type label").forEach((l) => l.classList.remove("checked"));
    label.classList.add("checked");
    if (liveChart) refreshCandles();
  });
});

document.getElementById("ct-load-btn").addEventListener("click", loadChartTabData);

async function loadChartTabData() {
  if (!ctChart) initChartTab();

  const params = new URLSearchParams({
    symbol: getCtSymbol(),
    ema_fast: document.getElementById("ct-ema-fast").value,
    ema_slow: document.getElementById("ct-ema-slow").value,
    tp_pct: document.getElementById("ct-tp").value,
    sl_pct: document.getElementById("ct-sl").value,
    direction: getCtDirection(),
    days: document.getElementById("ct-days").value,
  });

  document.getElementById("ct-loading").style.display = "flex";
  try {
    const data = await apiGet(`/api/backtest_chart?${params.toString()}`);
    ctLastData = data;
    renderChartTabTimeframe({ fitContent: true });
    renderChartTabTrades(data.trades);
    showToast(`Загружено: ${data.candles_4m.length} свечей 4m, ${data.trades.length} сделок`);
    startChartAutoRefresh();
  } catch (e) {
    console.error("loadChartTabData failed", e);
    showToast("Ошибка загрузки графика. См. консоль.");
  } finally {
    document.getElementById("ct-loading").style.display = "none";
  }
}

// Re-fetches the same symbol/params from the server (which auto-gap-fills
// any minutes missed while the dashboard was closed/asleep -- see
// ensure_backtest_history()'s stale-check) and re-renders WITHOUT resetting
// the user's current zoom/scroll position, so live catch-up feels like new
// candles quietly appearing rather than the chart jumping around.
async function refreshChartTabData(isTabReopen = false) {
  if (!ctChart) return;
  const params = new URLSearchParams({
    symbol: getCtSymbol(),
    ema_fast: document.getElementById("ct-ema-fast").value,
    ema_slow: document.getElementById("ct-ema-slow").value,
    tp_pct: document.getElementById("ct-tp").value,
    sl_pct: document.getElementById("ct-sl").value,
    direction: getCtDirection(),
    days: document.getElementById("ct-days").value,
  });
  const preservedRange = ctChart.timeScale().getVisibleRange();
  try {
    const data = await apiGet(`/api/backtest_chart?${params.toString()}`);
    ctLastData = data;
    renderChartTabTimeframe({ fitContent: false });
    renderChartTabTrades(data.trades);
    if (preservedRange) {
      ctChart.timeScale().setVisibleRange(preservedRange);
    }
    if (isTabReopen) {
      showToast(`Данные обновлены до текущей минуты`);
    }
  } catch (e) {
    console.error("refreshChartTabData failed", e);
  }
}

function startChartAutoRefresh() {
  if (ctAutoRefreshTimer) return;
  // 60s cadence matches how often a fresh 1m candle can even appear;
  // only refreshes while the Chart tab is the one currently visible.
  ctAutoRefreshTimer = setInterval(() => {
    if (ctTabActive) refreshChartTabData(false);
  }, 60000);
}

function toUnixTime(iso) {
  const utcSeconds = Math.floor(new Date(iso).getTime() / 1000);
  // getTimezoneOffset() is minutes to ADD to local time to get UTC, so we
  // subtract it (i.e. add the negated value) to go from UTC -> local wall
  // clock, matching what Lightweight Charts will then render as "UTC".
  const localOffsetSeconds = new Date().getTimezoneOffset() * -60;
  return utcSeconds + localOffsetSeconds;
}

function dedupSorted(rows, timeKey = "time") {
  const sorted = [...rows].sort((a, b) => a[timeKey] - b[timeKey]);
  const seen = new Set();
  return sorted.filter((r) => {
    if (seen.has(r[timeKey])) return false;
    seen.add(r[timeKey]);
    return true;
  });
}

function fmtHmFromUnix(ts) {
  const d = new Date(ts * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

function find8mConfirmationTimeForTrade(trade) {
  if (!trade || !ctLastData || !trade.entry_time) return null;
  const rows8m = ctLastData.candles_8m || [];
  if (!rows8m.length) return null;

  const entryTs = toUnixTime(trade.entry_time);
  const isLong = (trade.direction || "").toLowerCase() === "long";
  const isShort = (trade.direction || "").toLowerCase() === "short";

  let lastConfirmTs = null;
  for (const row of rows8m) {
    const ts = toUnixTime(row.time);
    if (ts > entryTs) break;
    if (isLong && row.cross_up) lastConfirmTs = ts;
    if (isShort && row.cross_down) lastConfirmTs = ts;
  }
  return lastConfirmTs;
}

function renderChartTabTimeframe(opts = {}) {
  if (!ctLastData) return;
  const fitContent = opts.fitContent !== false;
  const tf = getCtTf();
  const candleType = getCtCandleType();
  const emaFastPeriod = parseInt(document.getElementById("ct-ema-fast").value, 10) || 7;
  const emaSlowPeriod = parseInt(document.getElementById("ct-ema-slow").value, 10) || 133;
  const key = tf === "4m" ? "candles_4m" : tf === "8m" ? "candles_8m" : "candles_1h";
  const rows = ctLastData[key] || [];

  const rawBars = rows.map((c) => ({
    time: toUnixTime(c.time), open: c.open, high: c.high, low: c.low, close: c.close,
  }));
  const bars = normalizeBarsForCandleType(rawBars, candleType);
  applyCtPriceFormat();
  ctCandleSeries.setData(dedupSorted(bars));

  if (candleType === "heikin") {
    if (tf === "4m") {
      const rawBars4m = (ctLastData["candles_4m"] || []).map((c) => ({
        time: toUnixTime(c.time), open: c.open, high: c.high, low: c.low, close: c.close,
      }));
      const rawBars8m = (ctLastData["candles_8m"] || []).map((c) => ({
        time: toUnixTime(c.time), open: c.open, high: c.high, low: c.low, close: c.close,
      }));
      const haBars4m = buildHeikinAshiBars(rawBars4m);
      const haBars8m = buildHeikinAshiBars(rawBars8m);
      ctEmaFast1m.setData(dedupSorted(computeSeriesEmaFromBars(haBars4m, emaFastPeriod)));
      ctEmaSlow1m.setData(dedupSorted(computeSeriesEmaFromBars(haBars4m, emaSlowPeriod)));
      ctEmaFast5m.setData(dedupSorted(computeSeriesEmaFromBars(haBars8m, emaFastPeriod)));
      ctEmaSlow5m.setData(dedupSorted(computeSeriesEmaFromBars(haBars8m, emaSlowPeriod)));
    } else if (tf === "8m") {
      const haBars8m = buildHeikinAshiBars(rawBars);
      ctEmaFast1m.setData([]);
      ctEmaSlow1m.setData([]);
      ctEmaFast5m.setData(dedupSorted(computeSeriesEmaFromBars(haBars8m, emaFastPeriod)));
      ctEmaSlow5m.setData(dedupSorted(computeSeriesEmaFromBars(haBars8m, emaSlowPeriod)));
    } else {
      ctEmaFast1m.setData([]);
      ctEmaSlow1m.setData([]);
      ctEmaFast5m.setData([]);
      ctEmaSlow5m.setData([]);
    }
  } else if (tf === "4m") {
    ctEmaFast1m.setData(dedupSorted(rows.filter((c) => c.ema_fast != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_fast }))));
    ctEmaSlow1m.setData(dedupSorted(rows.filter((c) => c.ema_slow != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_slow }))));
    ctEmaFast5m.setData(dedupSorted(rows.filter((c) => c.ema_fast_8m != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_fast_8m }))));
    ctEmaSlow5m.setData(dedupSorted(rows.filter((c) => c.ema_slow_8m != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_slow_8m }))));
  } else if (tf === "8m") {
    ctEmaFast1m.setData([]);
    ctEmaSlow1m.setData([]);
    ctEmaFast5m.setData(dedupSorted(rows.filter((c) => c.ema_fast != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_fast }))));
    ctEmaSlow5m.setData(dedupSorted(rows.filter((c) => c.ema_slow != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_slow }))));
  } else {
    ctEmaFast1m.setData([]);
    ctEmaSlow1m.setData([]);
    ctEmaFast5m.setData([]);
    ctEmaSlow5m.setData([]);
  }

  if (tf === "4m" || tf === "8m") {
    setTradeMarkers(ctLastData.trades || []);
    renderTradeLevels(ctLastData.trades || [], tf);
  } else {
    ctCandleSeries.setMarkers([]);
    renderTradeLevels([], tf);
  }

  if (ctLastData.data_start && ctLastData.data_end) {
    document.getElementById("ct-data-range").textContent =
      `Данные: ${fmtTime(ctLastData.data_start)} — ${fmtTime(ctLastData.data_end)} · таймфрейм: ${tf}`;
  }

  if (fitContent) {
    ctChart.timeScale().fitContent();
  }
}

function numberTrades(trades) {
  return [...trades]
    .sort((a, b) => new Date(a.entry_time) - new Date(b.entry_time))
    .map((t, i) => ({ ...t, tradeNum: i + 1 }));
}

function buildSignalValidationMarkers(rows, tf, selectedEntryIso = null) {
  if (tf !== "4m" || !rows || !rows.length || !selectedEntryIso) return [];

  const targetTime = toUnixTime(selectedEntryIso);
  const targetIdx = rows.findIndex((c) => toUnixTime(c.time) >= targetTime);
  if (targetIdx === -1) return [];

  const fromIdx = Math.max(0, targetIdx - 8);
  const toIdx = Math.min(rows.length - 1, targetIdx + 8);

  const markers = [];
  for (let i = fromIdx; i <= toIdx; i += 1) {
    const row = rows[i];
    const time = toUnixTime(row.time);
    const crossUp = !!row.cross_up;
    const crossDown = !!row.cross_down;
    const confirmUp8m = !!row.cross_up_8m;
    const confirmDown8m = !!row.cross_down_8m;
    const longSignal = !!row.long_signal;
    const shortSignal = !!row.short_signal;

    if (!(crossUp || crossDown || longSignal || shortSignal)) continue;

    const isLong = longSignal || crossUp;
    const confirmed = isLong ? confirmUp8m : confirmDown8m;

    if (crossUp || crossDown) {
      markers.push({
        time,
        position: isLong ? "belowBar" : "aboveBar",
        color: "#60a5fa",
        shape: isLong ? "arrowUp" : "arrowDown",
        text: "4m ✓",
      });

      markers.push({
        time,
        position: isLong ? "inBar" : "inBar",
        color: confirmed ? "#22c55e" : "#ef4444",
        shape: "circle",
        text: confirmed ? "8m ✓" : "×",
      });
    }

    if (longSignal || shortSignal) {
      markers.push({
        time,
        position: isLong ? "belowBar" : "aboveBar",
        color: "#f59e0b",
        shape: "square",
        text: "SIGNAL",
      });
    }
  }

  return markers;
}

function setTradeMarkers(trades) {
  const numbered = numberTrades(trades);
  const markers = [];

  const tf = getCtTf();
  const rows = tf === "4m"
    ? (ctLastData?.candles_4m || [])
    : tf === "8m"
      ? (ctLastData?.candles_8m || [])
      : [];

  markers.push(...buildSignalValidationMarkers(rows, tf, ctSelectedTradeEntryIso));

  numbered.forEach((t) => {
    markers.push({
      time: toUnixTime(t.entry_time),
      position: t.direction === "long" ? "belowBar" : "aboveBar",
      color: t.direction === "long" ? "#26a269" : "#d64545",
      shape: t.direction === "long" ? "arrowUp" : "arrowDown",
      text: `#${t.tradeNum} ${t.direction === "long" ? "LONG" : "SHORT"}`,
    });

    const pnlPct = Number(t.pnl_pct);
    const isProfit = Number.isFinite(pnlPct) ? pnlPct >= 0 : (t.exit_reason === "TP");
    const pnlColor = isProfit ? "#26a269" : "#d64545";
    const pnlText = Number.isFinite(pnlPct)
      ? `#${t.tradeNum} ${t.exit_reason} ${isProfit ? "+" : ""}${pnlPct.toFixed(2)}%`
      : `#${t.tradeNum} ${t.exit_reason}`;

    markers.push({
      time: toUnixTime(t.exit_time),
      position: t.direction === "long" ? "aboveBar" : "belowBar",
      color: pnlColor,
      shape: "circle",
      text: pnlText,
    });
  });

  markers.sort((a, b) => {
    if (a.time !== b.time) return a.time - b.time;
    return String(a.text || "").localeCompare(String(b.text || ""));
  });
  ctCandleSeries.setMarkers(markers);
}

// Short horizontal price-level segments at the *exact* entry/exit price of
// each trade -- unlike the arrow markers above (which snap to bar high/low
// and can look visually misleading), these are drawn on the real price
// scale using a line series whose data only exists for a few bars either
// side of the event, so it reads as a short tick rather than a full-width
// line across the whole chart.
function renderTradeLevels(trades, tf) {
  // clear previous batch first -- otherwise segments pile up on every redraw
  ctTradeLevelSeries.forEach((s) => ctChart.removeSeries(s));
  ctTradeLevelSeries = [];

  const stepSec = CT_TF_SECONDS[tf] || 240;
  const halfSpan = TRADE_LEVEL_BARS * stepSec;

  function addSegment(unixTime, price, color, label, lineStyle = LightweightCharts.LineStyle.Solid, markerText = null) {
    const series = ctChart.addLineSeries({
      color,
      lineWidth: 2,
      lineStyle,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      title: label,
      priceFormat: getCtPriceFormat(),
    });
    series.setData([
      { time: unixTime - halfSpan, value: price },
      { time: unixTime + halfSpan, value: price },
    ]);
    series.setMarkers([{
      time: unixTime,
      position: "inBar",
      color,
      shape: "circle",
      text: markerText || formatCtPrice(price),
    }]);
    ctTradeLevelSeries.push(series);
  }

  trades.forEach((t) => {
    const entryTs = toUnixTime(t.entry_time);
    const entryPrice = Number(t.entry_price);
    const entryColor = t.direction === "long" ? "#26a269" : "#d64545";

    addSegment(
      entryTs,
      entryPrice,
      entryColor,
      `entry ${formatCtPrice(t.entry_price)}`
    );

    const confirmTs = find8mConfirmationTimeForTrade(t);
    if (confirmTs && Number.isFinite(entryPrice)) {
      addSegment(
        confirmTs,
        entryPrice,
        "#7dd3fc",
        `8m confirm since ${fmtHmFromUnix(confirmTs)}`,
        LightweightCharts.LineStyle.Dashed,
        `8m ${fmtHmFromUnix(confirmTs)}`
      );
    }

    const exitPrice = t.exit_price != null ? Number(t.exit_price) : (t.exit_reason === "TP" ? Number(t.take_price) : Number(t.stop_price));
    const exitColor = t.exit_reason === "TP" ? "#26a269" : "#d64545";
    if (Number.isFinite(exitPrice)) {
      addSegment(
        toUnixTime(t.exit_time),
        exitPrice,
        exitColor,
        `exit ${formatCtPrice(exitPrice)}`
      );
    }
  });
}

function renderChartTabTrades(trades) {
  const body = document.getElementById("ct-trades-body");
  if (!trades || !trades.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="5">Сделок нет в этом окне</td></tr>`;
    return;
  }
  const numbered = numberTrades(trades);

  if (!ctSelectedTradeEntryIso && numbered.length) {
    ctSelectedTradeEntryIso = numbered[numbered.length - 1].entry_time;
  }
  if (ctSelectedTradeEntryIso && !numbered.some((t) => t.entry_time === ctSelectedTradeEntryIso)) {
    ctSelectedTradeEntryIso = numbered[numbered.length - 1].entry_time;
  }

  body.innerHTML = numbered.slice().reverse().map((t) => `
    <tr class="ct-trade-row ${t.entry_time === ctSelectedTradeEntryIso ? 'selected' : ''}" data-entry="${t.entry_time}" style="cursor:pointer;">
      <td>#${t.tradeNum}</td>
      <td class="${t.direction === 'long' ? 'pos-long' : 'pos-short'}">${t.direction.toUpperCase()}</td>
      <td>${fmtTime(t.entry_time)}</td>
      <td>${fmtTime(t.exit_time)}</td>
      <td><span class="badge ${t.exit_reason === 'TP' ? 'tp' : 'sl'}">${t.exit_reason}</span></td>
    </tr>`).join("");

  body.querySelectorAll("tr.ct-trade-row").forEach((row) => {
    row.addEventListener("click", () => {
      ctSelectedTradeEntryIso = row.dataset.entry;
      renderChartTabTrades(ctLastData?.trades || []);
      renderChartTabTimeframe({ fitContent: false });
      scrollChartToTrade(row.dataset.entry);
    });
  });
}

function scrollChartToTrade(entryIso) {
  if (!ctChart || !ctLastData) return;
  ctSelectedTradeEntryIso = entryIso;
  const tf = getCtTf();
  const key = tf === "8m" ? "candles_8m" : "candles_4m";
  const rows = ctLastData[key] || [];
  const targetTime = toUnixTime(entryIso);
  let idx = rows.findIndex((c) => toUnixTime(c.time) >= targetTime);
  if (idx === -1) idx = rows.length - 1;

  const from = Math.max(0, idx - 50);
  const to = Math.min(rows.length - 1, idx + 50);
  ctChart.timeScale().setVisibleRange({
    from: toUnixTime(rows[from].time),
    to: toUnixTime(rows[to].time),
  });
}

document.getElementById("ct-back-50").addEventListener("click", () => shiftChartTabWindow(-50));
document.getElementById("ct-fwd-50").addEventListener("click", () => shiftChartTabWindow(50));
document.getElementById("ct-fit").addEventListener("click", () => {
  if (ctChart) ctChart.timeScale().fitContent();
});

function shiftChartTabWindow(barsDelta) {
  if (!ctChart || !ctLastData) return;
  const tf = getCtTf();
  const key = tf === "8m" ? "candles_8m" : tf === "1h" ? "candles_1h" : "candles_4m";
  const rows = ctLastData[key] || [];
  if (!rows.length) return;

  const range = ctChart.timeScale().getVisibleRange();
  if (!range) return;

  const times = rows.map((c) => toUnixTime(c.time));
  let fromIdx = times.findIndex((t) => t >= range.from);
  let toIdx = times.findIndex((t) => t >= range.to);
  if (fromIdx === -1) fromIdx = 0;
  if (toIdx === -1) toIdx = times.length - 1;

  fromIdx = Math.max(0, Math.min(times.length - 1, fromIdx + barsDelta));
  toIdx = Math.max(0, Math.min(times.length - 1, toIdx + barsDelta));
  if (toIdx <= fromIdx) toIdx = Math.min(times.length - 1, fromIdx + 1);

  ctChart.timeScale().setVisibleRange({ from: times[fromIdx], to: times[toIdx] });
}

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  initLiveChart();
  startLivePolling();
});
