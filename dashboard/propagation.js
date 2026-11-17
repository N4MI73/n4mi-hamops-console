/* Ham Ops Console -- Propagation page: band conditions (good -> fair -> poor, in the
   order the service sends them) and the solar and geomagnetic figures.
   Shared parts (status bar, module row and badge, clock, freshness words, polling)
   are in common.js, which loads first. Freshness comes from the console service;
   this page only puts it into words. */
"use strict";

// ----------------------------------------------------------------- words for the figures

/* NOAA's geomagnetic scale for the K index: 5 and up is a storm, G1 (minor) to G5 (extreme). */
function kWords(k) {
  if (typeof k !== "number") return null;
  if (k <= 2) return "quiet";
  if (k === 3) return "unsettled";
  if (k === 4) return "active";
  return `storm G${Math.min(5, Math.floor(k) - 4)}`;
}

/* NOAA's radio blackout scale from the X-ray class: M1 and up, R1 (minor) to R4 (severe). */
function xrayWords(xray) {
  const m = /^([ABCMX])(\d+(?:\.\d+)?)/i.exec(xray || "");
  if (!m) return null;
  const cls = m[1].toUpperCase(), n = Number(m[2]);
  if ("ABC".includes(cls)) return "no blackout";
  if (cls === "M") return n < 5 ? "blackout R1" : "blackout R2";
  return n < 10 ? "blackout R3" : "blackout R4";
}

/* The same "disturbed" thresholds the alert list uses (settings), so the page and
   the Priority Card agree. */
function thresholds() {
  const t = ((state.data.settings || {}).thresholds) || {};
  return {k: typeof t.prop_bad_k_index === "number" ? t.prop_bad_k_index : 5,
          xray: Array.isArray(t.prop_bad_xray_classes) ? t.prop_bad_xray_classes : ["M", "X"]};
}

/* PropMon's `period`: the time of day where it rates (40m-6m; 160m and 80m switch
   only day/night). */
const PERIOD_WORDS = {dawn: "DAWN", day: "DAYTIME", dusk: "DUSK", night: "NIGHT"};

/* PropMon rates for the home station's location (its own sun times), so at home the footnote
   names the home grid square, 4 characters (the 6-character grid stays private). In Field
   Mode the console rates the Field grid itself and says which (`rated_for`) and on which
   scale (DX, or Field Day: North America reach). 2026-11-17. */
function ratingsNote(p) {
  const field = p && p.source !== "PropMon" && p.scale;
  if (field) {
    if (p.ratings_available === false)
      return "HamQSL isn't sending solar flux and K just now, so the bands aren't rated.";
    const where = p.rated_for ? ` for ${p.rated_for}` : "";
    return p.scale === "field_day"
      ? `Field Day ratings${where}: North America reach, from solar flux, K index and time of day`
      : `Ratings${where} from solar flux, K index and time of day`;
  }
  const st = (((state.data.settings || {}).station) || {}).home || {};
  const square = typeof st.grid === "string" && /^[A-R]{2}\d{2}/i.test(st.grid)
    ? st.grid.slice(0, 2).toUpperCase() + st.grid.slice(2, 4) : null;
  if (p && p.ratings_available === false)
    return "PropMon can't reach HamQSL just now, so it isn't rating bands. Figures from NOAA.";
  return square ? `Ratings for ${square} from solar flux, K index and time of day`
                : "Band ratings from PropMon: solar flux, K index and time of day";
}

/* Lines under the bands: a band's own note (6m on the Field Day scale), and the field
   engine's version check when a newer model exists. */
function bandNotes(p) {
  const lines = [];
  if (p && p.status !== "unavailable" && p.note) lines.push(`${p.note}.`);
  for (const b of (p && p.status !== "unavailable" && p.bands) || []) if (b.note) lines.push(`${b.band}: ${b.note}.`);
  const e = p && p.engine;
  if (e && e.status === "out_of_date" && e.message) lines.push(`${e.message}.`);
  return lines;
}

/* Help follows the scale on screen: the Field Day meanings come from the model file. */
function renderHelp(p) {
  const fd = p && p.scale === "field_day" && p.meanings;
  $("help-meanings").innerHTML = "";
  const add = (cls, word, text) => {
    const b = el("b", cls, word);
    $("help-meanings").append(b, document.createTextNode(": " + text), el("br"));
  };
  if (fd) {
    $("help-lead").textContent = "Field Day scale: North America reach on the Field Day bands (no WARC), right now:";
    add("g", "GOOD", fd.good); add("f", "FAIR", fd.fair); add("p", "POOR", fd.poor);
    $("help-meanings").append(document.createTextNode("6m always reads POOR (openings can't be forecast). Not yet checked against June (ARRL Field Day)."));
  } else {
    $("help-lead").textContent = "GOOD · FAIR · POOR for each band, right now:";
    add("g", "GOOD", "DX is workable on SSB.");
    add("f", "FAIR", "DX on FT8 only, or North America only on SSB. DX is often workable on FT8 even when SSB is quiet or North America only.");
    add("p", "POOR", "North America only, or nothing, even on FT8.");
  }
  $("help-source").textContent = p && p.source && p.source !== "PropMon"
    ? "In Field Mode this Pi rates the bands itself with PropMon's model: solar flux and K from HamQSL, NOAA's storm history, and the time of day at the grid square named under the bands. A guide to where to listen first."
    : "The ratings come from PropMon on the home server, worked out from solar flux, the K index and the time of day at the grid square named under the bands. They describe how open each band is in general, not a path to one place.";
}

function list(names) {
  return names.length < 2 ? names.join("") : names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
}

// ----------------------------------------------------------------- render

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderBands() {
  const p = (state.data.propagation || {}).propagation;
  setFresh($("bands-fresh"), p);
  const show = p && p.status !== "unavailable";
  $("t-bands").classList.toggle("is-stale", !!p && p.status === "stale");
  const grid = $("band-grid");
  const bands = show ? (p.bands || []) : [];
  const period = show && PERIOD_WORDS[p.period];
  $("bands-title").textContent = "BAND CONDITIONS · NOW" + (period ? " · " + period : "");
  $("bands-note").textContent = show || !p || p.source === "PropMon" ? ratingsNote(show ? p : null)
    : "Field Mode: bands rated on this Pi from HamQSL and NOAA";
  const notes = bandNotes(p);
  $("band-notes").hidden = !notes.length;
  $("band-notes").textContent = notes.join(" ");
  renderHelp(p);
  if (!bands.length) {
    $("best").textContent = "Band conditions not available";
    grid.replaceChildren(el("div", "band-msg", (p && p.message) || "No data from PropMon yet"));
    return;
  }
  if (p.ratings_available === false) {
    $("best").textContent = "Band ratings unavailable";
    grid.replaceChildren(...bands.map(b => {
      const cell = el("div", "cell");
      cell.append(el("b", "", b.band), el("span", "", "--"));
      return cell;
    }));
    return;
  }
  const good = bands.filter(b => b.rating === "good").map(b => b.band);
  const fair = bands.filter(b => b.rating === "fair").map(b => b.band);
  $("best").textContent = good.length ? `Best now: ${list(good)}`
    : fair.length ? `No band rated good · fair: ${list(fair)}` : "No band rated good or fair now";
  grid.replaceChildren(...bands.map(b => {
    const cell = el("div", "cell " + (b.rating || ""));
    cell.append(el("b", "", b.band), el("span", "", b.rating ? b.rating.toUpperCase() : "--"));
    return cell;
  }));
}

function renderSolar() {
  const p = (state.data.propagation || {}).propagation;
  const show = p && p.status !== "unavailable";
  $("t-solar").classList.toggle("is-stale", !!p && p.status === "stale");
  const v = show ? p : {};
  const t = thresholds();
  const k = kWords(v.k_index), x = xrayWords(v.xray);
  const rows = [
    ["Solar flux (SFI)", dash(v.sfi)],
    ["Sunspot number", dash(v.sunspots)],
    ["K index", v.k_index === null || v.k_index === undefined ? "--" : `${v.k_index}${k ? " · " + k : ""}`,
     typeof v.k_index === "number" && v.k_index >= t.k],
    ["A index", dash(v.a_index)],
    ["X-ray", v.xray ? `${v.xray}${x ? " · " + x : ""}` : "--",
     !!v.xray && t.xray.includes(v.xray.charAt(0).toUpperCase())],
    ["Solar wind", typeof v.solar_wind_kms === "number" ? `${Math.round(v.solar_wind_kms)} km/s` : "--"],
  ];
  /* After a storm (any 3-hour Kp of 5 or more) PropMon holds ratings down for 12 hours,
     so poor bands then are expected, not a fault (Ham Shack Automation, 2026-11-07). */

  $("solar").replaceChildren(...rows.flatMap(([name, value, disturbed]) =>
    [el("dt", "", name), el("dd", disturbed ? "fair" : "", value)]));
  const rec = $("recovery");
  rec.hidden = !(v.recovery_active === true && v.recovery_ends);
  const held = p && p.source === "PropMon" ? "PropMon holds ratings down" : "ratings held down";
  rec.textContent = rec.hidden ? "" : `Storm recovery: ${held} until ${clockPair(v.recovery_ends)}.`;
}

function renderAll() {
  renderBands();
  renderSolar();
}

startScreen({
  render: renderAll,
  slow: renderBands,       // the "N min ago" words
});
