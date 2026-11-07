/* Ham Ops Console -- shared by every screen: the status bar, the module row with
   the Alerts badge, the clock, the one freshness function, and polling.

   Everything here is display: the console service decides freshness and ranks
   the alerts (docs/contracts.md). A screen script (home.js, weather.js) loads
   after this file and registers what to draw with startScreen().

   Text is set with textContent only, never as HTML, so nothing from a data
   source can inject markup. */
"use strict";

const POLL_MS = 15000;
// A detail page goes back to Home after this long untouched (so the Priority Card is
// what someone walking up sees), and at once when a new unacknowledged critical item
// arrives. Proposed 2026-11-07 with the Weather page; Dan to confirm on the Pi.
const DETAIL_IDLE_MS = 3 * 60 * 1000;

const state = {
  data: {},              // latest reply per endpoint
  receivedAt: 0,         // Date.now() when the replies arrived (for ticking ages)
  badgeIds: null,        // badge item ids last time (null until the first poll)
  lastActivity: Date.now(),
};

const $ = id => document.getElementById(id);
const dash = v => (v === null || v === undefined || Number.isNaN(v)) ? "--" : v;
const round = (v, places = 0) => (typeof v === "number") ? Number(v.toFixed(places)) : null;
const PAGE = document.body.dataset.page || "home";

/* sessionStorage for small per-browser memory across screens (e.g. which critical
   items already flashed). It can be missing or throw; the screen works without it. */
function remember(key, value) { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* fine */ } }
function recall(key, fallback) {
  try { const v = sessionStorage.getItem(key); return v === null ? fallback : JSON.parse(v); }
  catch (e) { return fallback; }
}

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

// ----------------------------------------------------------------- module row and badge

function alertItems() {
  const a = state.data.alerts;
  return a && a.items ? a.items : [];
}

function renderBadge() {
  const items = alertItems().filter(i => i.tier === "badge");
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

/* Module buttons and toolbar buttons with data-href move to that screen. */
for (const b of document.querySelectorAll("[data-href]")) {
  b.addEventListener("click", () => { if (!b.disabled) location.href = b.dataset.href; });
}

/* On a detail page: back to Home when untouched for DETAIL_IDLE_MS, and at once for a
   new unacknowledged critical item (each one sends the screen Home once, so after
   looking at it the operator can come back here without being bounced again). */
function maybeReturnHome() {
  const sent = recall("hoc.sentHome", []);
  const critical = alertItems().filter(i => i.critical && !i.acknowledged);
  if (PAGE === "home") {
    // Anything critical already on the Home screen has been seen there: leaving Home
    // to look at the Weather page then doesn't bounce straight back.
    const add = critical.map(i => i.ref.id).filter(id => !sent.includes(id));
    if (add.length) remember("hoc.sentHome", sent.concat(add).slice(-50));
    return;
  }
  const fresh = critical.filter(i => !sent.includes(i.ref.id));
  if (fresh.length) {
    remember("hoc.sentHome", sent.concat(fresh.map(i => i.ref.id)).slice(-50));
    location.href = "/";
    return;
  }
  if (Date.now() - state.lastActivity > DETAIL_IDLE_MS) location.href = "/";
}
for (const ev of ["pointerdown", "keydown"]) {
  document.addEventListener(ev, () => { state.lastActivity = Date.now(); }, {passive: true});
}

// ----------------------------------------------------------------- polling

async function getJSON(path) {
  const r = await fetch(path, {cache: "no-store"});
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

const screen = {render: () => {}, tick: () => {}, slow: () => {}};
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
    renderStatusBar();
    renderBadge();
    screen.render();
    maybeReturnHome();
  } catch (e) {
    $("service-down").hidden = false;
  }
}

/* A screen script calls this once with its drawing functions:
   render() after every poll, tick() every second, slow() every 10 s (ages). */
function startScreen(fns) {
  Object.assign(screen, fns);
  const refresh = $("refresh");
  if (refresh) refresh.addEventListener("click", () => poll());
  tickClock();
  setInterval(() => {
    tickClock();
    if (state.receivedAt) { renderStatusBar(); screen.tick(); }
    maybeReturnHome();
  }, 1000);
  setInterval(() => { if (state.receivedAt) screen.slow(); }, 10000);
  setInterval(poll, POLL_MS);
  poll();
}
