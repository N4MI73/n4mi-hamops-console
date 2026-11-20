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
  secret: false,        // the keyboard is taking a Wi-Fi password: shown as dots unless Show
  show: false,
};

/* Wi-Fi tab state (2026-11-17). The password lives only in page.text while the keyboard is
   open, is sent once to /api/wifi/join and is then cleared. */
const wf = {data: null, timer: 0, armed: null, armTimer: 0, joinSsid: null, lastJob: null};

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
const fieldDay = () => settings().field_day || {};

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
  clearInterval(wf.timer);
  if (page.tab === "wifi") { loadWifi(); wf.timer = setInterval(loadWifi, 4000); }
}
for (const b of document.querySelectorAll(".st-tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));

// ----------------------------------------------------------------- drawing

function press(id, on) { $(id).setAttribute("aria-pressed", on ? "true" : "false"); }

function renderFieldDay() {
  const fdOn = settings().profile === "field_day";
  press("op-gen", !fdOn);
  press("op-fd", fdOn);
  const fd = fieldDay();
  const dateText = v => {
    if (!v) return "Set date";
    const d = new Date(v.replace("Z", ":00Z"));
    const part = o => d.toLocaleDateString("en-GB", Object.assign({timeZone: "UTC"}, o));
    return `${part({weekday: "short"})} ${part({day: "numeric"})} ${part({month: "short"})} ${d.getUTCFullYear()}`;
  };
  const timeText = v => v ? `${v.slice(11, 16)}Z` : "--";
  $("f-fd_start_date").textContent = dateText(fd.ops_start);
  $("f-fd_start_time").textContent = timeText(fd.ops_start);
  $("f-fd_end_date").textContent = dateText(fd.ops_end);
  $("f-fd_end_time").textContent = timeText(fd.ops_end);
  let note = "Setup counts down to the start of operations.";
  if (fd.ops_start && fd.ops_end) {
    const loc = v => new Date(v.replace("Z", ":00Z")).toLocaleString("en-US",
      {weekday: "short", hour: "numeric", minute: "2-digit", timeZoneName: "short"});
    note += ` Local: ${loc(fd.ops_start)} to ${loc(fd.ops_end)}.`;
  }
  $("fd-local").textContent = note;
}

function renderProfile() {
  renderFieldDay();
  press("src-home", mode() === "home");
  press("src-field", mode() === "field");
  $("src-note").textContent = mode() === "home"
    ? "Where the data comes from. Home: PropMon and Weather Sentinel on the home network."
    : "Where the data comes from. Field: NWS, HamQSL and NOAA over the internet; this Pi rates the bands. Set the Field grid below.";
  $("station-head").textContent = `STATION · ${mode().toUpperCase()}`;
  $("f-callsign").textContent = station().callsign || "--";
  $("f-grid").textContent = station().grid || "--";
  renderGps();
  const loc = (state.data.weather || {}).location || {};
  const warn = $("grid-warn");
  warn.hidden = !loc.grid_warning;
  warn.textContent = loc.grid_warning || "";
}

/* Grid from GPS (Phase 4, Dan 2026-11-18): Field station only. While GPS is chosen,
   the typed grid is the fallback until the GPS has a position. */
function ago(iso) {
  const min = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

function renderGps() {
  const field = mode() === "field";
  const useGps = field && station().grid_source === "gps";
  press("gs-manual", !useGps);
  press("gs-gps", useGps);
  $("gs-gps").disabled = !field;
  $("gs-gps-soon").hidden = field;
  $("grid-label").textContent = useGps ? "Typed grid" : "Grid";
  const note = $("gps-note");
  note.hidden = !useGps;
  if (!useGps) return;
  const g = ((state.data.system || {}).system || {}).gps || {};
  const loc = (state.data.weather || {}).location || {};
  // The service's own answer decides the wording; the GPS block adds the details.
  const from = loc.grid_from || (g.grid ? (g.grid_confirmed === false ? "gps_unconfirmed" : "gps") : "gps_waiting");
  const grid = loc.grid || g.grid;
  let text, warn = false;
  if (from === "gps" || from === "gps_unconfirmed") {
    text = `GPS grid ${grid}`;
    if (from === "gps_unconfirmed") { text += " · from before the restart, not yet confirmed"; warn = true; }
    if (g.fix) text += ` · ${g.fix.toUpperCase()} fix${g.satellites_used ? ` · ${g.satellites_used} satellites` : ""}`;
    else if (from === "gps") { text += ` · no fix now${g.last_fix_at ? `, last ${ago(g.last_fix_at)}` : ""}`; warn = true; }
    if (g.grid_pending) text += ` · checking ${g.grid_pending}`;
  } else {
    text = `Waiting for a GPS position · using the typed grid ${station().grid || "--"}`;
    if (g.message) text += ` · ${g.message}`;
    warn = true;
  }
  note.textContent = text;
  note.classList.toggle("warn", warn);
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
  renderFieldLightning();
}

/* Field lightning (board F): on/off is the console's switch (POST /api/lightning), not a
   setting; Full/Economy is a setting. On is for Field Mode only. */
function renderFieldLightning() {
  const l = satLightning();
  const d = (l && l.detection) || {};
  const field = mode() === "field";
  press("fl-off", !d.on);
  press("fl-on", !!d.on);
  $("fl-on").disabled = !field;
  $("fl-on-soon").hidden = field;
  const level = (settings().field_lightning || {}).level === "economy" ? "economy" : "full";
  press("fl-full", level === "full");
  press("fl-economy", level === "economy");
  const sys = (state.data.system || {}).satellite_lightning;
  const used = sys ? `Used today: ${dash(sys.mb_today)} MB${sys.running ? ` · ${dash(sys.mb_last_hour)} MB in the last hour` : ""}` : "--";
  const why = sys && sys.reader === false ? " · can't read satellite files yet (python3-netcdf4 not installed)" : "";
  $("fl-used").textContent = used + why;
  $("fl-used").className = "st-note st-foot" + (why ? " warn" : "");
}

// ----------------------------------------------------------------- Wi-Fi tab (2026-11-17)

async function wifiCall(path, body) {
  try {
    const r = await fetch(path, {method: body === undefined ? "GET" : "POST",
                                 headers: {"Content-Type": "application/json"},
                                 body: body === undefined ? undefined : JSON.stringify(body)});
    const j = await r.json();
    return r.ok ? {ok: true, body: j} : {ok: false, error: (j && j.error) || `Not done (${r.status}).`};
  } catch (e) {
    return {ok: false, error: "Couldn't reach the console service."};
  }
}

async function loadWifi() {
  const r = await wifiCall("/api/wifi");
  if (!r.ok) return;
  wf.data = r.body.wifi;
  const job = wf.data.job;
  const key = job ? job.ssid + job.state : null;
  if (job && key !== wf.lastJob) {
    if (job.state === "joining") say(`Joining ${job.ssid}…`);
    else if (job.state === "joined") { say(`Joined ${job.ssid}.`, "ok"); wifiCall("/api/wifi/dismiss", {}); }
    else { say(`Couldn't join ${job.ssid}: ${job.message || "no reason given"}`, "bad"); wifiCall("/api/wifi/dismiss", {}); }
  }
  wf.lastJob = key;
  if (page.tab === "wifi") renderWifi();
}

const strength = s => s >= 75 ? "strong" : s >= 50 ? "good" : s >= 25 ? "fair" : "weak";

function bars(n) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", "28"); svg.setAttribute("height", "24"); svg.setAttribute("viewBox", "0 0 28 24");
  svg.setAttribute("role", "img"); svg.setAttribute("aria-label", `Signal ${n} of 4`);
  [[1, 17, 6], [8, 12, 11], [15, 7, 16], [22, 2, 21]].forEach(([x, y, h], i) => {
    const r = document.createElementNS(NS, "rect");
    for (const [k, v] of [["x", x], ["y", y], ["width", 5], ["height", h], ["rx", 1]]) r.setAttribute(k, v);
    r.setAttribute("fill", i < n ? "#E6EBF0" : "#2E3B49");
    svg.append(r);
  });
  return svg;
}

function arrow(down, label, onClick, disabled) {
  const b = el("button", "arrow");
  b.setAttribute("aria-label", label);
  b.disabled = disabled;
  b.innerHTML = `<svg aria-hidden="true" width="22" height="22" viewBox="0 0 20 20"><path d="${down ? "M4 7.5l6 6 6-6" : "M4 12.5l6-6 6 6"}" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"></path></svg>`;
  b.addEventListener("click", onClick);
  return b;
}

function renderWifi() {
  const d = wf.data;
  if (!d) return;
  const can = d.available && d.can_change;
  const blocked = $("wf-blocked");
  blocked.hidden = can;
  blocked.textContent = d.message || "";
  const ids = d.saved.map(s => s.id);
  $("wf-saved").replaceChildren(...(d.saved.length ? d.saved.map((s, i) => {
    const row = el("div", "wf-row" + (s.active ? " on" : ""));
    const text = el("div", "wf-text");
    const sub = el("span", "wf-sub" + (s.active ? " on" : ""));
    sub.append(el("span", "wf-dot"), document.createTextNode(
      s.active ? `Connected · ${strength(s.signal)} signal` : s.in_range ? `In range · ${strength(s.signal)} signal` : "Not in range"));
    text.append(el("span", "wf-name", s.ssid), sub);
    const move = (from, to) => {
      const order = ids.slice();
      order.splice(to, 0, order.splice(from, 1)[0]);
      wifiAct("/api/wifi/order", {ids: order});
    };
    const forget = el("button", "forget" + (wf.armed === s.id ? " armed" : ""), wf.armed === s.id ? "Forget?" : "Forget");
    forget.disabled = !can;
    forget.addEventListener("click", () => {
      if (wf.armed === s.id) { wf.armed = null; wifiAct("/api/wifi/forget", {id: s.id}); return; }
      wf.armed = s.id;                    // two taps: the first only arms it, for 4 seconds
      clearTimeout(wf.armTimer);
      wf.armTimer = setTimeout(() => { wf.armed = null; renderWifi(); }, 4000);
      renderWifi();
    });
    row.append(el("span", "wf-rank", String(s.rank)), text,
               arrow(false, `Move ${s.ssid} up`, () => move(i, i - 1), !can || i === 0),
               arrow(true, `Move ${s.ssid} down`, () => move(i, i + 1), !can || i === ids.length - 1),
               forget);
    return row;
  }) : [el("span", "wf-empty", d.available ? "No saved Wi-Fi networks." : (d.message || "Wi-Fi not available."))]));

  const age = d.scanned_at ? Math.max(0, Math.round((Date.now() - Date.parse(d.scanned_at)) / 1000)) : null;
  $("wf-scanned").textContent = d.scanning ? "NEARBY · SCANNING…"
    : age === null ? "NEARBY · FROM THE LAST SCAN" : age < 120 ? `NEARBY · SCANNED ${age} S AGO` : `NEARBY · SCANNED ${Math.round(age / 60)} MIN AGO`;
  $("wf-scan").disabled = !can || d.scanning;
  const joining = d.job && d.job.state === "joining";
  $("wf-nearby").replaceChildren(...(d.nearby.length ? d.nearby.map(n => {
    const row = el("div", "wf-row");
    const text = el("div", "wf-text");
    const saved = d.saved.find(s => s.id === n.saved_id);
    const sec = n.enterprise ? "Username login · not supported" : n.open ? "Open · no password" : "Password";
    text.append(el("span", "wf-name", n.ssid), el("span", "wf-sub", saved ? `${sec} · saved` : sec));
    const connected = saved && saved.active;
    const b = el("button", "join", connected ? "Joined" : "Join");
    b.disabled = !can || n.enterprise || connected || joining;
    b.addEventListener("click", () => {
      if (saved || n.open) wifiAct("/api/wifi/join", {ssid: n.ssid});
      else openPassword(n.ssid, d.saved.length + 1);
    });
    row.append(bars(n.bars), text, b);
    return row;
  }) : [el("span", "wf-empty", "No networks heard yet. Tap Scan again.")]));
}

async function wifiAct(path, body) {
  const r = await wifiCall(path, body);
  if (!r.ok) say(plain(r.error), "bad");
  if (r.ok && r.body.wifi) { wf.data = r.body.wifi; renderWifi(); }
  else loadWifi();
}

function openPassword(ssid, choice) {
  page.editing = "__wifi";
  wf.joinSsid = ssid;
  page.text = "";
  page.caps = false;
  page.layer = "lower";
  page.secret = true;
  page.show = false;
  $("kb-title").textContent = `Join “${ssid}”`;
  $("kb-hint").textContent = `Saved as choice ${choice} · move it up in the list afterwards`;
  $("kb-error").textContent = "";
  $("kb-save").textContent = "Join network";
  $("kb-save").disabled = false;
  $("kb-show").hidden = false;
  press("kb-show", false);
  drawKeys();
  drawField();
  $("kb").hidden = false;
}

async function joinWithPassword() {
  const pw = page.text;
  if (pw.length < 8 || pw.length > 63) { $("kb-error").textContent = "A Wi-Fi password is 8 to 63 characters."; return; }
  $("kb-save").disabled = true;
  const r = await wifiCall("/api/wifi/join", {ssid: wf.joinSsid, password: pw});
  $("kb-save").disabled = false;
  if (!r.ok) { $("kb-error").textContent = plain(r.error); return; }
  closeKeyboard();
  say(`Joining ${wf.joinSsid}…`);
  loadWifi();
}

$("wf-scan").addEventListener("click", () => wifiAct("/api/wifi/scan", {}));
$("kb-show").addEventListener("click", () => { page.show = !page.show; press("kb-show", page.show); drawField(); });

function render() {
  renderProfile();
  renderDisplay();
  renderSources();
  if (page.tab === "wifi") renderWifi();
}

// ----------------------------------------------------------------- choices

$("src-home").addEventListener("click", () => { if (mode() !== "home") saveOrSay({mode: "home"}); });
$("src-field").addEventListener("click", () => { if (mode() !== "field") saveOrSay({mode: "field"}); });
$("gs-manual").addEventListener("click", () => {
  if (station().grid_source === "gps") saveOrSay({station: {[mode()]: {grid_source: "manual"}}});
});
$("gs-gps").addEventListener("click", () => {
  if (mode() === "field" && station().grid_source !== "gps") saveOrSay({station: {field: {grid_source: "gps"}}});
});
$("op-gen").addEventListener("click", () => { if (settings().profile === "field_day") saveOrSay({profile: "general_hf"}); });
$("op-fd").addEventListener("click", () => { if (settings().profile !== "field_day") saveOrSay({profile: "field_day"}); });
$("mc-utc").addEventListener("click", () => { if (mainClock() !== "utc") saveOrSay({display: {main_clock: "utc"}}); });
$("mc-local").addEventListener("click", () => { if (mainClock() !== "local") saveOrSay({display: {main_clock: "local"}}); });
$("ns-dim").addEventListener("click", () => saveOrSay({display: {night_style: "dim"}}));
$("ns-red").addEventListener("click", () => saveOrSay({display: {night_style: "red"}}));
$("red-preview").addEventListener("click", () => previewRed(10));
$("fl-on").addEventListener("click", async () => {
  const d = (satLightning() || {}).detection || {};
  if (!d.on && !(await lightningAction("on"))) say("Lightning detection didn't turn on (the console service didn't accept it).", "bad");
});
$("fl-off").addEventListener("click", async () => {
  const d = (satLightning() || {}).detection || {};
  if (d.on && !(await lightningAction("off"))) say("Lightning detection didn't turn off (the console service didn't answer).", "bad");
});
$("fl-full").addEventListener("click", () => saveOrSay({field_lightning: {level: "full"}}));
$("fl-economy").addEventListener("click", () => saveOrSay({field_lightning: {level: "economy"}}));

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
function drawField() { $("kb-text").textContent = page.secret && !page.show ? "•".repeat(page.text.length) : page.text; }

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
function closeKeyboard() {
  page.editing = null;
  if (page.secret) page.text = "";      // the password is never kept
  page.secret = false;
  page.show = false;
  $("kb-show").hidden = true;
  $("kb-save").textContent = "Save";
  $("kb").hidden = true;
}

async function saveKeyboard() {
  if (page.editing === "__wifi") return joinWithPassword();
  const f = FIELDS[page.editing];
  if (!f) return;
  $("kb-save").disabled = true;
  $("kb-error").textContent = "";
  const change = f.change(page.text.trim());
  if (change.error) { $("kb-save").disabled = false; $("kb-error").textContent = change.error; return; }
  const res = await save(change);
  $("kb-save").disabled = false;
  if (res.ok) closeKeyboard();
  else $("kb-error").textContent = res.errors.map(plain).join(" ");
}

for (const name of Object.keys(FIELDS)) $("f-" + name).addEventListener("click", () => openKeyboard(name));

// ----------------------------------------------------------------- date and time picker (2026-11-16)

/* The touch picker for the Field Day operations times (mockup approved 2026-11-13): a
   month calendar (Monday first) and hour/minute steppers, all in UTC, with quick buttons
   for the usual Field Day start times and a summary with the local time and how long
   operations last. Stored as "2027-01-30T19:00Z". */
const dp = {key: null, y: 0, m: 0, d: 1, hh: 19, mm: 0};
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December"];
const fdDate = v => v ? new Date(v.replace("Z", ":00Z")) : null;
const pad2 = n => String(n).padStart(2, "0");

function dpValue() { return `${dp.y}-${pad2(dp.m + 1)}-${pad2(dp.d)}T${pad2(dp.hh)}:${pad2(dp.mm)}Z`; }
function dpOtherKey() { return dp.key === "ops_start" ? "ops_end" : "ops_start"; }

function openPicker(key) {
  dp.key = key;
  const fd = fieldDay();
  let t = fdDate(fd[key]);
  const other = fdDate(fd[dpOtherKey()]);
  if (!t && key === "ops_end" && other) t = new Date(other.getTime() + 24 * 3600e3);   // a day after the start
  if (!t && key === "ops_start" && other) t = new Date(other.getTime() - 24 * 3600e3);
  if (!t) { const n = new Date(); t = new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate(), 19, 0)); }
  dp.y = t.getUTCFullYear(); dp.m = t.getUTCMonth(); dp.d = t.getUTCDate();
  dp.hh = t.getUTCHours(); dp.mm = t.getUTCMinutes() - t.getUTCMinutes() % 5;
  $("dp-title").textContent = key === "ops_start" ? "Operations start" : "Operations end";
  $("dp-error").textContent = "";
  renderPicker();
  $("dp").hidden = false;
}

function dpSetDay(y, m, d) {
  const t = new Date(Date.UTC(y, m, d));
  dp.y = t.getUTCFullYear(); dp.m = t.getUTCMonth(); dp.d = t.getUTCDate();
  renderPicker();
}

function renderPicker() {
  $("dp-month").textContent = `${MONTHS[dp.m]} ${dp.y}`;
  const first = new Date(Date.UTC(dp.y, dp.m, 1));
  const lead = (first.getUTCDay() + 6) % 7;                     // Monday first
  const inMonth = new Date(Date.UTC(dp.y, dp.m + 1, 0)).getUTCDate();
  const rows = Math.ceil((lead + inMonth) / 7);
  const other = fdDate(fieldDay()[dpOtherKey()]);
  const now = new Date();
  const cells = [];
  for (let i = 0; i < rows * 7; i++) {
    const t = new Date(Date.UTC(dp.y, dp.m, 1 - lead + i));
    const b = document.createElement("button");
    const y = t.getUTCFullYear(), m = t.getUTCMonth(), d = t.getUTCDate();
    b.textContent = d;
    const cls = [];
    if (m !== dp.m) cls.push("out");
    if (y === dp.y && m === dp.m && d === dp.d) cls.push("sel");
    else if (other && y === other.getUTCFullYear() && m === other.getUTCMonth() && d === other.getUTCDate()) cls.push("end");
    if (y === now.getUTCFullYear() && m === now.getUTCMonth() && d === now.getUTCDate()) cls.push("today");
    b.className = cls.join(" ");
    b.setAttribute("aria-label", t.toLocaleDateString("en-GB", {weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC"}));
    b.addEventListener("click", () => dpSetDay(y, m, d));
    cells.push(b);
  }
  $("dp-days").replaceChildren(...cells);
  $("dp-days").style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
  $("dp-hh").textContent = pad2(dp.hh);
  $("dp-mm").textContent = pad2(dp.mm);
  for (const b of document.querySelectorAll("#dp-quick button")) press(b.id, Number(b.dataset.h) === dp.hh && dp.mm === 0);

  const t = fdDate(dpValue());
  const day = t.toLocaleDateString("en-GB", {weekday: "short", timeZone: "UTC"}) + " " +
              t.toLocaleDateString("en-GB", {day: "numeric", month: "short", year: "numeric", timeZone: "UTC"});
  $("dp-sum").textContent = `${day} · ${pad2(dp.hh)}:${pad2(dp.mm)}Z`;
  let line = "Local: " + t.toLocaleString("en-US", {weekday: "short", hour: "numeric", minute: "2-digit", timeZoneName: "short"});
  let ok = true;
  if (other) {
    const hours = (dp.key === "ops_end" ? t - other : other - t) / 3600e3;
    if (hours <= 0) { line += " · ends before it starts"; ok = false; }
    else line += ` · operations last ${Number.isInteger(hours) ? hours : hours.toFixed(1)} h`;
  }
  $("dp-local").textContent = line;
  $("dp-local").classList.toggle("warn", !ok);
  $("dp-set").disabled = !ok;
}

function dpStep(field, delta) {
  if (field === "hh") dp.hh = (dp.hh + delta + 24) % 24;
  else dp.mm = (dp.mm + delta + 60) % 60;
  renderPicker();
}

$("dp-prev").addEventListener("click", () => dpSetDay(dp.y, dp.m - 1, Math.min(dp.d, 28)));
$("dp-next").addEventListener("click", () => dpSetDay(dp.y, dp.m + 1, Math.min(dp.d, 28)));
$("dp-hh-up").addEventListener("click", () => dpStep("hh", 1));
$("dp-hh-down").addEventListener("click", () => dpStep("hh", -1));
$("dp-mm-up").addEventListener("click", () => dpStep("mm", 5));
$("dp-mm-down").addEventListener("click", () => dpStep("mm", -5));
for (const b of document.querySelectorAll("#dp-quick button")) {
  b.addEventListener("click", () => { dp.hh = Number(b.dataset.h); dp.mm = 0; renderPicker(); });
}
$("dp-cancel").addEventListener("click", () => { $("dp").hidden = true; dp.key = null; });
$("dp-set").addEventListener("click", async () => {
  $("dp-set").disabled = true;
  const res = await save({field_day: {[dp.key]: dpValue()}});
  if (res.ok) { $("dp").hidden = true; dp.key = null; }
  else { $("dp-error").textContent = res.errors.map(plain).join(" "); $("dp-set").disabled = false; }
});
for (const [id, key] of [["f-fd_start_date", "ops_start"], ["f-fd_start_time", "ops_start"],
                         ["f-fd_end_date", "ops_end"], ["f-fd_end_time", "ops_end"]]) {
  $(id).addEventListener("click", () => openPicker(key));
}
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
  render() { if (!page.editing && !dp.key) render(); },
  tick() { if (page.tab === "display" && !page.editing) renderDisplay(); },
  slow() { if (!page.editing) renderSources(); },
});
