// Reports: PDF / Excel for the dashboard event and for any analysed reported event.
import { api, download } from "../api.js";
import { esc, num, note, panel, toast } from "../ui.js";
import { state } from "../state.js";

export async function render(el) {
  const [dash, cat] = await Promise.all([api("/api/dashboard"), api("/api/catalog")]);
  const run = dash.run;
  const analysed = cat.events.flatMap((e) => (e.analyses || []).map((a) => ({ e, a })));
  el.innerHTML = `${panel("Reports")}
    <div class="grid g2">
      <div class="card"><h4>Latest dashboard event</h4>
        ${run ? `<p class="small">${run.selected ? `<b>${esc(run.selected.district)}</b> · ${esc(run.selected.status)} · peak ${esc(run.selected.peak_date)}` : "No confirmed event in the stored run"}<br>
          Refreshed ${esc(String(run.refreshed_at).slice(0, 16).replace("T", " "))} UTC · window ${esc((run.monitoring_window || []).join(" → "))}
          ${run.detail ? `<br>Flooded ${num(run.detail.flood_ha)} ha · farmland ${num(run.detail.agricultural_flood_ha)} ha` : ""}</p>
          <div class="row"><button class="btn primary r-pdf">⬇ PDF</button><button class="btn r-xls">📊 Excel</button></div>
          ${state.event ? `<p class="small muted">A user-selected event is open on the Dashboard; use its export buttons for that event.</p>` : ""}`
          : note("info", "No monitoring run stored yet. Open the Dashboard and press Refresh.")}</div>
      <div class="card"><h4>Reported flood event (2010–2026)</h4>
        ${analysed.length ? `<label class="f">Event and district<select class="c-sel">${analysed.map(({ e, a }, i) => `<option value="${i}">${esc(e.year)} · ${esc(e.event)} · ${esc(a.district)} · ${esc(a.status)}</option>`).join("")}</select></label>
          <div class="row" style="margin-top:.6rem"><button class="btn primary c-pdf">⬇ PDF</button><button class="btn c-xls">📊 Excel</button></div>`
          : note("info", "No reported event analysed yet.")}</div>
    </div>
    <p class="small muted" style="margin-top:1rem">Reports contain only values computed from Earth Engine data, their sources and the method caveats. New Analysis results can be exported from that page.</p>`;
  const go = (body, name) => async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    try { await download(`/api/report/${name.split(".").pop()}`, body(), name); } catch (e) { toast(`Report failed: ${e.message}`, true); } finally { b.disabled = false; }
  };
  if (run) {
    el.querySelector(".r-pdf").addEventListener("click", go(() => ({ run_id: run.run_id }), "ZarailensAI - KPK event.pdf"));
    el.querySelector(".r-xls").addEventListener("click", go(() => ({ run_id: run.run_id }), "ZarailensAI - KPK event.xlsx"));
  }
  if (analysed.length) {
    const pick = () => analysed[+el.querySelector(".c-sel").value];
    const body = () => ({ event_id: pick().e.id, district: pick().a.district });
    el.querySelector(".c-pdf").addEventListener("click", (ev) => go(body, `ZarailensAI - ${pick().e.id} - ${pick().a.district}.pdf`)(ev));
    el.querySelector(".c-xls").addEventListener("click", (ev) => go(body, `ZarailensAI - ${pick().e.id} - ${pick().a.district}.xlsx`)(ev));
  }
}
