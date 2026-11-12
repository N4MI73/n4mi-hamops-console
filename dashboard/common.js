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

/* The main clock (display.main_clock, Dan 2026-11-11): "utc" or "local" is shown large and
   first everywhere; the other follows, smaller. Changed by tapping the big time in the status
   bar or on the Settings Display tab. Remembered in this browser so a new screen doesn't
   flicker to UTC before the settings arrive. */
function mainClock() {
  const s = state.data.settings && state.data.settings.display && state.data.settings.display.main_clock;
  const m = s || recall("hoc.mainClock", "utc");
  return m === "local" ? "local" : "utc";
}
function localClock(d) {
  return d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
}
function utcClock(d) { return d.toISOString().slice(11, 16) + "Z"; }
/* A time standing alone ("Until 3:04 PM" / "Until 20:04Z"): the main clock's. */
function mainTime(d) { return mainClock() === "local" ? localClock(d) : utcClock(d); }
/* Both times, main first: "15:21 UTC (10:21 AM)" or "10:21 AM (15:21 UTC)". */
function clockPair(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  const utc = `${d.toISOString().slice(11, 16)} UTC`;
  return mainClock() === "local" ? `${localClock(d)} (${utc})` : `${utc} (${localClock(d)})`;
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
  const zone = new Intl.DateTimeFormat([], {timeZoneName: "short"}).formatToParts(now)
    .find(p => p.type === "timeZoneName");
  const tz = zone ? zone.value : "";
  const day = now.toLocaleDateString("en-GB", {weekday: "short", day: "numeric", month: "short"});
  // The ids stay "utc" (large) and "local" (small); what they hold follows the main clock.
  if (mainClock() === "local") {
    $("utc").textContent = localClock(now);
    $("local").textContent = [tz, utcClock(now), fdShowing() ? "" : day].filter(Boolean).join(" · ");
  } else {
    $("utc").textContent = utcClock(now);
    $("local").textContent = `${localClock(now)}${tz ? " " + tz : ""}` + (fdShowing() ? "" : ` · ${day}`);
  }
  if (state.receivedAt) renderFieldDayPill();   // the countdown moves each minute
}

/* Tap the big time to swap UTC and local. Saved on the Pi (PUT /api/settings), so the
   choice survives a restart and the Settings Display tab shows it. The screen changes at
   once; if saving fails it changes back. */
async function swapMainClock() {
  const before = mainClock();
  const next = before === "local" ? "utc" : "local";
  const apply = m => {
    state.data.settings = state.data.settings || {};
    state.data.settings.display = Object.assign({}, state.data.settings.display, {main_clock: m});
    remember("hoc.mainClock", m);
    tickClock();
    if (state.receivedAt) { renderStatusBar(); screen.render(); }
  };
  apply(next);
  try {
    const r = await fetch("/api/settings", {method: "PUT", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify({display: {main_clock: next}})});
    if (!r.ok) throw new Error(String(r.status));
  } catch (e) {
    apply(before);
    return;
  }
  poll();   // the service's own wording ("Until ...") follows on the next reply
}
function setUpClockTap() {
  const clock = document.querySelector(".status-bar .clock");
  if (!clock) return;
  clock.setAttribute("role", "button");
  clock.setAttribute("aria-label", "Swap UTC and local time");
  clock.classList.add("tappable");
  clock.addEventListener("click", swapMainClock);
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

// ----------------------------------------------------------------- Field Day timer (2026-11-12)

/* The Field Day profile's pill in the status bar (approved 2026-11-05): a reminder before
   the day, then Setup counting down to the start of operations (from local midnight on
   the day -- the service works that out), then Operations counting down to the end, then
   "Operations complete" until the profile is switched off. Hours:minutes, rounded up so
   it never reads 0:00 while time is left. Times in the pill are UTC, as entered. */
/* While the pill shows, the status bar drops the date beside the clock to make room. */
function fdShowing() {
  const p = document.getElementById("fd-pill");
  return !!(p && !p.hidden);
}

function fdHM(ms) {
  const m = Math.max(0, Math.ceil(ms / 60000));
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
}

function fdUtcHM(d) {
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}Z`;
}

function renderFieldDayPill() {
  let p = $("fd-pill");
  if (!p) {
    const station = $("station");
    if (!station) return;
    p = document.createElement("span");
    p.id = "fd-pill";
    p.className = "fd-pill";
    p.hidden = true;
    station.after(p);
  }
  const fd = state.data.system && state.data.system.field_day;
  if (!fd || !fd.enabled) { p.hidden = true; return; }
  const span = (cls, text) => { const e = document.createElement("span"); e.className = cls; e.textContent = text; return e; };
  const tag = span("fd-tag", "FIELD DAY");
  const now = Date.now();
  const start = fd.ops_start ? new Date(fd.ops_start) : null;
  const end = fd.ops_end ? new Date(fd.ops_end) : null;
  const setup = fd.setup_start ? new Date(fd.setup_start) : null;
  let cls = "fd-pill", parts, label;
  if (!start || !end || !setup) {
    cls += " quiet";
    parts = [span("fd-dim", "Set ops times in Settings")];
    label = "Field Day: operations times not set";
  } else if (now < setup) {
    cls += " quiet";
    const day = start.toLocaleDateString("en-GB", {weekday: "short", day: "numeric", month: "short", timeZone: "UTC"});
    parts = [span("fd-day", day), span("fd-dim", `ops ${fdUtcHM(start)}`)];
    label = `Field Day ${day}, operations start ${fdUtcHM(start)}`;
  } else if (now < start) {
    const left = fdHM(start - now);
    parts = [span("fd-word", "Setup"), span("fd-num num", left), span("fd-dim", "until operations")];
    label = `Field Day setup, ${left} until operations`;
  } else if (now < end) {
    const left = fdHM(end - now);
    parts = [span("fd-word", "Operations"), span("fd-num num", left), span("fd-dim", "left")];
    label = `Field Day operations, ${left} left`;
  } else {
    cls += " done";
    const tick = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    tick.setAttribute("viewBox", "0 0 20 20");
    tick.setAttribute("aria-hidden", "true");
    tick.innerHTML = '<path d="M4 10.5l4 4 8-9" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>';
    parts = [tick, span("fd-day", "Operations complete"), span("fd-dim", fdUtcHM(end))];
    label = `Field Day operations complete, ended ${fdUtcHM(end)}`;
  }
  p.className = cls;
  p.setAttribute("aria-label", label);
  p.replaceChildren(tag, ...parts);
  p.hidden = false;
}

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
  renderFieldDayPill();
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
  if (!badge) return;                // the Settings screen has no module row
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

// ----------------------------------------------------------------- Sleep (2026-11-11)

/* Sleep turns the backlight off (the service does it). A black cover goes over the page
   at the same time, so the touch that wakes the screen doesn't also press a button that
   can't be seen. A new unacknowledged critical item wakes the screen by itself. The
   service remembers Sleep, so a page loaded while asleep (the 3-minute return to Home)
   covers itself again. */
let sleepCover = null;
let sleepChangedAt = 0;          // a reply from before the last Sleep/wake is ignored

function showSleep(on) {
  if (!sleepCover) {
    sleepCover = document.createElement("div");
    sleepCover.className = "sleep-cover";
    sleepCover.hidden = true;
    sleepCover.textContent = "Touch to wake";
    const wakeNow = e => { e.preventDefault(); e.stopPropagation(); setSleep(false); };
    sleepCover.addEventListener("pointerdown", wakeNow, {capture: true});
    sleepCover.addEventListener("click", e => { e.preventDefault(); e.stopPropagation(); }, {capture: true});
    document.body.append(sleepCover);
  }
  sleepCover.hidden = !on;
}

async function setSleep(on) {
  sleepChangedAt = Date.now();
  showSleep(on);
  try {
    const r = await fetch("/api/display", {method: "POST", headers: {"Content-Type": "application/json"},
                                           body: JSON.stringify({action: on ? "sleep" : "wake"})});
    const body = await r.json();
    if (state.data.system && body.display) state.data.system.display = body.display;
    return r.ok;
  } catch (e) {
    if (!on) showSleep(false);    // can't reach the service: never leave the page covered
    return false;
  }
}

function syncSleep() {
  const d = state.data.system && state.data.system.display;
  if (!d || Date.now() - sleepChangedAt < 5000) return;
  showSleep(!!d.asleep);
  if (!d.asleep) return;
  const woke = recall("hoc.wokeFor", []);
  const fresh = alertItems().filter(i => i.critical && !i.acknowledged && !woke.includes(i.ref.id));
  if (fresh.length) {
    setSleep(false).then(ok => {
      if (ok) remember("hoc.wokeFor", woke.concat(fresh.map(i => i.ref.id)).slice(-50));
    });
  }
}

// ----------------------------------------------------------------- Red night style (2026-11-12)

/* Night style "Red": during the night times in Settings, the whole page is shown in red
   only, to protect night vision. A colour filter turns every colour into its brightness in
   red, so green/amber/red no longer differ: alerts rely on their words and the flashing
   border, as approved with the design (2026-10-12). "Dim" changes only the backlight. The
   service decides whether it is night (the `display` block); Settings can preview it. */
let redPreviewUntil = 0;

function syncNightStyle() {
  if (!document.getElementById("hoc-red")) {
    document.body.insertAdjacentHTML("beforeend",
      '<svg aria-hidden="true" width="0" height="0" style="position:absolute">' +
      '<filter id="hoc-red" color-interpolation-filters="sRGB">' +
      '<feColorMatrix type="matrix" values="0.30 0.75 0.15 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0"/>' +
      '</filter></svg>');
  }
  const d = (state.data.system && state.data.system.display) || {};
  const style = ((state.data.settings || {}).display || {}).night_style;
  const on = (d.night === true && style === "red") || Date.now() < redPreviewUntil;
  document.documentElement.classList.toggle("night-red", on);
}

/* Settings' Preview button: red for `sec` seconds, whatever the time. */
function previewRed(sec) {
  redPreviewUntil = Date.now() + sec * 1000;
  syncNightStyle();
  setTimeout(syncNightStyle, sec * 1000 + 50);
}

// ----------------------------------------------------------------- Shut down (2026-11-11)

/* The approved Shut down dialog: large Cancel on the left, Restart in the middle, red
   Shut down on the right, and "Wait for the screen to go dark before unplugging power." */
function openShutdown() {
  const box = document.createElement("div");
  box.className = "sd-overlay";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  const panel = document.createElement("div");
  panel.className = "sd-panel";
  const title = document.createElement("div");
  title.className = "sd-title";
  title.textContent = "Shut down the console?";
  const note = document.createElement("div");
  note.className = "sd-note";
  note.textContent = "Wait for the screen to go dark before unplugging power.";
  const err = document.createElement("div");
  err.className = "sd-error";
  err.setAttribute("role", "alert");
  const row = document.createElement("div");
  row.className = "sd-buttons";
  const button = (text, cls, fn) => {
    const b = document.createElement("button");
    b.className = cls;
    b.textContent = text;
    b.addEventListener("click", fn);
    row.append(b);
    return b;
  };
  const close = () => box.remove();
  const go = async action => {
    for (const b of row.querySelectorAll("button")) b.disabled = true;
    err.textContent = "";
    let r, body = {};
    try {
      r = await fetch("/api/power", {method: "POST", headers: {"Content-Type": "application/json"},
                                     body: JSON.stringify({action})});
      body = await r.json();
    } catch (e) { /* r stays undefined */ }
    if (r && r.ok) {
      panel.replaceChildren(title, note);
      title.textContent = action === "restart" ? "Restarting…" : "Shutting down…";
      note.textContent = action === "restart"
        ? "The console comes back by itself in about a minute."
        : "Wait for the screen to go dark before unplugging power.";
      return;
    }
    err.textContent = (body && body.error) || "The console service didn't answer.";
    for (const b of row.querySelectorAll("button")) b.disabled = false;
  };
  button("Cancel", "sd-cancel", close);
  button("Restart", "sd-restart", () => go("restart"));
  button("Shut down", "sd-shutdown", () => go("shutdown"));
  panel.append(title, note, err, row);
  box.append(panel);
  document.body.append(box);
}

// ----------------------------------------------------------------- toolbar buttons

/* Refresh: dimmed and "Refreshing…" until the new data is in, then "Updated" for a moment,
   so it's clear the tap worked (Dan, 2026-11-11). Extra taps meanwhile do nothing. */
function setUpToolbar() {
  const refresh = $("refresh");
  if (refresh) refresh.addEventListener("click", async () => {
    if (refresh.disabled) return;
    refresh.disabled = true;
    refresh.textContent = "Refreshing…";
    const started = Date.now();
    const ok = await poll();
    await new Promise(res => setTimeout(res, Math.max(0, 500 - (Date.now() - started))));
    refresh.textContent = ok ? "Updated" : "No answer";
    setTimeout(() => { refresh.textContent = "Refresh"; refresh.disabled = false; }, 1500);
  });
  const sleep = $("sleep");
  if (sleep) sleep.addEventListener("click", () => setSleep(true));
  const shutdown = $("shutdown");
  if (shutdown) shutdown.addEventListener("click", openShutdown);
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
    if (settings && settings.display && settings.display.main_clock) remember("hoc.mainClock", settings.display.main_clock);
    state.receivedAt = Date.now();
    $("service-down").hidden = true;
    renderStatusBar();
    renderBadge();
    syncSleep();
    syncNightStyle();
    screen.render();
    maybeReturnHome();
    return true;
  } catch (e) {
    $("service-down").hidden = false;
    return false;
  }
}

/* A screen script calls this once with its drawing functions:
   render() after every poll, tick() every second, slow() every 10 s (ages). */
function startScreen(fns) {
  Object.assign(screen, fns);
  setUpClockTap();
  setUpToolbar();
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
