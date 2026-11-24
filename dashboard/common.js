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
   the day, then Setup counting down to the start of operations (from Setup start, or
   local midnight on the day when that is blank -- the service works it out), then Operations counting down to the end, then
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
  } else if (now < setup && start - now >= 864e5) {
    // A day or more away: the date and how many days (Dan, 2026-11-16).
    cls += " quiet";
    const day = start.toLocaleDateString("en-GB", {weekday: "short", day: "numeric", month: "short", timeZone: "UTC"});
    const days = Math.floor((start - now) / 864e5);
    const away = `${days} day${days === 1 ? "" : "s"} away`;
    parts = [span("fd-day", day), span("fd-word", away)];
    label = `Field Day ${day}, ${away}, operations start ${fdUtcHM(start)}`;
  } else if (now < start) {
    // Under a day: hours and minutes until operations; "Setup" once setup has begun
    // (Setup start, or local midnight on the day). A setup started more than a day ahead
    // reads e.g. "Setup 30:12 until operations". In the last hour the pill fills in solid blue.
    const left = fdHM(start - now);
    const inSetup = now >= setup;
    if (start - now <= 36e5) cls += " soon";
    parts = [...(inSetup ? [span("fd-word", "Setup")] : []), span("fd-num num", left), span("fd-dim", "until operations")];
    label = `Field Day${inSetup ? " setup" : ""}, ${left} until operations`;
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
  // While panel cycling is on, its own 3-minute resume replaces this on the screens it
  // cycles through (the same 3 minutes, counted from the last touch on any screen).
  if (cyclingOn() && CYCLE_AT >= 0) return;
  if (Date.now() - state.lastActivity > DETAIL_IDLE_MS) location.href = "/";
}
for (const ev of ["pointerdown", "keydown"]) {
  document.addEventListener(ev, () => {
    state.lastActivity = Date.now();
    if (cyclingOn()) { const c = recall("hoc.cycle", {}); c.touchAt = Date.now(); remember("hoc.cycle", c); }
  }, {passive: true, capture: true});
}

// ----------------------------------------------------------------- panel cycling (2026-11-24)

/* Panel cycling (Dan, 2026-11-22; boards CycleSettings, CycleHome and CyclePaused approved):
   a Settings switch (display.panel_cycling) that shows Home, Propagation and Weather in
   turn, 60 seconds each, for a tent display or a mirrored monitor.
   - Any touch pauses it, so the page never changes under a finger; 3 minutes after the
     last touch (on any screen) it carries on from Home.
   - Safety rule: while an unacknowledged critical item is in the list it stays on Home
     (going there at once if it isn't), so the Priority Card with Acknowledge is what
     shows; once acknowledged it carries on. A new critical item still sends a paused
     screen Home at once (maybeReturnHome), as it always has.
   - It doesn't move while the screen is asleep.
   - "Resume normal mode" in the bottom toolbar of the three screens turns it off.
   Which screen it is on, when it got there and the last touch are kept in this browser's
   session memory, since each screen is its own page. */
const CYCLE = [["home", "/", "Home"], ["propagation", "/propagation", "Propagation"], ["weather", "/weather", "Weather"]];
const CYCLE_MS = 60 * 1000;
const CYCLE_PAUSE_MS = 3 * 60 * 1000;
const CYCLE_AT = CYCLE.findIndex(c => c[0] === PAGE);

function cyclingOn() {
  const d = state.data.settings && state.data.settings.display;
  if (d && typeof d.panel_cycling === "boolean") return d.panel_cycling;
  return recall("hoc.cycling", false) === true;        // before the settings arrive
}

function criticalWaiting() {
  return alertItems().some(i => i.critical && !i.acknowledged);
}

function mmss(ms) {
  const sec = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

let cycleBar = null;
function cycleBarEls() {
  if (cycleBar || CYCLE_AT < 0) return cycleBar;
  const shutdown = $("shutdown");
  if (!shutdown) return null;
  const box = document.createElement("div");
  box.className = "cyc-status";
  box.setAttribute("role", "status");
  box.hidden = true;
  const dot = document.createElement("span");
  const words = document.createElement("div");
  words.className = "cyc-words";
  const head = document.createElement("span");
  head.className = "cyc-head";
  const line = document.createElement("span");
  line.className = "cyc-line";
  words.append(head, line);
  box.append(dot, words);
  const resume = document.createElement("button");
  resume.className = "cyc-resume";
  resume.textContent = "Resume normal mode";
  resume.hidden = true;
  resume.addEventListener("click", stopCycling);
  shutdown.before(box, resume);
  cycleBar = {box, dot, head, line, resume};
  return cycleBar;
}

function showCycleBar(on, ring, head, line) {
  const b = cycleBarEls();
  if (!b) return;
  b.box.hidden = b.resume.hidden = !on;
  if (!on) return;
  b.dot.className = "cyc-dot" + (ring ? " ring" : "");
  b.head.textContent = head;
  b.line.textContent = line;
}

async function stopCycling() {
  try {
    const r = await fetch("/api/settings", {method: "PUT", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify({display: {panel_cycling: false}})});
    if (r.ok) {
      const body = await r.json();
      delete body.warnings;
      state.data.settings = body;
      remember("hoc.cycling", false);
      // A poll already under way could land with the settings from before this change;
      // ask again so the screen settles on what was saved (as Settings does).
      settingsAt = 0;
      setTimeout(poll, 800);
    }
  } catch (e) { /* the service didn't answer: the switch stays as it was */ }
  cycleTick();
}

let lastPollOk = 0;            // when the console service last answered a poll

function cycleGo(c, index, now) {
  // Each screen is served by the console service: moving while it doesn't answer (e.g. a
  // restart after an update) would leave Chromium on its "can't connect" page, which
  // nothing recovers. Stay put; the service-down line shows meanwhile.
  if (now - lastPollOk > 20000) return;
  c.page = CYCLE[index][0];
  c.since = now;
  remember("hoc.cycle", c);
  location.href = CYCLE[index][1];
}

function cycleTick() {
  const on = cyclingOn() && CYCLE_AT >= 0;
  if (!on) { showCycleBar(false); return; }
  if (!state.receivedAt) return;                  // nothing decided before the first answer
  const now = Date.now();
  const c = recall("hoc.cycle", {});
  if (c.page !== PAGE || !c.since || c.since > now) { c.page = PAGE; c.since = now; remember("hoc.cycle", c); }
  if (c.touchAt && c.touchAt > now) { c.touchAt = null; remember("hoc.cycle", c); }   // the clock stepped back

  if (c.touchAt && now - c.touchAt < CYCLE_PAUSE_MS) {
    showCycleBar(true, true, "CYCLING PAUSED", `Resumes in ${mmss(CYCLE_PAUSE_MS - (now - c.touchAt))}`);
    return;
  }
  if (c.touchAt) {                                // the pause is over: carry on from Home
    c.touchAt = null;
    if (PAGE !== "home") { cycleGo(c, 0, now); return; }
    c.since = now;
    remember("hoc.cycle", c);
  }
  if (criticalWaiting()) {
    if (PAGE !== "home") { cycleGo(c, 0, now); return; }
    c.since = now;                                // a full minute on Home once acknowledged
    remember("hoc.cycle", c);
    showCycleBar(true, true, "CYCLING HELD", "Until the alert is acknowledged");
    return;
  }
  const disp = (state.data.system || {}).display;
  if (disp && disp.asleep) {                      // nobody is looking: don't move
    c.since = now;
    remember("hoc.cycle", c);
    return;
  }
  const next = (CYCLE_AT + 1) % CYCLE.length;
  const left = CYCLE_MS - (now - c.since);
  if (left <= 0) { cycleGo(c, next, now); return; }
  showCycleBar(true, false, `CYCLING · ${CYCLE_AT + 1} OF ${CYCLE.length}`, `${CYCLE[next][2]} next · ${mmss(left)}`);
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

// ----------------------------------------------------------------- satellite lightning (2026-11-20)

/* Field Mode lightning detection (GOES satellite): the switch, shared by the Home card,
   the Weather page and Settings. POST /api/lightning, then a fresh poll. */
async function lightningAction(action) {
  let ok = false;
  try {
    const r = await fetch("/api/lightning", {method: "POST", headers: {"Content-Type": "application/json"},
                                             body: JSON.stringify({action})});
    ok = r.ok;
  } catch (e) { /* the service is restarting or down: the next poll shows the real state */ }
  poll();
  return ok;            // never throws, so no button is left disabled
}
function satLightning() {               // the Field lightning block, or null at home
  const l = (state.data.weather || {}).lightning;
  return l && l.source === "GOES satellite" ? l : null;
}
const SAT_MB_HOUR = {full: 60, economy: 20};

/* The keep-on question (Dan, 2026-11-20): 30 minutes after a hand turn-on with no NWS
   lightning outlook, and once more at 3 hours after Keep on. It sits below the Priority
   Card, so the card stays in view; it never appears while a critical alert is
   unacknowledged (it waits for that alert to be acknowledged or end). Shown on the Home
   screen only, so it never covers the Weather page's lightning row or the Alerts list;
   detail pages return Home after 3 minutes untouched. No answer: detection stays on,
   and turns itself off at the 6-hour limit. */
let keepOnBox = null, keepOnStage = null;
function renderKeepOn() {
  const l = satLightning();
  const q = l && l.detection && l.detection.on ? l.detection.question : null;
  const blocked = alertItems().some(i => i.critical && !i.acknowledged);
  if (!q || blocked || PAGE !== "home") {
    if (keepOnBox) { keepOnBox.remove(); keepOnBox = keepOnStage = null; }
    return;
  }
  if (keepOnBox && keepOnStage === q.stage) { placeKeepOn(); return; }
  if (keepOnBox) keepOnBox.remove();
  const d = l.detection;
  const off = d.auto_off_at ? mainTime(new Date(d.auto_off_at)) : null;
  const box = document.createElement("div");
  box.className = "ko-overlay";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  const panel = document.createElement("div");
  panel.className = "ko-panel";
  const head = document.createElement("div");
  head.className = "ko-head";
  head.textContent = "LIGHTNING DETECTION · GOES SATELLITE";
  const title = document.createElement("div");
  title.className = "ko-title";
  title.textContent = q.stage === "3h"
    ? "Lightning detection has been on for 3 hours with no storms forecast. Keep it on?"
    : "Lightning detection has been on for 30 min with no storms forecast. Keep it on?";
  const note = document.createElement("div");
  note.className = "ko-note";
  const level = d.level === "economy" ? "Economy" : "Full";
  note.textContent = `${level} · about ${SAT_MB_HOUR[d.level] || 60} MB an hour. `
    + (q.stage === "3h"
      ? `If kept on, it turns itself off at ${off || "the 6-hour limit"}.`
      : `If nobody answers, it stays on${off ? ` and turns itself off at ${off}` : ""}.`);
  const row = document.createElement("div");
  row.className = "ko-buttons";
  const button = (text, cls, action) => {
    const b = document.createElement("button");
    b.className = cls;
    b.textContent = text;
    b.addEventListener("click", async () => {
      for (const x of row.querySelectorAll("button")) x.disabled = true;
      // Always taken down: if the answer didn't reach the service, the next poll
      // brings the question back with live buttons.
      box.remove(); keepOnBox = keepOnStage = null;
      await lightningAction(action);
    });
    row.append(b);
  };
  button("Keep on", "ko-keep", "keep_on");
  button("Turn off", "ko-off", "off");
  panel.append(head, title, note, row);
  box.append(panel);
  document.body.append(box);
  keepOnBox = box; keepOnStage = q.stage;
  placeKeepOn();
}
/* Below the Priority Card, measured each poll in case the card's height changes. */
function placeKeepOn() {
  const above = $("priority");
  if (keepOnBox && above) keepOnBox.style.top = `${Math.round(above.getBoundingClientRect().bottom) + 6}px`;
}

// ----------------------------------------------------------------- GPS details (2026-11-18)

/* Approved mockup "GPS details" (Dan, 2026-11-18): opened by tapping the GPS light on any
   screen, or the GPS row on the System page. Sky plot (centre overhead, outer ring the
   horizon; green = used in the fix, grey ring = heard, not used), the details, and one
   signal bar per satellite. Reads GET /api/gps every 2 s while open. */
let gpsBox = null, gpsTimer = null;

function gEl(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function gpsSky(sats) {
  const ns = "http://www.w3.org/2000/svg", cx = 190, cy = 190, R = 170;
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", "380"); svg.setAttribute("height", "380"); svg.setAttribute("viewBox", "0 0 380 380");
  const add = (tag, attrs, text) => {
    const e = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    svg.append(e);
  };
  for (const elv of [0, 30, 60]) {
    const r = R * (90 - elv) / 90;
    add("circle", {cx, cy, r, fill: "none", stroke: "#2A3542", "stroke-width": elv ? 1 : 2});
    if (elv) add("text", {x: cx + 4, y: cy - r + 16, fill: "#5C6B7A", "font-size": 14}, `${elv}°`);
  }
  add("line", {x1: cx, y1: cy - R, x2: cx, y2: cy + R, stroke: "#1E2731"});
  add("line", {x1: cx - R, y1: cy, x2: cx + R, y2: cy, stroke: "#1E2731"});
  for (const [t, x, y] of [["N", cx, cy - R - 4], ["S", cx, cy + R + 18], ["E", cx + R + 12, cy + 6], ["W", cx - R - 14, cy + 6]])
    add("text", {x, y, fill: "#A3B0BE", "font-size": 17, "font-weight": 700, "text-anchor": "middle"}, t);
  for (const s of sats) {
    if (typeof s.az !== "number" || typeof s.el !== "number" || s.el < 0) continue;
    const r = R * (90 - Math.min(s.el, 90)) / 90, a = s.az * Math.PI / 180;
    const x = Math.round(cx + r * Math.sin(a)), y = Math.round(cy - r * Math.cos(a));
    add("circle", {cx: x, cy: y, r: 15, fill: s.used ? "#3FCF6E" : "none", stroke: s.used ? "#3FCF6E" : "#7D8B99", "stroke-width": 2});
    add("text", {x, y: y + 5, fill: s.used ? "#06090D" : "#C9D3DD", "font-size": 14, "font-weight": 700, "text-anchor": "middle"}, String(s.prn));
  }
  return svg;
}

function gpsMinutes(sec) {
  if (typeof sec !== "number") return null;
  return sec < 90 ? `${Math.round(sec)} s` : `${Math.round(sec / 60)} min`;
}

function renderGpsPanel(g, loc) {
  const panel = gpsBox.querySelector(".g-panel");
  const fixed = !!g.fix;
  panel.className = "g-panel " + (fixed ? "good" : "fair");
  const chip = gpsBox.querySelector(".g-chip");
  chip.className = "g-chip " + (fixed ? "good" : "fair");
  chip.textContent = fixed ? `${g.fix.toUpperCase()} FIX` : (g.present ? "NO FIX" : "NOT CONNECTED");
  gpsBox.querySelector(".g-sky-plot").replaceChildren(gpsSky(g.satellites || []));

  const sats = g.satellites || [];
  const used = sats.filter(s => s.used).length;
  const heard = sats.length || g.satellites_seen || 0;
  const span = (text, cls) => gEl("span", cls || "", text);
  const rows = [];
  rows.push(["Satellites", heard
    ? [span(`${used || g.satellites_used || 0} used`, used ? "g-good" : ""), span(` of ${heard} heard`)]
    : [span(g.present ? "none heard" : "--", "g-fair")]]);
  const from = {gps: "from GPS", gps_unconfirmed: "from GPS · not yet confirmed",
                gps_waiting: "typed (waiting for GPS)", manual: "typed"}[loc.grid_from] || "";
  rows.push(["Grid in use", [span(`${dash(loc.grid)} · ${loc.label || ""}${from ? " · " + from : ""}`,
                                  loc.grid_from === "gps_unconfirmed" || loc.grid_from === "gps_waiting" ? "g-fair" : "")]]);
  rows.push(["Position", [span(typeof g.lat === "number"
    ? `${Math.abs(g.lat).toFixed(4)}° ${g.lat >= 0 ? "N" : "S"} · ${Math.abs(g.lon).toFixed(4)}° ${g.lon >= 0 ? "E" : "W"}` : "--")]]);
  rows.push(["Altitude", [span(typeof g.alt_m === "number" ? `${Math.round(g.alt_m)} m (${Math.round(g.alt_m * 3.28084)} ft)` : "--")]]);
  const acc = [typeof g.accuracy_m === "number" ? `about ±${Math.round(g.accuracy_m)} m` : null,
               typeof g.hdop === "number" ? `HDOP ${g.hdop.toFixed(1)}` : null].filter(Boolean).join(" · ");
  rows.push(["Accuracy", [span(acc || "--")]]);
  let timeSpans;
  if (g.time_check === "agrees") timeSpans = [span(`${utcClock(new Date(g.gps_time))} · `), span("agrees with the Pi clock", "g-good")];
  else if (g.time_check === "differs") timeSpans = [span(`differs from the Pi clock by ${Math.abs(Math.round(g.time_offset_sec))} s`, "g-fair")];
  else if (g.time_check === "gps_wrong") timeSpans = [span("GPS date wrong · ignored (the Pi clock is used)", "g-fair")];
  else timeSpans = [span(fixed ? "--" : "not checked without a fix", fixed ? "" : "muted")];
  rows.push(["GPS time", timeSpans]);
  if (fixed || g.last_fix_at) {
    const ago = g.last_fix_at ? gpsMinutes((Date.now() - Date.parse(g.last_fix_at)) / 1000) : null;
    const first = gpsMinutes(g.first_fix_sec);
    rows.push(["Last fix", [span([ago ? `${ago} ago` : "--", first ? `first fix ${first} after start` : null].filter(Boolean).join(" · "))]]);
  } else if (g.searching_since) {
    rows.push(["Searching", [span(`${gpsMinutes((Date.now() - Date.parse(g.searching_since)) / 1000)} since the receiver started`)]]);
  }
  const d = g.device || {};
  const recv = [d.driver, d.path, d.bps ? `${d.bps} baud` : null, g.gpsd_release ? `gpsd ${g.gpsd_release}` : null].filter(Boolean).join(" · ");
  rows.push(["Receiver", [span(recv || g.message || "--", recv ? "" : "g-fair")]]);
  gpsBox.querySelector(".g-facts").replaceChildren(...rows.flatMap(([k, v]) => [gEl("span", "", k), gEl("span", "", "")].map((e, i) => {
    if (i === 1) e.append(...v);
    return e;
  })));

  const bars = gpsBox.querySelector(".g-bars");
  if (!sats.length) bars.replaceChildren(gEl("div", "g-empty", g.present ? "No satellites heard" : "No GPS receiver reporting"));
  else bars.replaceChildren(...[...sats].sort((a, b) => (b.ss || 0) - (a.ss || 0)).slice(0, 18).map(s => {
    const bar = gEl("div", "g-bar");
    const well = gEl("div", "g-well"), fill = gEl("div");
    fill.style.height = `${Math.max(4, Math.min(100, (s.ss || 0) * 2))}%`;
    fill.style.background = s.used ? "var(--good)" : "var(--grey)";
    well.append(fill);
    bar.append(gEl("span", "g-snr num", s.ss ? String(Math.round(s.ss)) : "–"), well, gEl("span", "g-prn num", String(s.prn)));
    return bar;
  }));
  const tip = gpsBox.querySelector(".g-tip");
  tip.hidden = fixed || heard > 0 && g.present;
  tip.textContent = !g.present
    ? "No GPS receiver reporting. Check that it is plugged into a black (USB 2) port; the console finds it by itself when plugged in."
    : "No satellites heard. Put the receiver where it can see open sky: outside, or flat on a window sill away from metal and coated glass. A first fix can take 15 minutes. Still nothing after 15 minutes outside: unplug it for a minute and plug it back in.";
}

async function refreshGpsPanel() {
  if (!gpsBox) return;
  try {
    const r = await getJSON("/api/gps");
    if (gpsBox) renderGpsPanel(r.gps || {}, (state.data.weather && state.data.weather.location) || {});
  } catch (e) {
    if (gpsBox) gpsBox.querySelector(".g-facts").replaceChildren(gEl("span", "", "GPS"), gEl("span", "g-fair", "The console service didn't answer."));
  }
}

function closeGps() {
  if (gpsTimer) clearInterval(gpsTimer);
  gpsTimer = null;
  if (gpsBox) gpsBox.remove();
  gpsBox = null;
}

function openGps() {
  if (gpsBox) return;
  gpsBox = gEl("div", "g-back");
  gpsBox.setAttribute("role", "dialog");
  gpsBox.setAttribute("aria-modal", "true");
  gpsBox.setAttribute("aria-label", "GPS details");
  gpsBox.innerHTML = `<div class="g-panel fair">
      <div class="g-head"><h2>GPS</h2><span class="g-chip fair">…</span><button class="g-close">Close</button></div>
      <div class="g-body">
        <div class="g-sky"><div class="g-sky-plot"></div>
          <div class="g-legend"><span><i class="used"></i>used in the fix</span><span><i class="heard"></i>heard, not used</span></div>
          <div class="g-note">Centre = overhead · outer ring = horizon</div></div>
        <div class="g-right"><div class="g-facts"></div>
          <div class="g-sub">SIGNAL STRENGTH · dB-Hz (30+ is good)</div>
          <div class="g-bars"></div><div class="g-tip" hidden></div></div>
      </div></div>`;
  gpsBox.querySelector(".g-close").addEventListener("click", closeGps);
  gpsBox.addEventListener("click", e => { if (e.target === gpsBox) closeGps(); });
  document.body.append(gpsBox);
  gpsBox.querySelector(".g-sky-plot").replaceChildren(gpsSky([]));
  refreshGpsPanel();
  gpsTimer = setInterval(refreshGpsPanel, 2000);
}

function setUpGpsPill() {
  const p = $("pill-gps");
  if (!p) return;
  p.setAttribute("role", "button");
  p.classList.add("tappable");
  p.addEventListener("click", openGps);
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
  setUpHelp();
}

/* Help (style approved 2026-11-13): pages with a #help panel get a live Help button;
   the panel covers the page until Close. Pages without one keep "Help · Coming". */
function setUpHelp() {
  const panel = $("help"), open = $("help-open"), close = $("help-close");
  if (!panel || !open || !close) return;
  open.addEventListener("click", () => { panel.hidden = false; close.focus(); });
  close.addEventListener("click", () => { panel.hidden = true; });
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
    if (settings && settings.display) remember("hoc.cycling", settings.display.panel_cycling === true);
    state.receivedAt = Date.now();
    lastPollOk = state.receivedAt;
    $("service-down").hidden = true;
    renderStatusBar();
    renderBadge();
    syncSleep();
    syncNightStyle();
    screen.render();
    renderKeepOn();
    maybeReturnHome();
    cycleTick();
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
  setUpGpsPill();
  tickClock();
  setInterval(() => {
    tickClock();
    if (state.receivedAt) { renderStatusBar(); screen.tick(); }
    maybeReturnHome();
    cycleTick();
  }, 1000);
  setInterval(() => { if (state.receivedAt) screen.slow(); }, 10000);
  setInterval(poll, POLL_MS);
  poll();
}
