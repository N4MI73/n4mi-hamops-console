/* Ham Ops Console -- Settings (tabs approved 2026-11-05; main clock settled 2026-11-11).

   Every change is sent at once with PUT /api/settings. The service checks it and saves
   nothing if anything is wrong; its reasons are shown where the change was made. Text
   fields open the on-screen keyboard. Text is set with textContent only. */
"use strict";

const page = {
  tab: recall("hoc.settingsTab", "profile"),
  dragging: false,      // a brightness slider is being moved: don't overwrite it from a poll
  editing: null,        // the field open in the keyboard
  text: "",             // what has been typed
  layer: "lower",       // keyboard layer: lower, upper, symbols
  caps: false,          // stay in capitals (callsign, grid)
  results: null,        // last Test connections reply
  testing: false,
  msgTimer: 0,
};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

const settings = () => state.data.settings || {};
const mode = () => (settings().mode === "field" ? "field" : "home");
const station = () => ((settings().station || {})[mode()]) || {};
const display = () => settings().display || {};
const sources = () => settings().sources || {};

// ----------------------------------------------------------------- saving

function say(text, cls) {
  const m = $("msg");
  m.textContent = text || "";
  m.className = "st-msg " + (cls || "");
  clearTimeout(page.msgTimer);
  if (text) page.msgTimer = setTimeout(() => say(""), 6000);
}

/* The service's reasons start with where the problem is ("station.home.grid: ..."). */
const plain = e => String(e).replace(/^[\w.]+:\s*/, "");

/* PUT a change. Returns {ok, errors}. On success the screen redraws from the saved settings. */
async function save(change) {
  let r, body;
  try {
    r = await fetch("/api/settings", {method: "PUT", headers: {"Content-Type": "application/json"},
                                      body: JSON.stringify(change)});
    body = await r.json();
  } catch (e) {
    return {ok: false, errors: ["Couldn't reach the console service."]};
  }
  if (!r.ok) return {ok: false, errors: (body && body.errors) || [`Not saved (${r.status}).`]};
  const warnings = body.warnings || [];
  delete body.warnings;
  state.data.settings = body;
  if (body.display && body.display.main_clock) remember("hoc.mainClock", body.display.main_clock);
  tickClock();
  renderStatusBar();
  render();
  if (warnings.length) say(warnings.map(plain).join(" "), "warn"); else say("Saved.", "ok");
  // A poll already under way could land with the settings from before this change; ask
  // again shortly so the screen settles on what was saved.
  setTimeout(() => { settingsAt = 0; poll(); }, 800);
  return {ok: true, errors: []};
}

async function saveOrSay(change) {
  const res = await save(change);
  if (!res.ok) say(res.errors.map(plain).join(" "), "bad");
}

// ----------------------------------------------------------------- tabs

function showTab(tab) {
  page.tab = ["profile", "display", "sources", "wifi"].includes(tab) ? tab : "profile";
  remember("hoc.settingsTab", page.tab);
  for (const b of document.querySelectorAll(".st-tabs button")) {
    const on = b.dataset.tab === page.tab;
    b.classList.toggle("current", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
  for (const t of ["profile", "display", "sources", "wifi"]) $("tab-" + t).hidden = t !== page.tab;
}
for (const b of document.querySelectorAll(".st-tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));

// ----------------------------------------------------------------- drawing

function press(id, on) { $(id).setAttribute("aria-pressed", on ? "true" : "false"); }

function renderProfile() {
  press("src-home", mode() === "home");
  $("src-note").textContent = mode() === "home"
    ? "Where the data comes from. Home: PropMon and Weather Sentinel on the home network. Field (the internet, through this Pi) comes with Phase 3."
    : "Where the data comes from. Field: the internet, through this Pi.";
  $("station-head").textContent = `STATION · ${mode().toUpperCase()}`;
  $("f-callsign").textContent = station().callsign || "--";
  $("f-grid").textContent = station().grid || "--";
  const loc = (state.data.weather || {}).location || {};
  const warn = $("grid-warn");
  warn.hidden = !loc.grid_warning;
  warn.textContent = loc.grid_warning || "";
}

function renderDisplay() {
  const d = display();
  const main = mainClock();
  press("mc-utc", main === "utc");
  press("mc-local", main === "local");
  $("mc-preview-head").textContent = `PREVIEW · ${main === "local" ? "LOCAL" : "UTC"} FIRST`;
  $("pv-big").textContent = $("utc").textContent;
  $("pv-small").textContent = $("local").textContent;
  $("pv-card").textContent = `Card: Until ${clockPair(new Date(Date.now() + 78 * 60000).toISOString())}`;
  if (!page.dragging) {
    for (const [id, key] of [["day-b", "day_brightness_pct"], ["night-b", "night_brightness_pct"]]) {
      if (typeof d[key] === "number") $(id).value = d[key];
      $(id + "-val").textContent = typeof d[key] === "number" ? `${d[key]}%` : "--";
    }
  }
  const dc = (state.data.system || {}).display;
  const note = $("bright-note");
  if (dc && dc.control) {
    note.className = "st-note";
    note.textContent = `The screen is at ${dc.night ? "night" : "day"} brightness now. Changes show within a few seconds.`;
  } else {
    note.className = "st-note warn";
    note.textContent = dc
      ? "Saved, but this Pi can't change the screen's brightness yet (the brightness permission isn't installed)."
      : "--";
  }
  press("ns-dim", d.night_style !== "red");
  press("ns-red", d.night_style === "red");
  $("f-night_start").textContent = d.night_start || "--";
  $("f-night_end").textContent = d.night_end || "--";
}

function radioAgeWords(r) {
  if (!r) return "--";
  if (r.updating) return "Updating now…";
  const age = liveAge(r);
  if (age === null) return r.status === "unavailable" ? (r.message || "Not downloaded yet") : "--";
  const days = Math.floor(age / 86400);
  return days === 0 ? "Updated today" : `${days} day${days === 1 ? "" : "s"} old`;
}

function renderSources() {
  const s = sources();
  $("f-propmon_url").textContent = s.propmon_url || "Not set";
  $("f-sentinel_url").textContent = s.sentinel_url || "Not set";
  const t = $("test");
  t.disabled = page.testing;
  t.textContent = page.testing ? "Testing…" : "Test connections";
  const rows = (page.results || []).map(r => {
    const row = el("div", "r");
    row.append(el("span", "dot " + (r.ok ? "good" : "bad")), el("b", "", r.source),
               el("span", "msg", r.ok ? `OK${typeof r.ms === "number" ? ` · ${r.ms} ms` : ""}${r.message ? " · " + r.message : ""}`
                                     : (r.message || "failed")));
    return row;
  });
  $("results").replaceChildren(...rows);
  const r = (state.data.weather || {}).weather_radio;
  $("radio-age").textContent = radioAgeWords(r);
  $("radio-update").disabled = !!(r && r.updating);
}

function renderWifi() {
  const n = ((state.data.system || {}).system || {}).network || {};
  const sig = typeof n.wifi_signal_pct === "number" ? ` · signal ${n.wifi_signal_pct} %` : "";
  $("wifi-now").textContent = n.connection ? `${n.connection}${sig}` : "Not connected";
}

function render() {
  renderProfile();
  renderDisplay();
  renderSources();
  renderWifi();
}

// ----------------------------------------------------------------- choices

$("src-home").addEventListener("click", () => { if (mode() !== "home") saveOrSay({mode: "home"}); });
$("mc-utc").addEventListener("click", () => { if (mainClock() !== "utc") saveOrSay({display: {main_clock: "utc"}}); });
$("mc-local").addEventListener("click", () => { if (mainClock() !== "local") saveOrSay({display: {main_clock: "local"}}); });
$("ns-dim").addEventListener("click", () => saveOrSay({display: {night_style: "dim"}}));
$("ns-red").addEventListener("click", () => saveOrSay({display: {night_style: "red"}}));
$("red-preview").addEventListener("click", () => previewRed(10));

for (const [id, key] of [["day-b", "day_brightness_pct"], ["night-b", "night_brightness_pct"]]) {
  const slider = $(id);
  slider.addEventListener("input", () => { page.dragging = true; $(id + "-val").textContent = `${slider.value}%`; });
  slider.addEventListener("change", async () => {
    await saveOrSay({display: {[key]: Number(slider.value)}});
    page.dragging = false;
    renderDisplay();
  });
}

$("test").addEventListener("click", async () => {
  page.testing = true;
  page.results = null;
  renderSources();
  try {
    const r = await fetch("/api/settings/test", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    const body = await r.json();
    page.results = body.results || [];
  } catch (e) {
    page.results = [{source: "Console service", ok: false, message: "not answering"}];
  }
  page.testing = false;
  renderSources();
});

$("radio-update").addEventListener("click", async () => {
  try {
    const r = await fetch("/api/weather-radio/refresh", {method: "POST"});
    say(r.status === 409 ? "The weather radio list is already updating." : "Updating the weather radio list…", "ok");
  } catch (e) {
    say("Couldn't reach the console service.", "bad");
  }
  setTimeout(poll, 1500);
});

$("done").addEventListener("click", () => {
  let back = "/";
  try {
    const ref = new URL(document.referrer);
    if (ref.origin === location.origin && ref.pathname !== "/settings") back = ref.pathname;
  } catch (e) { /* no referrer: Home */ }
  location.href = back;
});

// ----------------------------------------------------------------- on-screen keyboard

const FIELDS = {
  callsign: {title: "Callsign", hint: "3 to 12 letters, digits or /", caps: true,
             get: () => station().callsign, change: v => ({station: {[mode()]: {callsign: v}}})},
  grid: {title: "Grid", hint: "6 characters for this location, e.g. FM18lv", caps: true,
         get: () => station().grid, change: v => ({station: {[mode()]: {grid: v}}})},
  propmon_url: {title: "PropMon address", hint: "Address and port, e.g. http://<address>:8076",
                get: () => sources().propmon_url, change: v => ({sources: {propmon_url: v}})},
  sentinel_url: {title: "Weather Sentinel address", hint: "Address and port, e.g. http://<address>:8085",
                 get: () => sources().sentinel_url, change: v => ({sources: {sentinel_url: v}})},
  night_start: {title: "Night starts", hint: "24-hour local time, e.g. 22:00",
                get: () => display().night_start, change: v => ({display: {night_start: v}})},
  night_end: {title: "Night ends", hint: "24-hour local time, e.g. 07:00",
              get: () => display().night_end, change: v => ({display: {night_end: v}})},
};

/* Layout from the approved board; ":" and "/" are on the main layer because addresses
   and times need them. */
const LAYERS = {
  lower: ["1234567890", "qwertyuiop", " asdfghjkl ", "⇧zxcvbnm⌫", "§-_/␣.:@"],
  symbols: ["1234567890", "!?#$%&*()=", " ~;'\",+[] ", "⇧<>|\\^{}⌫", "§-_/␣.:@"],
};

function keyRow(chars) {
  const row = el("div", "kb-row");
  for (const c of chars) {
    if (c === " ") { row.append(el("span", "gap")); continue; }
    let b;
    if (c === "⇧") {
      b = el("button", "fn" + (page.layer === "upper" ? " on" : ""), page.layer === "symbols" ? "" : "⇧");
      b.setAttribute("aria-label", "Shift");
      b.style.flex = "1.5 1 0";
      if (page.layer === "symbols") b.disabled = true;
      b.addEventListener("click", () => { page.layer = page.layer === "upper" ? "lower" : "upper"; drawKeys(); });
    } else if (c === "⌫") {
      b = el("button", "fn", "⌫");
      b.setAttribute("aria-label", "Delete");
      b.style.flex = "1.5 1 0";
      b.addEventListener("click", () => { page.text = page.text.slice(0, -1); drawField(); });
    } else if (c === "§") {          // the layer key
      b = el("button", "fn" + (page.layer === "symbols" ? " on" : ""), page.layer === "symbols" ? "ABC" : "#+=");
      b.setAttribute("aria-label", page.layer === "symbols" ? "Letters" : "Symbols");
      b.style.flex = "1.5 1 0";
      b.addEventListener("click", () => {
        page.layer = page.layer === "symbols" ? (page.caps ? "upper" : "lower") : "symbols";
        drawKeys();
      });
    } else if (c === "␣") {
      b = el("button", "fn", "space");
      b.setAttribute("aria-label", "Space");
      b.style.flex = "4 1 0";
      b.addEventListener("click", () => type(" "));
    } else {
      const ch = page.layer === "upper" ? c.toUpperCase() : c;
      b = el("button", "", ch);
      b.setAttribute("aria-label", ch);
      b.addEventListener("click", () => type(ch));
    }
    row.append(b);
  }
  return row;
}

function drawKeys() {
  const rows = page.layer === "symbols" ? LAYERS.symbols : LAYERS.lower;
  $("kb-keys").replaceChildren(...rows.map(keyRow));
}
function drawField() { $("kb-text").textContent = page.text; }

function type(ch) {
  if (page.text.length >= 120) return;
  page.text += ch;
  // Shift is one letter at a time, except in fields that stay in capitals.
  if (page.layer === "upper" && !page.caps) { page.layer = "lower"; drawKeys(); }
  drawField();
}

function openKeyboard(name) {
  const f = FIELDS[name];
  page.editing = name;
  page.text = f.get() || "";
  page.caps = !!f.caps;
  page.layer = f.caps ? "upper" : "lower";
  $("kb-title").textContent = f.title;
  $("kb-hint").textContent = f.hint;
  $("kb-error").textContent = "";
  $("kb-save").disabled = false;
  drawKeys();
  drawField();
  $("kb").hidden = false;
}
function closeKeyboard() { page.editing = null; $("kb").hidden = true; }

async function saveKeyboard() {
  const f = FIELDS[page.editing];
  if (!f) return;
  $("kb-save").disabled = true;
  $("kb-error").textContent = "";
  const res = await save(f.change(page.text.trim()));
  $("kb-save").disabled = false;
  if (res.ok) closeKeyboard();
  else $("kb-error").textContent = res.errors.map(plain).join(" ");
}

for (const name of Object.keys(FIELDS)) $("f-" + name).addEventListener("click", () => openKeyboard(name));
$("kb-cancel").addEventListener("click", closeKeyboard);
$("kb-save").addEventListener("click", saveKeyboard);
$("kb-field").addEventListener("click", () => { page.text = ""; drawField(); });   // touch the box to clear it

// A real keyboard works too (handy at the bench).
document.addEventListener("keydown", e => {
  if ($("kb").hidden) return;
  if (e.key === "Enter") { e.preventDefault(); saveKeyboard(); }
  else if (e.key === "Escape") closeKeyboard();
  else if (e.key === "Backspace") { e.preventDefault(); page.text = page.text.slice(0, -1); drawField(); }
  else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) { e.preventDefault(); page.text += e.key; drawField(); }
});

// ----------------------------------------------------------------- start

showTab(page.tab);
startScreen({
  render() { if (!page.editing) render(); },
  tick() { if (page.tab === "display" && !page.editing) renderDisplay(); },
  slow() { if (!page.editing) renderSources(); },
});
