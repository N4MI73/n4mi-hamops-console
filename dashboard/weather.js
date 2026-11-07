/* Ham Ops Console -- Weather page: this location's NWS alerts and lightning, current
   conditions, the NOAA Weather Radio panel, the forecast periods and the next 12 hours.
   Shared parts (status bar, module row and badge, clock, freshness words, polling)
   are in common.js, which loads first. Freshness and alert ranking come from the
   console service; this page only puts them into words. */
"use strict";

const GUST_HIGH_MPH = 25;    // gusts at or above this are marked (approved mockup)

// ----------------------------------------------------------------- small helpers

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function setRow(id, cls, what, note) {
  const row = $(id);
  row.className = "state-row " + (cls || "");
  setDot(row.querySelector(".dot"), cls === "good" ? "good" : cls === "fair" ? "fair" : cls === "bad" ? "bad" : "");
  row.querySelector(".what").textContent = what;
  row.querySelector(".note").textContent = note || "";
}
function whenWords(iso) {           // "now" or "after 3 PM"
  if (!iso) return "";
  const d = new Date(iso);
  return d <= new Date() ? "now" : `after ${localClock(d)}`;
}
function hourLabel(iso) {           // "1P", "12A" -- local hours, as in the mockup
  const h = new Date(iso).getHours();
  return `${h % 12 === 0 ? 12 : h % 12}${h < 12 ? "A" : "P"}`;
}
const isNight = name => /night|tonight/i.test(name || "");
const isStorm = text => /thunder|t-storm/i.test(text || "");

// ----------------------------------------------------------------- This location

function renderHere() {
  const w = state.data.weather || {};
  const a = w.alerts, l = w.lightning, fc = w.forecast;
  setFresh($("here-fresh"), a, "NWS");
  renderLightningRow(l, fc);
  renderAlertsRow(a);
}

function renderLightningRow(l, fc) {
  const outlook = fc && fc.status !== "unavailable" ? fc.lightning_outlook : null;
  const outlookNote = outlook ? `${outlook.level} ${whenWords(outlook.start)} · NWS forecast, not detected` : "none forecast";

  if (l && (l.status === "ok" || l.status === "stale")) {      // Home Mode: the home Tempest
    const stale = l.status === "stale" ? " (STALE)" : "";
    if (l.within_5mi) {
      // The time to stay in comes first in the note, so it's never the part that gets cut.
      setRow("row-lightning", "bad", `Lightning ${dash(l.closest_mi)} mi${stale}`,
        (l.hold_until_5mi ? `Stay in until ${localClock(new Date(l.hold_until_5mi))} · ` : "")
        + `${dash(l.strikes_recent)} in ${dash(l.window_min)} min · Home Tempest`);
    } else if (l.within_10mi) {
      setRow("row-lightning", "fair", `Lightning within 10 mi${stale}`,
        (l.hold_until_10mi ? `Hold until ${localClock(new Date(l.hold_until_10mi))} · ` : "")
        + `closest ${dash(l.closest_mi)} mi · Home Tempest`);
    } else if (outlook) {
      setRow("row-lightning", "fair", `Lightning ${outlook.level} ${whenWords(outlook.start)}`,
        `NWS forecast, not detected · none within 10 mi${stale}`);
    } else {
      const cleared = l.cleared_at ? ` · hold cleared ${localClock(new Date(l.cleared_at))}` : "";
      setRow("row-lightning", "good", `No lightning within 10 mi${stale}`, `Home Tempest · none forecast${cleared}`);
    }
    return;
  }
  if (l && l.status === "test") {
    setRow("row-lightning", "fair", "Lightning: home station in test mode", outlook ? `Forecast: ${outlookNote}` : "");
    return;
  }
  // No detection (Field Mode, or the home station unavailable): the NWS forecast only.
  const lost = l ? "Home Tempest unavailable · " : "";
  const fcStale = fc && fc.status === "stale" ? " (STALE)" : "";
  if (!fc || fc.status === "unavailable") {
    setRow("row-lightning", "bad", "Lightning forecast unavailable", lost + ((fc && fc.message) || "NWS not reached"));
  } else if (outlook) {
    setRow("row-lightning", "fair",
      `Lightning ${outlook.level} ${whenWords(outlook.start)}${fcStale}`, lost + "NWS forecast, not detected");
  } else {
    setRow("row-lightning", l || fcStale ? "fair" : "good", "No lightning forecast" + fcStale, lost + "NWS · next 12 hours");
  }
}

function renderAlertsRow(a) {
  if (!a || a.status === "unavailable") {
    // Never "no alerts" when alerts can't be checked (approved design).
    setRow("row-alerts", "bad", "NWS alerts UNAVAILABLE", "Not the same as no alerts");
    return;
  }
  if (a.status === "test") {
    setRow("row-alerts", "fair", "NWS: home station in test mode", "Alerts can't pass through");
    return;
  }
  const items = alertItems().filter(i => i.ref.block === "weather.alerts" && i.category !== "source_down");
  const hazards = items.filter(i => i.tier === "card");
  const notices = items.filter(i => i.tier === "badge");
  const stale = a.status === "stale" ? " (STALE)" : "";
  const noticeWords = notices.length === 1 ? `1 notice: ${notices[0].title}`
    : notices.length ? `${notices.length} notices` : "";
  if (hazards.length) {
    const top = hazards[0];
    // Short line: when it ends, how many more, any notices. The full hazard text is on the
    // Home screen's card and (later) the Alerts page.
    const parts = [top.until ? `Until ${localClock(new Date(top.until))}` : (top.detail || "")];
    if (hazards.length > 1) parts.push(`+${hazards.length - 1} more`);
    if (noticeWords) parts.push(noticeWords);
    setRow("row-alerts", top.color === "red" ? "bad" : "fair", top.title + stale, parts.filter(Boolean).join(" · "));
  } else {
    setRow("row-alerts", stale ? "fair" : "good", "No hazard alerts" + stale, noticeWords || "No NWS alerts");
  }
}

// ----------------------------------------------------------------- Now and Weather Radio

function renderNow() {
  const w = state.data.weather || {};
  const c = w.conditions;
  const tile = $("t-now");
  const home = (w.mode || "home") === "home";
  $("now-name").textContent = home ? "NOW · HOME TEMPEST" : "NOW · NEAREST STATION";
  setFresh($("now-fresh"), c);
  if (c && c.status === "ok") $("now-fresh").textContent = ageWords(liveAge(c));   // heading names the source
  const show = c && c.status !== "unavailable";
  tile.classList.toggle("is-stale", !!c && (c.status === "stale" || c.status === "test"));
  const extra = $("now-extra");
  extra.classList.toggle("unavailable", !show);
  document.querySelector(".now-main").hidden = !show;   // no empty "--": the reason gets the room
  if (!show) {
    // The word and the reason together on one line; the heading stays short enough to fit.
    $("now-fresh").textContent = "";
    $("now-temp").textContent = "--";
    $("now-wind").textContent = "";
    extra.textContent = `UNAVAILABLE · ${(c && c.message) || "no current conditions"}`;
    return;
  }
  $("now-temp").textContent = c.temperature_f === null ? "--" : `${Math.round(c.temperature_f)}°F`;
  $("now-wind").textContent = c.wind_mph === null ? "--"
    : `${dash(c.wind_dir)} ${Math.round(c.wind_mph)}${c.gust_mph !== null ? ", gust " + Math.round(c.gust_mph) : ""} mph`;
  const parts = [];
  if (home) {
    if (c.feels_like_f !== null) parts.push(`Feels ${Math.round(c.feels_like_f)}°`);
    if (c.humidity_pct !== null) parts.push(`${c.humidity_pct}%`);
  } else if (c.station) {
    parts.push(c.station.id || "--");
    if (c.station.distance_mi !== null && c.station.distance_mi !== undefined) parts.push(`${Math.round(c.station.distance_mi)} mi`);
  }
  if (c.pressure_inhg !== null) parts.push(`${c.pressure_inhg.toFixed(2)} inHg`);
  $("now-extra").textContent = parts.join(" · ");
}

function renderRadio() {
  const r = (state.data.weather || {}).weather_radio;
  const tile = $("t-radio");
  const pick = r && r.status !== "unavailable" && (r.nearest || []).find(s => s.status !== "OUT OF SERVICE");
  tile.classList.toggle("not-loaded", !pick);
  if (!pick) {
    // No stored list yet (first download pending or failed), or no working transmitter.
    $("nwr-freq").textContent = (r && r.fetched_at) ? "Station unknown" : "Station list not loaded yet";
    $("nwr-station").textContent = (r && r.message) || "No working transmitter found";
    $("nwr-same").textContent = "";
    return;
  }
  $("nwr-freq").textContent = `${pick.frequency_mhz.toFixed(3)} MHz`;
  $("nwr-station").textContent = [pick.callsign, pick.site, pick.distance_mi !== null ? `${Math.round(pick.distance_mi)} mi` : null]
    .filter(Boolean).join(" · ");
  // Only a transmitter NWS lists for the county carries its SAME-coded warnings:
  // say so when that isn't confirmed.
  const line = pick.covers_county === true ? (r.same_code ? `County SAME code ${r.same_code}` : "")
    : pick.covers_county === false ? ["Not listed for county", r.same_code && `SAME ${r.same_code}`].filter(Boolean).join(" · ")
    : "County not known yet";
  $("nwr-same").textContent = line;
  $("nwr-same").classList.toggle("unconfirmed", pick.covers_county !== true);
}

// ----------------------------------------------------------------- Forecast and hours

function renderPeriods() {
  const fc = (state.data.weather || {}).forecast;
  const box = $("periods");
  const periods = fc && fc.status !== "unavailable" ? (fc.periods || []).slice(0, 4) : [];
  box.classList.toggle("is-stale", !!fc && fc.status === "stale");
  if (!periods.length) {
    box.replaceChildren(el("div", "empty", (fc && fc.message) || "NWS forecast not available yet"));
    return;
  }
  box.replaceChildren(...periods.map(p => {
    const card = el("div", "period" + (isStorm(p.short) ? " storm" : ""));
    const temp = el("span", "temp num", p.temperature_f === null ? "--" : `${p.temperature_f}°`);
    temp.append(el("small", "", isNight(p.name) ? "low" : "high"));
    const row = el("div", "row");
    row.append(temp, el("span", "rain", p.rain_chance_pct === null ? "Rain --" : `Rain ${p.rain_chance_pct}%`));
    card.append(el("span", "pname", p.name || "--"), el("span", "short", p.short || "--"), row,
                el("span", "wind", p.wind ? `Wind ${p.wind}` : "Wind --"));
    return card;
  }));
}

function renderHours() {
  const fc = (state.data.weather || {}).forecast;
  const tile = $("t-hours");
  const hours = fc && fc.status !== "unavailable" ? (fc.hourly || []).slice(0, 12) : [];
  tile.classList.toggle("is-stale", !!fc && fc.status === "stale");
  const head = $("hours-fresh");
  if (fc && fc.status === "stale") setFresh(head, fc);
  else { head.className = "fresh"; head.textContent = fc && fc.issued_at ? `NWS hourly · forecast updated ${localClock(new Date(fc.issued_at))}` : "NWS hourly"; }

  const grid = $("hours"), msg = $("hours-msg");
  grid.hidden = !hours.length;
  msg.hidden = !!hours.length;
  if (!hours.length) { msg.textContent = (fc && fc.message) || "NWS hourly forecast not available yet"; return; }
  grid.style.gridTemplateColumns = `112px repeat(${hours.length}, minmax(0, 1fr))`;
  const cells = [];
  const row = (name, make) => { cells.push(el("span", "rowname", name)); for (const h of hours) cells.push(make(h)); };
  row("Hour", h => el("span", "cell hr", hourLabel(h.time)));
  row("Temp °F", h => el("span", "cell tmp", h.temperature_f === null ? "--" : `${h.temperature_f}°`));
  row("Lightning", h => el("span", "cell lt " + (h.lightning || ""),
    h.lightning === "likely" ? "LIKELY" : h.lightning === "possible" ? "POSSIBLE" : "–"));
  row("Rain", h => el("span", "cell", h.rain_chance_pct === null ? "--" : `${h.rain_chance_pct}%`));
  row("Gust mph", h => el("span", "cell gust" + (h.gust_mph !== null && h.gust_mph >= GUST_HIGH_MPH ? " high" : ""),
    h.gust_mph === null ? "--" : String(h.gust_mph)));
  grid.replaceChildren(...cells);
}

// ----------------------------------------------------------------- start

function renderAll() {
  renderHere();
  renderNow();
  renderRadio();
  renderPeriods();
  renderHours();
}

startScreen({
  render: renderAll,
  slow: () => { renderHere(); renderNow(); renderHours(); },
});
