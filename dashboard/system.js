/* Ham Ops Console -- System page (approved mockup "System page", 2026-10-12).

   Three tiles: THIS PI (temperature, load, memory, disk, power, uptime), NETWORK ·
   LOCATION · TIME (Wi-Fi, internet, location, GPS, clock, backup clock) and DATA
   SOURCES (each source's freshness, decided by the service). Readings the service
   can't take show "--". Text is set with textContent only. */
"use strict";

// CPU temperature words. The Pi 5 starts slowing itself down at 80 °C and throttles
// hard at 85 °C, so amber from 75 °C gives warning before that.
const TEMP_WARM_C = 75;
const TEMP_HOT_C = 80;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

/* rows: [label, value, cls] -> <dt>/<dd> pairs. */
function setFacts(id, rows) {
  $(id).replaceChildren(...rows.flatMap(([label, value, cls]) =>
    [el("dt", "", label), el("dd", cls || "", value === null || value === undefined ? "--" : value)]));
}

const num = v => typeof v === "number" && !Number.isNaN(v);

function uptimeWords(sec) {
  if (!num(sec)) return null;
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${m} min`;
  return `${m} min`;
}

function powerWords(p) {
  if (!p || p.undervoltage_now === null || p.undervoltage_now === undefined) return [null, ""];
  if (p.undervoltage_now) return ["Under-voltage now", "bad"];
  if (p.throttled_now) return ["Throttled now", "fair"];
  if (p.undervoltage_since_boot) return ["Under-voltage since boot", "fair"];
  if (p.throttled_since_boot) return ["Throttled since boot", "fair"];
  return ["No under-voltage", ""];
}

/* "6:04 PM", or "Sun 6:04 PM" when the service started on another day. */
function shortStart(iso) {
  const d = new Date(iso);
  const day = new Date().toDateString() === d.toDateString() ? "" : d.toLocaleDateString([], {weekday: "short"}) + " ";
  return day + mainTime(d);
}

function renderPi(s) {
  const tempCls = !num(s.cpu_temp_c) ? "" : s.cpu_temp_c >= TEMP_HOT_C ? "bad" : s.cpu_temp_c >= TEMP_WARM_C ? "fair" : "";
  const tempWords = !num(s.cpu_temp_c) ? null
    : `${s.cpu_temp_c.toFixed(1)} °C${tempCls === "bad" ? " · hot" : tempCls === "fair" ? " · warm" : ""}`;
  const mem = num(s.memory_used_gb) && num(s.memory_total_gb)
    ? `${s.memory_used_gb.toFixed(1)} of ${s.memory_total_gb.toFixed(1)} GB${num(s.memory_used_pct) ? ` (${s.memory_used_pct} %)` : ""}`
    : (num(s.memory_used_pct) ? `${s.memory_used_pct} %` : null);
  const disk = num(s.disk_used_gb) && num(s.disk_total_gb) ? `${s.disk_used_gb.toFixed(1)} of ${s.disk_total_gb} GB` : null;
  const [power, powerCls] = powerWords(s.power);
  setFacts("pi-facts", [
    ["CPU temp", tempWords, tempCls],
    ["CPU load", num(s.cpu_load_pct) ? `${s.cpu_load_pct} %` : null],
    ["Memory", mem],
    ["Disk", disk],
    ["Power", power, powerCls],
    ["Uptime", uptimeWords(s.uptime_sec)],
  ]);
  const svc = s.service || {};
  const started = svc.started_at ? ` · started ${shortStart(svc.started_at)}` : "";
  $("pi-foot").textContent = `Ham Ops Console ${svc.version || "--"}${started}`;
  setFresh($("pi-fresh"), state.data.system && state.data.system.system, s.hostname || "Pi");
}

function renderNet(s) {
  const n = s.network || {};
  const wifi = n.connection
    ? `${n.connection}${num(n.wifi_signal_pct) ? ` · signal ${n.wifi_signal_pct} %` : ""}`
    : "Not connected";
  const wifiCls = n.connection ? "" : "bad";

  // Internet: whether NWS answers (the forecast fetch), the same test as the status-bar pill.
  const fc = (state.data.weather || {}).forecast;
  let internet = null, internetCls = "";
  if (fc) {
    if (fc.status === "ok") internet = "Reachable";
    else { internet = "NWS not answering"; internetCls = fc.status === "stale" ? "fair" : "bad"; }
  }

  const mode = ((state.data.weather || {}).mode) || ((state.data.settings || {}).mode) || "home";
  const st = (((state.data.settings || {}).station) || {})[mode] || {};
  const grid = ((state.data.weather || {}).location || {}).grid || st.grid;
  const source = st.grid_source === "gps" ? "GPS" : "Manual";
  const location = grid ? `${source} · ${grid}` : null;

  const g = s.gps || {};
  let gps = "Not connected", gpsCls = "";
  if (g.present) {
    if (g.fix) {
      gps = `${g.fix.toUpperCase()} fix${num(g.satellites_used) ? ` · ${g.satellites_used} satellites` : ""}`;
      gpsCls = "good";
    } else {
      gps = "Connected · no fix yet";
    }
  }

  const c = s.clock || {};
  let clockText = null, clockCls = "";
  if (c.synced === true) clockText = c.source === "gps" ? "GPS time" : "Internet time (NTP)";
  else if (c.synced === false) { clockText = "Not synced · RTC"; clockCls = "fair"; }

  const r = s.rtc || {};
  let backup = null;
  if (num(r.battery_v)) backup = `${r.battery_v.toFixed(2)} V${r.charging === true ? " · charging" : r.charging === false ? " · not charging" : ""}`;

  setFacts("net-facts", [
    ["Wi-Fi", wifi, wifiCls],
    ["Internet", internet, internetCls],
    ["Location", location],
    ["GPS", gps, gpsCls],
    ["Clock", clockText, clockCls],
    ["Backup clock", backup],
  ]);
}

/* One row per source: dot, name with what it supplies, and its age or state. */
function sourceRow(name, what, block, ageText) {
  const f = freshText(block);
  let right = f.text, cls = f.cls;
  if (block && block.status === "ok") right = ageText !== undefined ? ageText : ageWords(liveAge(block));
  const sub = block && block.status !== "ok" && block.message ? block.message : what;
  const dot = el("span", "dot " + (f.dot || ""));
  const mid = el("div", "what");
  mid.append(el("b", "", name), el("span", "", sub));
  return [dot, mid, el("span", "age " + cls, right)];
}

/* The worst of several blocks from one source (Weather Sentinel has three). */
function worstBlock(blocks) {
  const rank = {unavailable: 3, test: 2, stale: 1, ok: 0};
  const present = blocks.filter(Boolean);
  if (!present.length) return null;
  return present.reduce((a, b) => ((rank[b.status] ?? 3) > (rank[a.status] ?? 3) ? b : a));
}

function radioAge(r) {
  if (!r) return undefined;
  if (r.updating) return "updating";
  const age = liveAge(r);
  if (age === null) return undefined;
  const days = Math.floor(age / 86400);
  return days === 0 ? "today" : `${days} day${days === 1 ? "" : "s"} old`;
}

function renderSources(s) {
  const w = state.data.weather || {};
  const p = (state.data.propagation || {}).propagation;
  const mode = w.mode || "home";
  $("src-title").textContent = `DATA SOURCES · ${mode === "field" ? "FIELD" : "HOME"} MODE`;
  const svc = s.service || {};
  const up = svc.started_at ? (Date.now() - new Date(svc.started_at)) / 1000 : null;
  const service = {status: "ok", age_sec: 0};
  const rows = [
    sourceRow("PropMon", "home server · propagation", p),
    sourceRow("Weather Sentinel", "home server · Tempest + NWS alerts", worstBlock([w.alerts, w.conditions, w.lightning])),
    sourceRow("Console service", "this Pi · page and data relay", service,
              up === null ? "running" : `running · ${uptimeWords(up)}`),
    sourceRow("NWS forecast", `internet · ${mode} grid`, w.forecast),
    sourceRow("Weather radio list", "stored on this Pi · updated weekly", w.weather_radio, radioAge(w.weather_radio)),
  ];
  $("sources").replaceChildren(...rows.flat());
}

function render() {
  const s = (state.data.system || {}).system || {};
  renderPi(s);
  renderNet(s);
  renderSources(s);
}

startScreen({render, slow: render});
