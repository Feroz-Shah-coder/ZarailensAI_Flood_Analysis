// Flood event details: every district of one reported event, with real satellite measurements.
import { api, post, meta } from "../api.js";
import { esc, num, kpis, note, panel, table, risk, statusPill, progress, toast } from "../ui.js";
import { districtWindow } from "../pipeline.js";
import { renderResult, headline } from "../result_view.js";

export async function render(el, [eventId]) {
  const [e, m] = await Promise.all([api(`/api/catalog/${encodeURIComponent(eventId)}`), meta()]);
  const results = e.results || {};
  const options = e.analysis_districts;
  const choices = [...options, ...m.districts.filter((d) => !options.includes(d))];
  const ranked = Object.values(results).filter((r) => headline(r)).sort((a, b) => headline(b).flood_ha - headline(a).flood_ha);
  let current = ranked[0]?.district || options.find((d) => results[d]) || options[0];

  el.innerHTML = `<a class="btn" href="#/events">← Back to all flood events</a>
    <div style="margin-top:.8rem">${panel(`${e.year} · ${e.period} · ${e.event}`)}</div>
    <p>📍 <b>Reported area:</b> ${esc(e.areas)}<br>🛰 <b>Satellite data:</b> ${esc(e.era_label)} · searched ${esc(e.search_window[0])} → ${esc(e.search_window[1])}</p>
    ${e.note ? `<p class="small muted">${esc(e.note)}</p>` : ""}
    <p class="small muted">Reported event from the user-provided list. All numbers below are measured from real Earth Engine imagery for each district.</p>
    ${!e.districts.length ? note("info", `The report names no district, so this event is measured over the main flood-prone river districts: ${esc(options.join(", "))}. Any other district can be chosen below.`) : ""}
    <h3 style="margin-top:1rem">All districts of this event</h3><div class="overview"></div>
    <h3 style="margin-top:1.4rem">District details</h3>
    <div class="form"><label class="f">District<select class="dsel">${choices.map((d) => `<option ${d === current ? "selected" : ""} value="${esc(d)}">${esc(d)}${results[d] ? "  ✓ analysed" : ""}</option>`).join("")}</select></label>
      <button class="btn primary b-run"></button></div>
    <div class="prog" style="margin-top:.6rem"></div><div class="result" style="margin-top:.6rem"></div>`;

  const overview = () => {
    const rows = options.map((d) => { const r = results[d], h = headline(r);
      return { district: d, status: r ? r.status : "Not analysed yet", flood: h?.flood_ha, farm: h?.farmland_ha, risk: h?.risk, after: h?.after }; });
    const done = rows.filter((r) => r.flood !== undefined && r.flood !== null);
    const top = ["High", "Medium", "Low"].find((l) => done.some((r) => r.risk === l));
    const pics = options.filter((d) => results[d]?.thumb);
    el.querySelector(".overview").innerHTML = (done.length ? kpis([
      ["k-blue", "🌊", "Total underwater area", `${num(done.reduce((a, r) => a + r.flood, 0))} ha`, `${done.length} district(s) measured`],
      ["k-green", "🌱", "Total flooded farmland", `${num(done.reduce((a, r) => a + (r.farm || 0), 0))} ha`, "exposure, not loss"],
      ["k-red", "⚠️", "Highest crop damage risk", top || "—", "preliminary · NDVI-based"],
      ["k-violet", "🛰️", "Districts analysed", `${options.filter((d) => results[d]).length}/${options.length}`, "results saved"]]) : "") +
      table([{ key: "district", label: "District" }, { label: "Satellite result", fmt: (r) => statusPill(r.status) },
        { key: "flood", label: "Underwater (ha)", num: 0 }, { key: "farm", label: "Flooded farmland (ha)", num: 0 },
        { label: "Crop damage risk", fmt: (r) => risk(r.risk) }, { key: "after", label: "After image" }], rows,
        { onRowAttr: (r) => `class="click" data-d="${esc(r.district)}"` }) +
      (pics.length ? `<p class="small muted" style="margin-top:.8rem">Real satellite pictures after the event (blue = measured underwater area; grey = cloud or no data). Click a district to see its full details.</p>
        <div class="cards">${pics.map((d) => `<button class="pick ${d === current ? "on" : ""}" data-d="${esc(d)}"><div class="thumb"><img loading="lazy" src="${esc(results[d].thumb)}" alt="${esc(d)}"></div><b>🔍 ${esc(d)}</b></button>`).join("")}</div>` : "");
    el.querySelectorAll(".overview [data-d]").forEach((x) => x.addEventListener("click", () => pick(x.dataset.d, true)));
  };

  const show = () => {
    const r = results[current];
    el.querySelector(".b-run").textContent = r ? "⟳ Re-run analysis" : "▶ Analyse with real satellite data";
    const out = el.querySelector(".result");
    if (!r) { out.innerHTML = note("info", `${esc(current)} has not been analysed for this event yet. Sentinel-era analyses take about 2–8 minutes (keep this page open); Landsat-era analyses about 30 seconds. Results are saved.`); return; }
    renderResult(out, r, { report: { body: { event_id: e.id, district: current }, name: `ZarailensAI - ${e.id} - ${current}` } });
  };
  const pick = (d, scroll) => {
    current = d; el.querySelector(".dsel").value = d; overview(); show();
    if (scroll) el.querySelector(".dsel").scrollIntoView({ behavior: "smooth", block: "start" });
  };
  el.querySelector(".dsel").addEventListener("change", (ev) => pick(ev.target.value));
  el.querySelector(".b-run").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget, d = current; btn.disabled = true;
    const p = progress(el.querySelector(".prog"), `Analysing ${d} with real satellite data…`);
    try {
      let r;
      if (e.era === "landsat") { p.set(.2, "Searching Landsat scenes, measuring water change, farmland and NDVI"); r = await post(`/api/catalog/${encodeURIComponent(e.id)}/landsat`, { district: d }); }
      else {
        const raw = await districtWindow({ district: d, start: e.search_window[0], end: e.search_window[1], onProgress: (f, t) => p.set(f, t) });
        p.set(.97, "Saving the result and its picture");
        r = await post(`/api/catalog/${encodeURIComponent(e.id)}/save`, { ...raw, district: d });
      }
      results[d] = r; p.done(); toast(`${d}: ${r.status}`);
      el.querySelector(`.dsel option[value="${CSS.escape(d)}"]`).textContent = `${d}  ✓ analysed`;
      if (current === d) { overview(); show(); }
    } catch (err) { p.fail(err); } finally { btn.disabled = false; }
  });
  overview(); show();
}
