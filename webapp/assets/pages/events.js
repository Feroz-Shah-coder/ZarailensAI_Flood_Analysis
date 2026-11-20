// Flood Events 2010–2026: reported KPK floods as picture cards in tabs; a card opens the full satellite details.
import { api } from "../api.js";
import { esc, num, note, panel, tabs } from "../ui.js";

const GROUPS = [
  ["2010–2014 (Landsat era)", (e) => e.year <= 2014],
  ["2015–2019", (e) => e.year >= 2015 && e.year <= 2019],
  ["2020–2023", (e) => e.year >= 2020 && e.year <= 2023],
  ["2024–2026", (e) => e.year >= 2024],
];
let lastTab = 0;

export async function render(el) {
  el.innerHTML = `${panel("Reported flood events in Khyber Pakhtunkhwa, 2010–2026")}<div class="ev-intro small muted"></div><div class="ev-tabs"></div>`;
  const data = await api("/api/catalog");
  el.querySelector(".ev-intro").textContent = `${data.source || ""} Open an event for real satellite measurements: underwater area, farmland, crop damage risk, before/after images and the acquisition timeline.`;
  tabs(el.querySelector(".ev-tabs"), GROUPS.map((g) => g[0]), (i, body) => {
    lastTab = i;
    const events = data.events.filter(GROUPS[i][1]);
    body.innerHTML = `<div class="cards">${events.map(card).join("")}</div>` + (i === 0 ? `<p class="small muted" style="margin-top:1rem">Sentinel-1 radar starts in October 2014 and Sentinel-2 in mid-2015, so these events use the Landsat 5/7/8 optical archive (16-day revisit; clouds often hide monsoon floods; Landsat 7 has striped gaps after 2003).</p>` : "");
  }, lastTab);
}

function card(e) {
  const done = e.analyses || [];
  const measured = done.filter((r) => r.headline).sort((a, b) => b.headline.flood_ha - a.headline.flood_ha);
  const best = measured[0] || done.find((r) => r.thumb) || null;
  const badges = [`<span class="pill p-hist">${e.era === "sentinel" ? "Sentinel-1/2" : "Landsat"}</span>`];
  if (/forecast|not established/i.test(e.event)) badges.push(`<span class="pill p-possible">Not confirmed in report</span>`);
  if (e.event.includes("GLOF")) badges.push(`<span class="pill p-violet">GLOF</span>`);
  badges.push(done.length ? `<span class="pill p-none">${done.length}/${e.analysis_districts.length} districts analysed</span>` : `<span class="pill p-grey">Not analysed yet</span>`);
  const sum = (k) => measured.reduce((a, r) => a + (r.headline[k] || 0), 0);
  const href = `#/events/${encodeURIComponent(e.id)}`;
  return `<article class="card ecard">
    <a class="thumb" href="${href}" aria-label="Open ${esc(e.year)} ${esc(e.event)}">${best?.thumb ? `<img loading="lazy" src="${esc(best.thumb)}" alt="Satellite picture of ${esc(best.district)} after the event">` : "🛰️<br>Satellite picture appears after analysis"}</a>
    ${best?.thumb ? `<div class="tiny muted">🛰 ${esc(best.district)} · ${best.headline ? esc(best.headline.after) + " · blue = underwater area" : "satellite view, no flood signal"}</div>` : ""}
    <h4>${esc(e.year)} · ${esc(e.event)}</h4>
    <div class="small"><span class="muted">${esc(e.period)}</span><br>📍 ${esc(e.areas)}</div>
    <div>${badges.join("")}</div>
    ${measured.length ? `<div class="small"><b>${num(sum("flood_ha"))} ha</b> underwater · <b>${num(sum("farmland_ha"))} ha</b> farmland flooded<br>
      <span class="muted">sum over ${measured.length} district(s) with a measured flood</span></div>
      ${measured.slice(0, 3).map((r) => `<div class="tiny muted"><b>${esc(r.district)}</b>: ${num(r.headline.flood_ha)} ha underwater · ${num(r.headline.farmland_ha)} ha farmland · risk ${esc(r.headline.risk || "—")}</div>`).join("")}`
      : done.length ? `<div class="tiny muted">Satellite result: ${esc([...new Set(done.map((r) => r.status))].join(", "))}</div>` : ""}
    <a class="btn primary block" href="${href}">↗ Open details</a>
  </article>`;
}
