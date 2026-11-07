/* Ham Ops Console -- Home screen logic: the Priority Card and the three tiles.
   Shared parts (status bar, module row and badge, clock, freshness words,
   polling) are in common.js, which loads first. */
"use strict";

const IDLE_BACK_TO_TOP_MS = 30000;     // after a touch, the card returns to the worst item (approved)
const ROTATE_MS = 60000;               // with nothing critical, the card moves on every 60 s (Dan, 2026-11-05)
const FLASH_MAX_MS = 2 * 60 * 1000;    // a critical item flashes for at most 2 minutes (approved)

Object.assign(state, {
  index: 0,              // Priority Card page
  topId: null,           // id of the worst card item last time
  shownId: null,         // id of the item on the card, so a new list keeps it in view
  shownSince: 0,         // when the card last changed page (for the 60 s rotation)
  lastTouch: 0,          // last touch of < > or Clear; 0 = no touch pending
  // critical item id -> when this browser first showed it, kept across screens so
  // visiting the Weather page doesn't restart a warning's 2-minute flash.
  firstSeen: recall("hoc.firstSeen", {}),
  clearArmed: 0,         // Clear hold: first tap arms (time), second confirms within 10 s
});

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

  // Which page to show (rules approved 2026-10-12, rotation added 2026-11-05):
  //  - a new worst item jumps to the front;
  //  - a critical item (always ranked first) pins the card: no rotation, even after Acknowledge;
  //  - otherwise the card moves to the next item every 60 s, wrapping round;
  //  - touching < > pauses that; 30 s after the last touch the card goes back to the worst
  //    item and rotation carries on from there. One item (or "All quiet.") never rotates.
  const t = Date.now();
  const n = cards.length;
  const go = i => { state.index = i; state.shownSince = t; };
  if (cards[0].ref.id !== state.topId) { state.topId = cards[0].ref.id; go(0); }
  else {
    const at = cards.findIndex(c => c.ref.id === state.shownId);   // the list may have changed
    if (at >= 0) state.index = at;
    else go(state.index < n ? state.index : 0);                   // its item ended: fresh 60 s
  }
  if (state.lastTouch && t - state.lastTouch > IDLE_BACK_TO_TOP_MS) { state.lastTouch = 0; go(0); }
  const pinned = cards[0].critical;
  if (!state.lastTouch && !pinned && n > 1 && t - state.shownSince >= ROTATE_MS) go((state.index + 1) % n);
  const item = cards[state.index];
  state.shownId = item.ref.id;

  let added = false;
  for (const c of cards) if (c.critical && !(c.ref.id in state.firstSeen)) { state.firstSeen[c.ref.id] = t; added = true; }
  if (added) remember("hoc.firstSeen", state.firstSeen);
  const flashing = item.critical && !item.acknowledged && t - state.firstSeen[item.ref.id] < FLASH_MAX_MS;

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
  if (!lightning || t - state.clearArmed > 10000) state.clearArmed = 0;
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
  renderPriority();
  renderPropagation();
  renderWeather();
  renderSystem();
}

// ----------------------------------------------------------------- actions

$("p-prev").addEventListener("click", () => page(-1));
$("p-next").addEventListener("click", () => page(1));
function page(step) {
  const cards = cardItems(), n = cards.length;
  if (!n) return;
  state.index = (state.index + step + n) % n;
  state.shownId = cards[state.index].ref.id;
  state.shownSince = Date.now();
  touched();
}
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
function touched() { state.lastTouch = Date.now(); state.clearArmed = 0; renderPriority(); }

startScreen({
  render: renderAll,
  tick: renderPriority,
  slow: () => { renderPropagation(); renderWeather(); renderSystem(); },
});
