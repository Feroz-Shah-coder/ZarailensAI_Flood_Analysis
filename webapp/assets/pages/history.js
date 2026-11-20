// Analysis History: saved analyses, reported-event results and dashboard monitoring runs.
import { api } from "../api.js";
import { esc, num, note, panel, table, tabs, fmtTime, barChart, statusPill } from "../ui.js";

const OUTCOME = { event: "Recent event detected", historical_event: "No recent event · historical event shown", no_event: "No confirmed event", no_data: "No data", error: "Failed" };

export async function render(el) {
  el.innerHTML = `${panel("Analysis History")}<div class="h-tabs"></div>`;
  const h = await api("/api/history");
  tabs(el.querySelector(".h-tabs"), ["Saved analyses", "Impact analytics", "Reported events analysed", "Dashboard monitoring runs"], (i, body) => {
    if (i === 0) {
      body.innerHTML = h.analyses.length ? table([{ key: "id", label: "ID" }, { label: "Created (UTC)", fmt: (r) => fmtTime(r.created_at) }, { key: "location", label: "Location" },
        { key: "acquired_before", label: "Before acquisition" }, { key: "acquired_after", label: "After acquisition" }, { key: "sensor", label: "Sensor" },
        { key: "flood_ha", label: "Flood area (ha)", num: 0 }, { key: "agricultural_exposure_ha", label: "Cropland exposed (ha)", num: 0 }], h.analyses,
        { onRowAttr: (r) => `class="click" data-id="${r.id}"` }) + `<pre class="h-json small" style="white-space:pre-wrap;background:#fff;border:1px solid #d5e2da;border-radius:10px;padding:.8rem;max-height:420px;overflow:auto" hidden></pre>`
        : note("info", "No saved analyses yet. Run one on the New Analysis page and press “Save to Analysis History”.");
      body.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => {
        const r = h.analyses.find((x) => String(x.id) === tr.dataset.id), pre = body.querySelector(".h-json");
        pre.hidden = false; pre.textContent = JSON.stringify(r.summary, null, 2); pre.scrollIntoView({ behavior: "smooth" });
      }));
    } else if (i === 1) {
      const rows = [...h.analyses.map((a) => ({ loc: a.location || "Unknown", flood: a.flood_ha || 0, ag: a.agricultural_exposure_ha || 0 })),
                    ...h.catalog.filter((c) => c.headline).map((c) => ({ loc: `${c.district} district, Khyber Pakhtunkhwa`, flood: c.headline.flood_ha, ag: c.headline.farmland_ha }))];
      if (!rows.length) { body.innerHTML = note("info", "Impact charts appear after real analyses are saved. No sample statistics are shown."); return; }
      const by = {}; rows.forEach((r) => { const k = r.loc.split(" district")[0]; by[k] = by[k] || { flood: 0, ag: 0 }; by[k].flood += r.flood; by[k].ag += r.ag; });
      const keys = Object.keys(by).sort((a, b) => by[b].flood - by[a].flood);
      body.innerHTML = `<div class="grid g2"><div class="card"><div class="chart c1"></div></div><div class="card"><div class="chart c2"></div></div></div>
        <p class="small muted">Sums over all saved analyses and reported-event results. Repeated districts or periods overlap; totals are not unique geographic area.</p>`;
      barChart(body.querySelector(".c1"), keys, keys.map((k) => by[k].flood), { title: "Measured flood area by district (ha, summed)" });
      barChart(body.querySelector(".c2"), keys, keys.map((k) => by[k].ag), { color: "#1f8a4c", title: "Flooded farmland by district (ha, summed)" });
    } else if (i === 2) {
      const rows = h.catalog.sort((a, b) => String(b.event_id).localeCompare(String(a.event_id)));
      body.innerHTML = rows.length ? table([{ label: "Event", fmt: (r) => `<a href="#/events/${encodeURIComponent(r.event_id)}">${esc(r.event_id)}</a>` }, { key: "district", label: "District" },
        { label: "Satellite result", fmt: (r) => statusPill(r.status) }, { label: "Underwater (ha)", fmt: (r) => num(r.headline?.flood_ha), num: 0 },
        { label: "Flooded farmland (ha)", fmt: (r) => num(r.headline?.farmland_ha), num: 0 }, { label: "Risk", fmt: (r) => esc(r.headline?.risk || "–") },
        { label: "Computed (UTC)", fmt: (r) => fmtTime(r.computed_at) }], rows, { scroll: true }) : note("info", "No reported event has been analysed yet.");
    } else {
      body.innerHTML = h.runs.length ? table([{ key: "id", label: "Run" }, { label: "Refreshed (UTC)", fmt: (r) => fmtTime(r.refreshed_at) },
        { label: "Monitoring window", fmt: (r) => `${esc(r.window_start || "–")} → ${esc(r.window_end || "–")}` },
        { label: "Latest Sentinel-1 acquisition", fmt: (r) => fmtTime(r.latest_acquisition) }, { label: "Outcome", fmt: (r) => esc(OUTCOME[r.status] || r.status) }], h.runs)
        : note("info", "No dashboard monitoring runs yet. Open the Dashboard and press Refresh.");
    }
  });
}
