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
let currentTf = "1m";
let liveChart = null;
let liveCandleSeries = null;
let liveEmaFast1m = null;
let liveEmaSlow1m = null;
let liveEmaFast5m = null;
let liveEmaSlow5m = null;

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

  liveEmaFast1m = liveChart.addLineSeries({ color: "#4f9dff", lineWidth: 1, title: "EMA fast 1m", priceLineVisible: false, lastValueVisible: false });
  liveEmaSlow1m = liveChart.addLineSeries({ color: "#a259ff", lineWidth: 1, title: "EMA slow 1m", priceLineVisible: false, lastValueVisible: false });
  liveEmaFast5m = liveChart.addLineSeries({ color: "#ffb84f", lineWidth: 2, title: "EMA fast 5m", priceLineVisible: false, lastValueVisible: false });
  liveEmaSlow5m = liveChart.addLineSeries({ color: "#ff5f8f", lineWidth: 2, title: "EMA slow 5m", priceLineVisible: false, lastValueVisible: false });

  const legend = document.getElementById("live-chart-legend");
  if (legend) {
    legend.innerHTML = `
      <span style="color:#4f9dff;">─ EMA fast 1m</span>
      <span style="color:#a259ff;">─ EMA slow 1m</span>
      <span style="color:#ffb84f;">─ EMA fast 5m</span>
      <span style="color:#ff5f8f;">─ EMA slow 5m</span>`;
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

function getStrategyEmaPeriods() {
  return {
    fast: parseInt(document.getElementById("p-ema-fast").value, 10) || 7,
    slow: parseInt(document.getElementById("p-ema-slow").value, 10) || 133,
  };
}

async function refreshLiveEmaOverlay() {
  try {
    const { fast, slow } = getStrategyEmaPeriods();

    const data1m = await apiGet(`/api/candles?symbol=${currentSymbol}&tf=1m&n=300`);
    const closes1m = data1m.candles.map((c) => c.close);
    const times1m = data1m.candles.map((c) => Math.floor(new Date(c.time).getTime() / 1000));
    const emaFast1m = computeEma(closes1m, fast);
    const emaSlow1m = computeEma(closes1m, slow);
    liveEmaFast1m.setData(times1m.map((t, i) => ({ time: t, value: emaFast1m[i] })));
    liveEmaSlow1m.setData(times1m.map((t, i) => ({ time: t, value: emaSlow1m[i] })));

    const data5m = await apiGet(`/api/candles?symbol=${currentSymbol}&tf=5m&n=300`);
    const closes5m = data5m.candles.map((c) => c.close);
    const times5m = data5m.candles.map((c) => Math.floor(new Date(c.time).getTime() / 1000));
    const emaFast5m = computeEma(closes5m, fast);
    const emaSlow5m = computeEma(closes5m, slow);
    liveEmaFast5m.setData(times5m.map((t, i) => ({ time: t, value: emaFast5m[i] })));
    liveEmaSlow5m.setData(times5m.map((t, i) => ({ time: t, value: emaSlow5m[i] })));
  } catch (e) {
    console.error("refreshLiveEmaOverlay failed", e);
  }
}

async function refreshCandles() {
  try {
    const data = await apiGet(`/api/candles?symbol=${currentSymbol}&tf=${currentTf}&n=300`);
    const bars = data.candles.map((c) => ({
      time: Math.floor(new Date(c.time).getTime() / 1000),
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    liveCandleSeries.setData(bars);
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
        time: Math.floor(new Date(t.entry_time).getTime() / 1000),
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
        time: Math.floor(new Date(t.exit_time).getTime() / 1000),
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

    if (data.balance && data.balance.total_equity !== null) {
      badge.textContent = `Equity: ${Number(data.balance.total_equity).toFixed(2)} USDT`;
      badge.className = "balance-badge balance-on";
    } else {
      badge.textContent = "Подключено";
      badge.className = "balance-badge balance-on";
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
      body.innerHTML = `<tr class="empty-row"><td colspan="7">Нет открытых позиций</td></tr>`;
      return;
    }

    body.innerHTML = positions.map((p) => `
      <tr>
        <td>${p.symbol}</td>
        <td class="${p.direction === 'long' ? 'pos-long' : 'pos-short'}">${p.direction.toUpperCase()}</td>
        <td>${fmtTime(p.entry_time)}</td>
        <td>${Number(p.entry_price).toFixed(4)}</td>
        <td>${Number(p.tp_pct).toFixed(2)}%</td>
        <td>${Number(p.sl_pct).toFixed(2)}%</td>
        <td>${Number(p.notional).toFixed(0)}</td>
      </tr>`).join("");
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

    // update equity stat cards from the last known equity curve point
    const equityCurve = await apiGet("/api/equity_curve?limit=1");
    if (equityCurve.length) {
      const last = equityCurve[equityCurve.length - 1];
      const eq = parseFloat(last.equity);
      const pnl = eq - 100000; // fallback baseline if initial equity unknown here
      document.getElementById("stat-equity").textContent = eq.toFixed(2);
      const pnlEl = document.getElementById("stat-pnl");
      pnlEl.textContent = fmtUsd(pnl);
      pnlEl.className = "value " + (pnl >= 0 ? "pos" : "neg");
    }
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

  ctEmaFast1m = ctChart.addLineSeries({ color: "#4f9dff", lineWidth: 1, title: "EMA fast (1m)" });
  ctEmaSlow1m = ctChart.addLineSeries({ color: "#a259ff", lineWidth: 1, title: "EMA slow (1m)" });
  ctEmaFast5m = ctChart.addLineSeries({ color: "#ffb84f", lineWidth: 2, title: "EMA fast (5m)" });
  ctEmaSlow5m = ctChart.addLineSeries({ color: "#ff5f8f", lineWidth: 2, title: "EMA slow (5m)" });

  document.getElementById("ct-legend").innerHTML = `
    <span style="color:#4f9dff;">─ EMA fast 1m</span>
    <span style="color:#a259ff;">─ EMA slow 1m</span>
    <span style="color:#ffb84f;">─ EMA fast 5m</span>
    <span style="color:#ff5f8f;">─ EMA slow 5m</span>
    <span style="color:#26a269;">▲ вход</span>
    <span style="color:#d64545;">▼ выход</span>`;

  window.addEventListener("resize", () => {
    ctChart.applyOptions({ width: container.clientWidth });
  });
}

["ct-symbols", "ct-tf", "ct-direction"].forEach((id) => {
  document.getElementById(id).addEventListener("click", (e) => {
    const label = e.target.closest("label[data-val]");
    if (!label) return;
    document.querySelectorAll(`#${id} label`).forEach((l) => l.classList.remove("checked"));
    label.classList.add("checked");
    if (id === "ct-tf" && ctLastData) renderChartTabTimeframe();
  });
});

function getCtSymbol() {
  const checked = document.querySelector("#ct-symbols label.checked");
  return checked ? checked.dataset.val : "BTCUSDT";
}
function getCtTf() {
  const checked = document.querySelector("#ct-tf label.checked");
  return checked ? checked.dataset.val : "1m";
}
function getCtDirection() {
  const checked = document.querySelector("#ct-direction label.checked");
  return checked ? checked.dataset.val : "both";
}

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
    showToast(`Загружено: ${data.candles_1m.length} свечей 1m, ${data.trades.length} сделок`);
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

function renderChartTabTimeframe(opts = {}) {
  if (!ctLastData) return;
  const fitContent = opts.fitContent !== false;
  const tf = getCtTf();
  const key = tf === "1m" ? "candles_1m" : tf === "5m" ? "candles_5m" : "candles_1h";
  const rows = ctLastData[key] || [];

  const bars = rows.map((c) => ({
    time: toUnixTime(c.time), open: c.open, high: c.high, low: c.low, close: c.close,
  }));
  ctCandleSeries.setData(dedupSorted(bars));

  if (tf === "1m") {
    ctEmaFast1m.setData(dedupSorted(rows.filter((c) => c.ema_fast != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_fast }))));
    ctEmaSlow1m.setData(dedupSorted(rows.filter((c) => c.ema_slow != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_slow }))));
    ctEmaFast5m.setData(dedupSorted(rows.filter((c) => c.ema_fast_5m != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_fast_5m }))));
    ctEmaSlow5m.setData(dedupSorted(rows.filter((c) => c.ema_slow_5m != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_slow_5m }))));
  } else if (tf === "5m") {
    ctEmaFast1m.setData([]);
    ctEmaSlow1m.setData([]);
    ctEmaFast5m.setData(dedupSorted(rows.filter((c) => c.ema_fast != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_fast }))));
    ctEmaSlow5m.setData(dedupSorted(rows.filter((c) => c.ema_slow != null).map((c) => ({ time: toUnixTime(c.time), value: c.ema_slow }))));
  } else {
    // 1h: context only, no EMA overlay computed server-side for this tf
    ctEmaFast1m.setData([]);
    ctEmaSlow1m.setData([]);
    ctEmaFast5m.setData([]);
    ctEmaSlow5m.setData([]);
  }

  // trade markers: only meaningful on 1m/5m where entry/exit timestamps
  // land on real bars; on 1h they'd be too imprecise to place reliably.
  if (tf === "1m" || tf === "5m") {
    setTradeMarkers(ctLastData.trades || []);
  } else {
    ctCandleSeries.setMarkers([]);
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

function setTradeMarkers(trades) {
  const numbered = numberTrades(trades);
  const markers = [];
  numbered.forEach((t) => {
    markers.push({
      time: Math.floor(new Date(t.entry_time).getTime() / 1000),
      position: t.direction === "long" ? "belowBar" : "aboveBar",
      color: t.direction === "long" ? "#26a269" : "#d64545",
      shape: t.direction === "long" ? "arrowUp" : "arrowDown",
      text: `#${t.tradeNum} ${t.direction === "long" ? "LONG" : "SHORT"} @ ${Number(t.entry_price).toFixed(2)}`,
    });

    // PnL label on the exit marker: green text/marker for profit,
    // red for loss, so a closed trade's outcome is readable directly
    // on the chart without opening the trades table. Same #N as the
    // entry marker above, so entry/exit pairs are visually traceable.
    const pnlPct = Number(t.pnl_pct);
    const isProfit = Number.isFinite(pnlPct) ? pnlPct >= 0 : (t.exit_reason === "TP");
    const pnlColor = isProfit ? "#26a269" : "#d64545";
    const pnlText = Number.isFinite(pnlPct)
      ? `#${t.tradeNum} ${t.exit_reason} ${isProfit ? "+" : ""}${pnlPct.toFixed(2)}%`
      : `#${t.tradeNum} ${t.exit_reason} @ ${Number(t.exit_price).toFixed(2)}`;

    markers.push({
      time: Math.floor(new Date(t.exit_time).getTime() / 1000),
      position: t.direction === "long" ? "aboveBar" : "belowBar",
      color: pnlColor,
      shape: "circle",
      text: pnlText,
    });
  });
  markers.sort((a, b) => a.time - b.time);
  ctCandleSeries.setMarkers(markers);
}

function renderChartTabTrades(trades) {
  const body = document.getElementById("ct-trades-body");
  if (!trades || !trades.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="5">Сделок нет в этом окне</td></tr>`;
    return;
  }
  const numbered = numberTrades(trades);
  body.innerHTML = numbered.slice().reverse().map((t) => `
    <tr class="ct-trade-row" data-entry="${t.entry_time}" style="cursor:pointer;">
      <td>#${t.tradeNum}</td>
      <td class="${t.direction === 'long' ? 'pos-long' : 'pos-short'}">${t.direction.toUpperCase()}</td>
      <td>${fmtTime(t.entry_time)}</td>
      <td>${fmtTime(t.exit_time)}</td>
      <td><span class="badge ${t.exit_reason === 'TP' ? 'tp' : 'sl'}">${t.exit_reason}</span></td>
    </tr>`).join("");

  body.querySelectorAll("tr.ct-trade-row").forEach((row) => {
    row.addEventListener("click", () => scrollChartToTrade(row.dataset.entry));
  });
}

function scrollChartToTrade(entryIso) {
  if (!ctChart || !ctLastData) return;
  const tf = getCtTf();
  const key = tf === "5m" ? "candles_5m" : "candles_1m";
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
  const key = tf === "5m" ? "candles_5m" : tf === "1h" ? "candles_1h" : "candles_1m";
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
