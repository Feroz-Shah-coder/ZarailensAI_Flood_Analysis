// Shared rendering of one district result (reported event or New Analysis).
import { post, download } from "./api.js";
import { esc, num, ha, signed, kpis, note, table, risk, makeMap, legend, barChart, toast, statusPill, fullMap } from "./ui.js";

export function headline(r) {
  if (!r) return null;
  if (r.landsat) {
    const l = r.landsat, rows = l.crop_rows || [];
    return { flood_ha: l.flood_ha, farmland_ha: l.agricultural_flood_ha, risk: rows[0]?.risk, after: l.after_date, total: l.total_cropland_ha };
  }
  if (r.detail) {
    const d = r.detail, rows = (d.crops || {}).rows || [];
    return { flood_ha: d.flood_ha, farmland_ha: d.agricultural_flood_ha, risk: overallRisk(rows), after: d.after?.date, total: d.total_cropland_ha };
  }
  return null;
}
export function overallRisk(rows) {
  const exposed = rows.filter((r) => (r.affected_ha || 0) >= 1);
  for (const lvl of ["High", "Medium", "Low"]) if (exposed.some((r) => r.risk === lvl)) return lvl;
  return exposed.length ? "Insufficient Data" : "Low";
}

const cropRowsOf = (r) => (r.landsat?.crop_rows) || (r.detail?.crops?.rows) || [];

export function renderResult(el, r, { windowLabel, report } = {}) {
  const h = headline(r), detail = r.detail, landsat = r.landsat, crops = cropRowsOf(r);
  const pct = h && h.total ? `${(100 * h.farmland_ha / h.total).toFixed(1)}% of district cropland` : "exposure, not loss";
  el.innerHTML = `
    ${kpis([
      ["k-blue", "🌊", "Underwater area (new water)", h ? ha(h.flood_ha) : "—", h ? `image ${h.after}` : "not measured"],
      ["k-green", "🌱", "Flooded farmland", h ? ha(h.farmland_ha) : "—", pct],
      ["k-red", "⚠️", "Crop damage risk", h?.risk || "—", "preliminary · NDVI-based"],
      ["k-violet", "🛰️", "Satellite result", r.status || "—", `${r.acquisitions_in_window ?? "–"} acquisitions in window`],
      ["k-yellow", "🗓️", "Searched window", (r.window || [])[0] || "—", `to ${(r.window || [])[1] || "–"}`],
    ])}
    ${r.message ? note("info", esc(r.message)) : ""}
    ${r.event ? `<details ${String(r.status).startsWith("Rejected") ? "open" : ""}><summary>Evidence checked</summary>
       <ul>${(r.event.reasons || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
    ${r.detail_error ? note("err", `Detailed analysis failed: ${esc(r.detail_error)}`) : ""}
    <div class="pics"></div>
    <div class="crops"></div>
    <h4>Time data</h4>
    <div class="grid g2"><div class="card"><div class="chart c-tl"></div><div class="small muted tl-note"></div></div>
      <div class="card"><div class="chart c-rain"></div><div class="small muted rain-note"></div></div></div>
    <div class="tehsils"></div>
    <div class="row" style="margin-top:1rem">
      <button class="btn b-pdf">⬇ Download PDF report</button><button class="btn b-xls">⬇ Export Excel</button></div>`;

  // pictures: fresh Earth Engine tiles (links expire after a few hours)
  const pics = el.querySelector(".pics");
  if (detail || landsat) {
    pics.innerHTML = `<div class="card"><span class="spin"></span> Loading live satellite images from Earth Engine…</div>`;
    post("/api/catalog/layers", { result: r }).then((L) => drawPictures(pics, r, L)).catch((e) => { pics.innerHTML = note("warn", `Live satellite images could not be loaded: ${esc(e.message)}`); });
  }

  if (crops.length) {
    el.querySelector(".crops").innerHTML = `<h4>Crop damage assessment</h4>` + table([
      { key: "crop", label: "Crop" }, { key: "affected_ha", label: "Flooded area (ha)", num: 0 },
      { key: "crop_area_ha", label: "Crop area in district (ha)", num: 0 },
      { label: "NDVI change – flooded", fmt: (x) => signed(x.ndvi_change_flooded), num: 3 },
      { label: "NDVI change – unflooded", fmt: (x) => signed(x.ndvi_change_unflooded), num: 3 },
      { label: "Relative NDVI change", fmt: (x) => signed(x.relative_ndvi_change), num: 3 },
      { label: "Damage risk", fmt: (x) => risk(x.risk) }], crops) +
      `<p class="small muted">Flooded farmland = exposure. NDVI change = observed vegetation change (flooded minus unflooded fields).
       Damage risk = preliminary class. Confirmed crop damage requires field verification; no loss percentage is estimated.</p>`;
  }

  const tl = (r.timeline || []).filter((t) => !t.phase || t.phase === "monitoring");
  if (tl.length) {
    barChart(el.querySelector(".c-tl"), tl.map((t) => t.date), tl.map((t) => t.new_water_ha), { title: "New water on each satellite acquisition (ha)", yTitle: "ha", hover: tl.map((t) => t.track) });
    el.querySelector(".tl-note").textContent = `${tl.length} acquisition(s) in the window. Gaps between bars are days with no satellite pass.`;
  } else el.querySelector(".c-tl").outerHTML = note("info", "No usable acquisition in this window.");
  const rain = (detail || {}).rainfall || (landsat || {}).rainfall || {};
  const series = (rain.series || []).filter((s) => s.mm !== null && s.mm !== undefined);
  if (series.length) {
    const marks = detail ? [{ x: detail.before.date, label: "Before image" }, { x: detail.after.date, label: "After image" }] : [];
    barChart(el.querySelector(".c-rain"), series.map((s) => s.date), series.map((s) => s.mm), { color: "#1f6b45", title: "Daily rainfall, district mean (mm)", marks });
    el.querySelector(".rain-note").textContent = `Source: ${rain.source || "–"}`;
  } else el.querySelector(".c-rain").outerHTML = note("info", "Rainfall series not available for this result.");

  if (detail?.tehsil_priorities?.length) {
    el.querySelector(".tehsils").innerHTML = `<h4 style="margin-top:1rem">Field inspection priority by tehsil</h4>` + table([
      { label: "Tehsil", fmt: (t) => esc(String(t.area).replace(/\b\w/g, (c) => c.toUpperCase())) }, { label: "Priority", fmt: (t) => risk(t.priority) },
      { key: "flood_ha", label: "Underwater (ha)", num: 0 }, { key: "flooded_cropland_ha", label: "Flooded farmland (ha)", num: 0 },
      { key: "high_risk_ha", label: "High-risk farmland (ha)", num: 0 }], detail.tehsil_priorities);
  }

  const base = (report?.name) || `ZarailensAI - ${r.district}`;
  const go = (fmt) => async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    try { await download(`/api/report/${fmt}`, report?.body || { result: r, title: windowLabel }, `${base}.${fmt}`); }
    catch (e) { toast(`Report failed: ${e.message}`, true); } finally { b.disabled = false; }
  };
  el.querySelector(".b-pdf").addEventListener("click", go("pdf"));
  el.querySelector(".b-xls").addEventListener("click", go("xlsx"));
}

function drawPictures(el, r, L) {
  const g = r.geometry, d = r.detail, l = r.landsat;
  const cell = (title, cap) => `<div class="card"><b>${esc(title)}</b><div class="map short"></div><div class="small muted">${cap}</div></div>`;
  if (d) {
    const t = (s) => esc(String(s.time_utc || s.date).slice(0, 16).replace("T", " ")) + " UTC";
    el.innerHTML = `<h4>Satellite pictures – before and after (actual acquisitions)</h4>
      <div class="grid g3">${cell("Before flood · Sentinel-1", t(d.before))}${cell("After flood · Sentinel-1", t(d.after))}${cell("Underwater area detected", "green = new water")}</div>
      <div class="grid g3" style="margin-top:1rem">${cell("Before · Sentinel-2 natural colour", L.s2_before ? "dates: " + esc(L.s2_before.dates.join(", ")) : "No Sentinel-2 image in this window")}
        ${cell("After · Sentinel-2 natural colour", L.s2_after ? "dates: " + esc(L.s2_after.dates.join(", ")) : "No Sentinel-2 image in this window")}
        ${cell("Flood & farmland map", "")}</div>
      <div class="row" style="margin-top:.6rem"><button class="btn b-full">🗺 View full map</button></div>`;
    const m = el.querySelectorAll(".map");
    makeMap(m[0], g, [{ url: L.before, name: "Before" }]);
    makeMap(m[1], g, [{ url: L.after, name: "After" }]);
    makeMap(m[2], g, [{ url: L.after, name: "After" }, { url: L.flood, name: "New water", opacity: .9 }]);
    if (L.s2_before) makeMap(m[3], g, [{ url: L.s2_before.url, name: "Sentinel-2 before" }]); else m[3].outerHTML = note("info", "No cloud-free Sentinel-2 image.");
    if (L.s2_after) makeMap(m[4], g, [{ url: L.s2_after.url, name: "Sentinel-2 after" }]); else m[4].outerHTML = note("info", "No cloud-free Sentinel-2 image.");
    const impact = [{ url: L.flood_blue, name: "Underwater area", opacity: .85 }, { url: L.cropland, name: "Farmland", opacity: .5 },
                    { url: L.flooded_cropland, name: "Flooded farmland", opacity: .95 }, { url: L.risk, name: "Damage risk", opacity: .95 }];
    makeMap(m[5], g, impact, { control: true });
    m[5].insertAdjacentHTML("afterend", legend([["Underwater area", "#2f7de1"], ["Farmland", "#f2c12e"], ["Flooded farmland", "#f26b1d"], ["Damage risk (layer menu)", "#d7301f"]]));
    el.querySelector(".b-full").addEventListener("click", () => fullMap(g, impact, `${r.district} – flood & crop impact`));
  } else if (l) {
    el.innerHTML = `<h4>Satellite pictures – ${esc(l.sensor)} (actual acquisitions)</h4>
      <div class="grid g3">${cell("Before flood", "composite of " + esc((l.before_dates || []).slice(-4).join(", ")))}
        ${cell("After flood", `${esc(l.after_date)} · ${num(l.after_clear_percent)}% of district clear`)}
        ${cell("Underwater area & farmland", "blue = new water · orange = flooded farmland")}</div>
      <p class="small muted">${esc(l.rule || "")} · ${esc(l.cropland_source || "")}</p>`;
    const m = el.querySelectorAll(".map");
    makeMap(m[0], g, [{ url: L.before, name: "Before" }]);
    makeMap(m[1], g, [{ url: L.after, name: "After" }]);
    makeMap(m[2], g, [{ url: L.after, name: "After" }, { url: L.flood, name: "Underwater", opacity: .85 }, { url: L.flooded_cropland, name: "Flooded farmland", opacity: .95 }], { control: true });
  }
}

export { statusPill };
