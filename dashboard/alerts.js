/* Ham Ops Console -- Alerts page (approved mockup "Alerts page", 2026-10-12, updated 2026-11-01).

   Three lists, all from GET /api/alerts:
     - ON THE PRIORITY CARD: the service's card items, worst first, with Acknowledge
       on critical ones and Details on each;
     - NOTIFICATIONS: the badge items (heat/cold, other warnings, other notices),
       "new" until this page has shown them once;
     - ENDED · LAST 24 HOURS: NWS alerts that left the list while it was current.
   Ranking, freshness and "ended" are decided in the service; this page only draws.
   Text is set with textContent only. */
"use strict";

const LIGHTNING_HOLDS = new Set(["lightning_5mi", "lightning_frequent", "lightning_10mi"]);
const SOURCE_NAMES = {
  "weather.alerts": "NWS", "weather.lightning": "Weather Sentinel", "weather.conditions": "Weather Sentinel",
  "weather.forecast": "NWS forecast", "propagation": "PropMon",
};

const page = {
  openId: null,          // the item shown in Details, if any
  clearArmed: 0,         // Clear hold: first tap arms (time), second confirms within 10 s
  newIds: new Set(),     // badge items not yet seen on this page
  firstRender: true,
};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

/* Label words and style for an item: a word always goes with the colour. */
function tagFor(item) {
  const c = item.category;
  if (item.critical) return {text: "CRITICAL", outline: false};
  if (c === "hazard_alert") return {text: (item.kind || "alert").toUpperCase(), outline: false};
  if (c === "alerts_unavailable_storms") return {text: "NWS DOWN", outline: false};
  if (c === "lightning_frequent" || c === "lightning_10mi") return {text: "LOCAL", outline: true};
  if (c === "lightning_likely_soon" || c === "lightning_outlook") return {text: "FORECAST", outline: true};
  if (c === "source_down") return {text: /test mode/i.test(item.title || "") ? "TEST" : "SOURCE", outline: true};
  if (c === "propagation") return {text: "BANDS", outline: true};
  if (c === "all_quiet") return {text: "QUIET", outline: true};
  if (c === "temperature_alert") return {text: /heat/i.test(item.title || "") ? "HEAT" : "COLD", outline: false};
  if (c === "other_warning") return {text: "WARNING", outline: false};
  if (c === "other_notice") return {text: (item.kind || "notice").toUpperCase(), outline: false};
  return {text: "ALERT", outline: false};
}
function makeTag(item) {
  const t = tagFor(item);
  return el("span", `tag ${item.color}${t.outline ? " outline" : ""}`, t.text);
}

/* An NWS product (warning, watch, advisory, statement), not a card about the NWS feed itself. */
const NWS_PRODUCTS = new Set(["hazard_warning", "hazard_alert", "temperature_alert", "other_warning", "other_notice"]);
function isNws(item) { return NWS_PRODUCTS.has(item.category); }
function nwsCurrent() { return (state.data.alerts || {}).nws_alerts_status === "ok"; }
function sourceName(item) { return SOURCE_NAMES[item.ref && item.ref.block] || ""; }

/* "Tuesday" only when it isn't today, so a time a day away can't be misread. */
function shortTime(iso) {
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  return (today ? "" : d.toLocaleDateString([], {weekday: "short"}) + " ") + localClock(d);
}

function titleFor(item) {
  if (isNws(item) && item.until) return `${item.title} until ${shortTime(item.until)}`;
  return item.title || "--";
}

function subFor(item) {
  const parts = [];
  if (isNws(item)) {
    parts.push("NWS");
    if (item.since) {
      parts.push(`${new Date(item.since) > new Date() ? "starts" : "from"} ${shortTime(item.since)}`);
    }
    if (item.critical) parts.push(item.acknowledged ? "acknowledged" : "not acknowledged");
    if (!nwsCurrent()) parts.push("NWS list out of date");
  } else {
    // Skip the source name when the detail already starts with it ("NWS forecast, not detected.").
    const src = sourceName(item);
    if (src && !(item.detail || "").startsWith(src)) parts.push(src);
    if (item.detail) parts.push(item.detail);
    if (item.critical && item.acknowledged) parts.push("acknowledged");
  }
  if (page.newIds.has(item.ref.id)) parts.push("new");
  return parts.join(" · ");
}

function hasDetails(item) {
  return item.category !== "all_quiet" || !!item.detail;
}

function row(item) {
  const r = el("div", `row ${item.color}${item.tier === "badge" ? " badge-row" : ""}`);
  const text = el("div", "text");
  text.append(el("span", "title", titleFor(item)), el("span", "sub", subFor(item)));
  r.append(makeTag(item), text);
  if (item.critical && !item.acknowledged) {
    const ack = el("button", "act", "Acknowledge");
    ack.addEventListener("click", () => acknowledge(item.ref.id));
    r.append(ack);
  }
  if (hasDetails(item)) {
    const more = el("button", "more", "Details ›");
    more.addEventListener("click", () => openDetails(item.ref.id));
    r.append(more);
  }
  return r;
}

function renderLists() {
  const items = alertItems();
  const badgeIds = items.filter(i => i.tier === "badge").map(i => i.ref.id);

  // "new": badge items this page hasn't shown before (kept for this visit).
  const seen = recall("hoc.seenBadge", []);
  for (const id of badgeIds) if (!seen.includes(id)) page.newIds.add(id);
  remember("hoc.seenBadge", seen.concat(badgeIds.filter(id => !seen.includes(id))).slice(-100));

  const cards = items.filter(i => i.tier === "card");
  $("card-list").replaceChildren(...cards.map(row));

  const badges = items.filter(i => i.tier === "badge");
  $("badge-list").replaceChildren(...(badges.length ? badges.map(row) : [el("span", "al-none", "None.")]));

  const a = state.data.alerts || {};
  const ended = a.ended || [];
  const current = nwsCurrent();
  let endedRows;
  if (ended.length) {
    endedRows = ended.map(e => {
      const r = el("div", "ended-row");
      r.append(el("span", "", e.title || "--"), el("span", "", `NWS · ended ${shortTime(e.ended_at)}`));
      return r;
    });
  } else if (current) {
    endedRows = [el("span", "al-none", "None in the last 24 hours.")];
  } else {
    // Never imply "nothing ended" when the NWS list can't be checked.
    endedRows = [el("span", "al-none warn", "Can't tell while NWS alerts are out of date.")];
  }
  if (ended.length && !current) {
    endedRows.push(el("span", "al-none warn", "NWS alerts out of date: more may have ended."));
  }
  $("ended-list").replaceChildren(...endedRows);
}

// ----------------------------------------------------------------- Details

function openDetails(id) {
  page.openId = id;
  page.clearArmed = 0;
  renderDetails();
}
function closeDetails() {
  page.openId = null;
  page.clearArmed = 0;
  $("details").hidden = true;
}

function renderDetails() {
  if (!page.openId) return;
  const box = $("details");
  const item = alertItems().find(i => i.ref.id === page.openId);
  box.hidden = false;
  const panel = $("d-panel");
  if (!item) {
    // Ended or replaced since it was opened.
    panel.className = "al-panel";
    $("d-tag").replaceWith(Object.assign(el("span", "tag outline green", "ENDED"), {id: "d-tag"}));
    $("d-source").textContent = "";
    $("d-title").textContent = "This item is no longer in the list.";
    $("d-detail").textContent = "It has ended, or the console has replaced it with a newer one.";
    $("d-instruction").hidden = true;
    $("d-ack").hidden = true;
    $("d-clear").hidden = true;
    return;
  }
  panel.className = `al-panel ${item.color}`;
  $("d-tag").replaceWith(Object.assign(makeTag(item), {id: "d-tag"}));
  const src = [sourceName(item)];
  if (isNws(item)) {
    if (item.since) src.push(`from ${clockPair(item.since)}`);
    if (item.until) src.push(`until ${clockPair(item.until)}`);
  }
  if (item.critical) src.push(item.acknowledged ? "acknowledged" : "not acknowledged");
  $("d-source").textContent = src.filter(Boolean).join(" · ");
  $("d-title").textContent = item.title || "--";
  $("d-detail").textContent = item.detail || "";
  const instr = $("d-instruction");
  instr.hidden = !(isNws(item) && item.instruction);
  instr.textContent = (isNws(item) && item.instruction) || "";
  $("d-ack").hidden = !(item.critical && !item.acknowledged);
  const clear = $("d-clear");
  clear.hidden = !LIGHTNING_HOLDS.has(item.category);
  if (Date.now() - page.clearArmed > 10000) page.clearArmed = 0;
  clear.textContent = page.clearArmed ? "Confirm clear" : "Clear hold";
  clear.classList.toggle("armed", !!page.clearArmed);
}

async function acknowledge(id) {
  try {
    await fetch("/api/alerts/ack", {method: "POST", headers: {"Content-Type": "application/json"},
                                    body: JSON.stringify({id})});
  } catch (e) { /* the next poll shows the real state */ }
  poll();
}

$("d-ack").addEventListener("click", () => { if (page.openId) acknowledge(page.openId); });
// Clear hold: two taps (approved 2026-11-05), so a stray touch can't end a lightning hold.
$("d-clear").addEventListener("click", async () => {
  if (!page.clearArmed) { page.clearArmed = Date.now(); renderDetails(); return; }
  page.clearArmed = 0;
  try { await fetch("/api/alerts/clear-hold", {method: "POST"}); } catch (e) { /* next poll */ }
  poll();
});
$("d-close").addEventListener("click", closeDetails);
$("details").addEventListener("click", e => { if (e.target === $("details")) closeDetails(); });

startScreen({
  render() { renderLists(); renderDetails(); },
  tick() { if (page.clearArmed && Date.now() - page.clearArmed > 10000) renderDetails(); },
  slow() { renderLists(); },
});
