/* Ham Ops Console -- Home screen logic.

   Everything here is display: the console service decides freshness and ranks
   the alerts (contracts 0.4, docs/contracts.md). This page only turns its JSON
   into words and colours, through one freshness function (freshText) so every
   tile shows age and stale/unavailable the same way.

   Polls the service every 15 s; the clock ticks every second. Text is set with
   textContent only, never as HTML, so nothing from a data source can inject markup. */
"use strict";

const POLL_MS = 15000;
const IDLE_BACK_TO_TOP_MS = 30000;     // Priority Card returns to the worst item (approved)
const FLASH_MAX_MS = 2 * 60 * 1000;    // a critical item flashes for at most 2 minutes (approved)

const state = {
  data: {},              // latest reply per endpoint
  receivedAt: 0,         // Date.now() when the replies arrived (for ticking ages)
  index: 0,              // Priority Card page
  topId: null,           // id of the worst card item last time
  lastTouch: 0,
  firstSeen: {},         // critical item id -> when this screen first showed it
  badgeIds: null,        // badge item ids last time (null until the first poll)
  clearArmed: 0,         // Clear hold: first tap arms (time), second confirms within 10 s
};

const $ = id => document.getElementById(id);
const dash = v => (v === null || v === undefined || Number.isNaN(v)) ? "--" : v;
const round = (v, places = 0) => (typeof v === "number") ? Number(v.toFixed(places)) : null;

// ----------------------------------------------------------------- time words

function localClock(d) {
  return d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
}
function clockPair(iso) {            // "10:21 AM (15:21 UTC)" -- local first, UTC beside it
  if (!iso) return "--";
  const d = new Date(iso);
  return `${localClock(d)} (${d.toISOString().slice(11, 16)} UTC)`;
}
function ageWords(sec) {
  if (sec === null || sec === undefined) return "--";
  if (sec < 60) return `${Math.round(sec)} s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
  return `${(sec / 3600).toFixed(1)} h ago`;
}
function liveAge(block) {           // the service's age, plus time since the reply arrived
  if (!block || block.age_sec === null || block.age_sec === undefined) return null;
  return block.age_sec + (Date.now() - state.receivedAt) / 1000;
}

function tickClock() {
  const now = new Date();
  $("utc").textContent = now.toISOString().slice(11, 16) + "Z";
  const zone = new Intl.DateTimeFormat([], {timeZoneName: "short"}).formatToParts(now)
    .find(p => p.type === "timeZoneName");
  const day = now.toLocaleDateString("en-GB", {weekday: "short", day: "numeric", month: "short"});
  $("local").textContent = `${localClock(now)}${zone ? " " + zone.value : ""} · ${day}`;
}

// ----------------------------------------------------------------- freshness (one function)

/* -> {text, cls, dot}: the words and colour for a block's freshness, used by every tile. */
function freshText(block, sourceName) {
  if (!block) return {text: "--", cls: "", dot: ""};
  const name = sourceName || block.source || "";
  switch (block.status) {
    case "ok": return {text: `${name} · ${ageWords(liveAge(block))}`, cls: "", dot: "good"};
    case "stale": {
      const age = liveAge(block);
      return {text: `STALE · ${age === null ? "--" : Math.round(age / 60)} MIN`, cls: "stale", dot: "fair"};
    }
    case "test": return {text: "TEST MODE", cls: "test", dot: "fair"};
    default: return {text: "UNAVAILABLE", cls: "unavailable", dot: "bad"};
  }
}
function setFresh(el, block, name) {
  const f = freshText(block, name);
  el.textContent = f.text;
  el.className = "fresh " + f.cls;
  return f;
}
function setDot(el, cls) { el.className = "dot " + (cls || ""); }

// ----------------------------------------------------------------- status bar

function renderStatusBar() {
  const {settings, weather, propagation, system} = state.data;
  const mode = (weather && weather.mode) || (settings && settings.mode) || "home";
  $("mode").textContent = mode === "field" ? "FIELD MODE" : "HOME MODE";
  const station = settings && settings.station && settings.station[mode];
  const loc = weather && weather.location;
  $("station").textContent = `${dash(station && station.callsign)} · ${dash((loc && loc.grid) || (station && station.grid))}`;

  // Internet: NWS reachable (the forecast fetch). NAS: the home sources. GPS: receiver present.
  const fc = weather && weather.forecast;
  pill("pill-internet", fc ? freshText(fc).dot : "", true);
  const nas = $("pill-nas");
  if (mode === "home") {
    nas.hidden = false;
    const blocks = [propagation && propagation.propagation, weather && weather.conditions];
    pill("pill-nas", worstDot(blocks.map(b => freshText(b).dot)), true);
  } else {
    nas.hidden = true;
  }
  const gps = system && system.system && system.system.gps;
  const gpsPill = $("pill-gps");
  const present = gps && gps.present;
  gpsPill.querySelector(".label").textContent = present ? (gps.fix ? `GPS ${gps.fix.toUpperCase()}` : "GPS") : "GPS off";
  pill("pill-gps", present ? (gps.fix ? "good" : "fair") : "off", present);
}
function pill(id, dot, on) {
  const el = $(id);
  setDot(el.querySelector(".dot"), dot);
  el.classList.toggle("off", !on);
}
function worstDot(dots) {
  for (const d of ["bad", "fair", ""]) if (dots.includes(d)) return d;
  return "good";
}

// ----------------------------------------------------------------- Priority Card

const LABELS = {
  hazard_warning: "NWS WARNING", lightning_5mi: "LIGHTNING DETECTED · HOME TEMPEST",
  alerts_unavailable_storms: "NWS ALERTS UNAVAILABLE", hazard_alert: "NWS",
  lightning_likely_soon: "LIGHTNING FORECAST", lightning_frequent: "LIGHTNING DETECTED · HOME TEMPEST",
  lightning_10mi: "LIGHTNING DETECTED · HOME TEMPEST", lightning_outlook: "LIGHTNING FORECAST",
  source_down: "DATA SOURCE", propagation: "PROPAGATION", all_quiet: "RIGHT NOW",
};

function cardItems() {
  const a = state.data.alerts;
  return (a && a.items ? a.items : []).filter(i => i.tier === "card");
}

function renderPriority() {
  const cards = cardItems();
  const card = $("priority");
  if (!cards.length) return;    // the service always sends at least all_quiet

  // A new worst item jumps to the front; after 30 s untouched, back to the worst.
  if (cards[0].ref.id !== state.topId) { state.index = 0; state.topId = cards[0].ref.id; }
  if (Date.now() - state.lastTouch > IDLE_BACK_TO_TOP_MS) state.index = 0;
  if (state.index >= cards.length) state.index = 0;
  const item = cards[state.index];

  const now = Date.now();
  for (const c of cards) if (c.critical && !(c.ref.id in state.firstSeen)) state.firstSeen[c.ref.id] = now;
  const flashing = item.critical && !item.acknowledged && now - state.firstSeen[item.ref.id] < FLASH_MAX_MS;

  const quiet = item.category === "all_quiet";
  card.className = "priority " + (item.color || "green") + (flashing ? " flash" : "")
    + (!quiet && item.detail ? " has-detail" : "");
  let label = LABELS[item.category] || "RIGHT NOW";
  if (item.category === "hazard_alert") label = "NWS · WATCH OR ADVISORY";
  if (item.critical) label = "CRITICAL · " + label;
  if (item.acknowledged) label += " · ACKNOWLEDGED";
  $("p-label").textContent = label;

  // The item's own words: title on the big line, detail on the line under it, so a
  // warning's name is never cut short. The right-hand aside is for the all-quiet card.
  const detail = $("p-detail");
  if (quiet) {
    $("p-title").textContent = item.detail ? `${item.title} ${item.detail}` : item.title;
    detail.hidden = true;
    $("p-aside").textContent = quietAside();
  } else {
    $("p-title").textContent = item.title;
    detail.hidden = !item.detail;
    detail.textContent = item.detail || "";
    $("p-aside").textContent = "";
  }

  const ack = $("p-ack");
  ack.hidden = !(item.critical && !item.acknowledged);
  ack.dataset.id = item.ref.id;
  const clear = $("p-clear");
  const lightning = item.ref.block === "weather.lightning";
  clear.hidden = !lightning;
  if (!lightning || Date.now() - state.clearArmed > 10000) state.clearArmed = 0;
  clear.textContent = state.clearArmed ? "Confirm clear" : "Clear hold";
  clear.classList.toggle("armed", !!state.clearArmed);

  $("pager").hidden = cards.length < 2;
  $("p-count").textContent = `${state.index + 1} of ${cards.length}`;
}

/* The right-hand line on an all-quiet card. Only shown when every source is ok
   (otherwise the service puts a source card first), so "No NWS alerts" is true. */
function quietAside() {
  const w = state.data.weather || {};
  const p = (state.data.propagation || {}).propagation || {};
  const parts = [];
  if (w.alerts && w.alerts.status === "ok") parts.push("No NWS alerts");
  if (w.lightning && w.lightning.status === "ok" && w.lightning.within_10mi === false) parts.push("no lightning");
  const line2 = (p.status === "ok") ? `K ${dash(p.k_index)} · SFI ${dash(p.sfi)}` : "";
  return parts.join(" · ") + (line2 ? (parts.length ? " · " : "") + line2 : "");
}

function renderBadge() {
  const a = state.data.alerts;
  const items = (a && a.items ? a.items : []).filter(i => i.tier === "badge");
  const badge = $("badge");
  badge.hidden = !items.length;
  badge.textContent = items.length;
  badge.setAttribute("aria-label", `${items.length} notification${items.length === 1 ? "" : "s"}`);
  badge.classList.toggle("blue", items.length > 0 && items.every(i => i.color === "blue"));
  const ids = items.map(i => i.ref.id);
  if (state.badgeIds && ids.some(id => !state.badgeIds.includes(id))) {
    badge.classList.remove("blink"); void badge.offsetWidth; badge.classList.add("blink");  // ~10 s
  }
  state.badgeIds = ids;
}

// ----------------------------------------------------------------- tiles

function renderPropagation() {
  const p = (state.data.propagation || {}).propagation;
  const f = setFresh($("prop-fresh"), p);
  const tile = $("t-prop");
  const show = p && p.status !== "unavailable";
  tile.classList.toggle("is-stale", !!p && p.status === "stale");
  $("sfi").textContent = show ? dash(p.sfi) : "--";
  $("kidx").textContent = show ? dash(p.k_index) : "--";
  $("aidx").textContent = show ? dash(p.a_index) : "--";
  $("xray").textContent = show ? dash(p.xray) : "--";
  const bands = $("bands");
  bands.replaceChildren(...(show ? p.bands : []).map(b => {
    const cell = document.createElement("div"); cell.className = "band";
    const name = document.createElement("b"); name.textContent = b.band;
    const rating = document.createElement("span");
    rating.textContent = b.rating ? b.rating.toUpperCase() : "--";
    rating.className = b.rating || "";
    cell.append(name, rating);
    return cell;
  }));
  $("prop-extra").textContent = show
    ? `SSN ${dash(p.sunspots)} · Solar wind ${dash(round(p.solar_wind_kms))} km/s`
    : (p && p.message) || f.text;
}

function renderWeather() {
  const w = state.data.weather || {};
  const c = w.conditions, l = w.lightning, a = w.alerts, fc = w.forecast;
  setFresh($("wx-fresh"), c);
  const showC = c && c.status !== "unavailable";
  $("t-wx").classList.toggle("is-stale", !!c && (c.status === "stale" || c.status === "test"));
  $("temp").textContent = showC && c.temperature_f !== null ? `${Math.round(c.temperature_f)}°F` : "--";
  // When unavailable, the reason is on the Priority Card and in Sources; the tile stays short.
  $("feels").textContent = showC
    ? `Feels ${c.feels_like_f === null ? "--" : Math.round(c.feels_like_f) + "°"} · Humidity ${dash(c.humidity_pct)}%`
    : "";
  $("wind").textContent = showC && c.wind_mph !== null
    ? `${dash(c.wind_dir)} ${Math.round(c.wind_mph)} mph${c.gust_mph !== null ? ", gust " + Math.round(c.gust_mph) : ""}` : "--";
  $("rain").textContent = !showC || c.rain_rate_in_hr === null ? "--"
    : c.rain_rate_in_hr === 0 ? "None" : `${c.rain_rate_in_hr.toFixed(2)} in/hr`;
  $("pressure").textContent = showC && c.pressure_inhg !== null
    ? `${c.pressure_inhg.toFixed(2)} inHg${c.station ? "" : " (station)"}` : "--";

  // Forecast: the first NWS period, or why there isn't one.
  const period = fc && fc.status !== "unavailable" && fc.periods && fc.periods[0];
  $("fc-name").textContent = period ? period.name : "Forecast";
  if (period) {
    const night = /night|tonight/i.test(period.name);
    $("fc").textContent = `${period.short}, ${night ? "low" : "high"} ${dash(period.temperature_f)}° · NWS`
      + (fc.status === "stale" ? " (STALE)" : "");
  } else {
    $("fc").textContent = (fc && fc.message) || "--";
  }

  renderLightningLine(l, fc);
  renderNwsLine(a);
}

function renderLightningLine(l, fc) {
  const dot = $("lt-dot"), text = $("lt-text");
  const outlook = fc && fc.lightning_outlook;
  const outlookWords = outlook
    ? `${outlook.level} ${new Date(outlook.start) <= new Date() ? "now" : "after " + localClock(new Date(outlook.start))} (forecast)`
    : "none forecast";
  if (!l) {                                  // Field Mode: no Tempest, forecast only
    setDot(dot, outlook ? "fair" : (fc && fc.status === "ok" ? "good" : ""));
    text.textContent = `Lightning: ${outlookWords}`;
    return;
  }
  if (l.status === "unavailable" || l.status === "test") {
    setDot(dot, l.status === "test" ? "fair" : "bad");
    text.textContent = l.status === "test" ? "Lightning: home station in test mode" : "Lightning: UNAVAILABLE";
    return;
  }
  const stale = l.status === "stale" ? " (STALE)" : "";
  if (l.within_5mi) {
    setDot(dot, "bad");
    text.textContent = `Lightning ${dash(l.closest_mi)} mi · ${dash(l.strikes_recent)} in ${dash(l.window_min)} min${stale}`;
  } else if (l.within_10mi) {
    setDot(dot, "fair");
    text.textContent = `Lightning ${dash(l.closest_mi)} mi · within 10 mi${stale}`;
  } else {
    setDot(dot, outlook ? "fair" : "good");
    const cleared = l.cleared_at ? ` · cleared ${localClock(new Date(l.cleared_at))}` : "";
    text.textContent = `No lightning nearby · ${outlookWords}${cleared}${stale}`;
  }
}

function renderNwsLine(a) {
  const dot = $("nws-dot"), text = $("nws-text");
  if (!a || a.status === "unavailable") {
    // Never "no alerts" when alerts can't be checked (approved design).
    setDot(dot, "bad");
    text.textContent = "NWS UNAVAILABLE — not the same as no alerts";
    return;
  }
  if (a.status === "test") { setDot(dot, "fair"); text.textContent = "NWS: home station in test mode"; return; }
  const items = a.items || [];
  const count = k => items.filter(i => i.kind === k).length;
  const WORDS = {warning: ["warning", "warnings"], watch: ["watch", "watches"],
                 advisory: ["advisory", "advisories"], statement: ["statement", "statements"]};
  const parts = [];
  for (const [kind, [one, many]] of Object.entries(WORDS)) {
    const n = count(kind);
    if (n) parts.push(`${n} ${n === 1 ? one : many}`);
  }
  const checked = a.issued_at ? ` · checked ${localClock(new Date(a.issued_at))}` : "";
  const stale = a.status === "stale" ? " (STALE)" : "";
  if (!items.length) {
    setDot(dot, a.status === "stale" ? "fair" : "good");
    text.textContent = `No active NWS alerts${checked}${stale}`;
  } else {
    setDot(dot, count("warning") ? "bad" : "fair");
    text.textContent = `NWS: ${parts.join(", ")}${stale}`;
  }
}

function renderSystem() {
  const s = (state.data.system || {}).system;
  if (!s) return;
  $("cpu").textContent = `${s.cpu_temp_c === null ? "--" : s.cpu_temp_c.toFixed(1) + " °C"} · ${s.cpu_load_pct === null ? "--" : Math.round(s.cpu_load_pct) + " %"}`;
  $("disk").textContent = s.disk_used_gb === null ? "--" : `${s.disk_used_gb} of ${s.disk_total_gb} GB`;
  $("net").textContent = (s.network && s.network.connection) || "Not connected";
  const pw = s.power || {};
  $("power").textContent = pw.undervoltage_now ? "UNDER-VOLTAGE now"
    : pw.throttled_now ? "Throttled now"
    : pw.undervoltage_now === null ? "--"
    : (pw.undervoltage_since_boot || pw.throttled_since_boot) ? "OK now · problem since boot" : "OK";

  const w = state.data.weather || {};
  const p = (state.data.propagation || {}).propagation;
  const home = (w.mode || "home") === "home";
  const rows = [];
  if (home) {
    rows.push(["PropMon", p]);
    rows.push(["Weather Sentinel", w.conditions]);
  }
  rows.push(["NWS forecast", w.forecast]);
  $("sources").replaceChildren(...rows.map(([name, block]) => {
    const row = document.createElement("div"); row.className = "line";
    const d = document.createElement("span"); setDot(d, freshText(block).dot);
    const t = document.createElement("span");
    const f = freshText(block);
    t.textContent = block && block.status !== "ok" ? `${name} · ${f.text.toLowerCase()}` : name;
    row.append(d, t);
    return row;
  }));
}

function renderAll() {
  renderStatusBar();
  renderPriority();
  renderBadge();
  renderPropagation();
  renderWeather();
  renderSystem();
}

// ----------------------------------------------------------------- polling and actions

async function getJSON(path) {
  const r = await fetch(path, {cache: "no-store"});
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

let settingsAt = 0;
async function poll() {
  try {
    const wantSettings = Date.now() - settingsAt > 60000;
    const [propagation, weather, alerts, system, settings] = await Promise.all([
      getJSON("/api/propagation"), getJSON("/api/weather"), getJSON("/api/alerts"), getJSON("/api/system"),
      wantSettings ? getJSON("/api/settings") : Promise.resolve(state.data.settings),
    ]);
    if (wantSettings) settingsAt = Date.now();
    Object.assign(state.data, {propagation, weather, alerts, system, settings});
    state.receivedAt = Date.now();
    $("service-down").hidden = true;
    renderAll();
  } catch (e) {
    $("service-down").hidden = false;
  }
}

$("p-prev").addEventListener("click", () => {
  const n = cardItems().length; state.index = (state.index - 1 + n) % n; touched();
});
$("p-next").addEventListener("click", () => {
  const n = cardItems().length; state.index = (state.index + 1) % n; touched();
});
$("p-ack").addEventListener("click", async e => {
  e.target.disabled = true;
  try {
    await fetch("/api/alerts/ack", {method: "POST", headers: {"Content-Type": "application/json"},
                                    body: JSON.stringify({id: e.target.dataset.id})});
  } finally { e.target.disabled = false; poll(); }
});
// Clear hold: two taps (approved 2026-11-05), so a stray touch can't end a lightning hold.
$("p-clear").addEventListener("click", async e => {
  state.lastTouch = Date.now();
  if (!state.clearArmed) { state.clearArmed = Date.now(); renderPriority(); return; }
  state.clearArmed = 0;
  e.target.disabled = true;
  try { await fetch("/api/alerts/clear-hold", {method: "POST"}); }
  finally { e.target.disabled = false; poll(); }
});
$("refresh").addEventListener("click", () => poll());
function touched() { state.lastTouch = Date.now(); state.clearArmed = 0; renderPriority(); }

tickClock();
setInterval(() => { tickClock(); if (state.receivedAt) { renderPriority(); renderStatusBar(); } }, 1000);
setInterval(() => { if (state.receivedAt) { renderPropagation(); renderWeather(); renderSystem(); } }, 10000);
setInterval(poll, POLL_MS);
poll();
