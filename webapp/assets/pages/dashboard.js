// Dashboard: latest KPK flood event from the stored monitoring run, refreshed on demand with real Sentinel-1 data.
import { api, post, download, districtsGeo, meta } from "../api.js";
import { esc, num, ha, signed, kpis, note, panel, table, risk, statusPill, makeMap, legend, barChart, donut, toast, fmtTime,
         hoursSince, shortCrop, progress, RISK_COLORS, ESRI_GRAY, fullMap } from "../ui.js";
import { dashboardRefresh, discover } from "../pipeline.js";
import { overallRisk } from "../result_view.js";
import { state } from "../state.js";

const ROD_KOHI = ["Tank", "D. I. Khan", "Lakki Marwat", "Bannu", "South Waziristan"];

export async function render(el) {
  el.innerHTML = `<div class="card"><div class="row"><h3 style="margin:0">Latest flood event · Khyber Pakhtunkhwa</h3><span class="spacer"></span>
      <button class="btn primary b-refresh">⟳ Refresh Latest Flood Data</button>
      <a class="btn" href="#/analysis">🔍 Analyze Selected Area</a></div>
      <div class="d-prog"></div><div class="d-status" style="margin-top:.6rem"></div></div>
    <div class="d-body" style="margin-top:1rem"></div>`;
  const status = el.querySelector(".d-status"), body = el.querySelector(".d-body"), prog = el.querySelector(".d-prog");
  const m = await meta();
  let run = state.run;
  if (!run) {
    try { run = (await api("/api/dashboard")).run; state.run = run; }
    catch (e) { status.innerHTML = note("err", `Stored results could not be loaded: ${esc(e.message)}`); }
  }
  let busy = false;
  const refresh = async () => {
    if (busy) return; busy = true;
    const btn = el.querySelector(".b-refresh"); btn.disabled = true;
    const p = progress(prog, "Searching KPK for flood events in real Sentinel-1 data… (keep this page open, 3–15 min)");
    try {
      const fresh = await dashboardRefresh((f, t) => p.set(f, t));
      if (fresh.status === "error") throw new Error(fresh.message);
      try { fresh.run_id = (await post("/api/runs", { run: fresh })).run_id; } catch (e) { toast(`Result shown but not saved: ${e.message}`, true); }
      state.run = fresh; state.event = null; state.detail = null; p.done(); toast("Dashboard refreshed with the latest Sentinel-1 data.");
      draw(fresh);
    } catch (e) { p.fail(e); } finally { busy = false; btn.disabled = false; }
  };
  el.querySelector(".b-refresh").addEventListener("click", refresh);

  const draw = (run) => drawRun(status, body, run, m, refresh);
  if (run) draw(run);
  else status.innerHTML = note("warn", "No monitoring result is stored yet. Press <b>Refresh Latest Flood Data</b> to run the first KPK-wide search.");
  if (run && hoursSince(run.refreshed_at) > m.params.auto_refresh_hours)
    status.insertAdjacentHTML("afterbegin", note("info", `These results are ${Math.round(hoursSince(run.refreshed_at))} h old (refresh interval ${m.params.auto_refresh_hours} h). Press <b>Refresh Latest Flood Data</b> to search the newest Sentinel-1 acquisitions.`));
}

function drawRun(status, body, run, m, refresh) {
  const event = state.event || run.selected;
  const detail = state.event ? state.detail : run.detail;
  const recent = run.recent || {}, historical = run.historical || [];
  const source = state.event ? (state.source || "user-selected event") : run.selected_source;
  // ---------------------------------------------------------- status
  let s = "";
  if (event) {
    const v = event.verdict || {};
    s += `<div class="card" style="background:#fff">${statusPill(event.status)}
      ${source !== "recent" ? `<span class="pill p-hist">${esc(state.event ? "Selected by user" : "Historical · " + source)}</span>` : ""}
      <span class="pill p-none">Confidence: ${esc(event.confidence)} · score ${num(event.score)}/100</span>
      <span class="pill p-hist">Optical check: ${esc(v.optical || "not checked")}</span>
      <div style="margin-top:.45rem;font-size:1.05rem"><b>${esc(event.district)} district</b> · first detected on the Sentinel-1 acquisition of
      <b>${esc(event.first_detection)}</b>, largest extent observed <b>${esc(event.peak_date)}</b></div></div>
      <details><summary>Why this classification? (evidence checked)</summary><ul>${(event.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul></details>`;
    if (source !== "recent" && !state.event && run.message) s += note("warn", `${esc(run.message)} The most recent detected event from earlier seasons is shown below.`);
  } else {
    s += `<div class="card"><span class="pill p-none">No confirmed recent flood event detected</span><div style="margin-top:.4rem">No district showed a corroborated flood signal in the monitoring window${historical.length ? " or in the historical monsoon seasons searched" : ""}. Possible events (if any) are listed below.</div></div>`;
  }
  const w = run.monitoring_window || ["–", "–"];
  s += `<div class="grid g4 small" style="margin-top:.6rem">
    <div><b>Last successful refresh</b><br>${fmtTime(run.refreshed_at)}</div>
    <div><b>Latest Sentinel-1 acquisition used</b><br>${fmtTime(run.latest_acquisition)}</div>
    <div><b>Monitoring window</b><br>${esc(w[0])} → ${esc(w[1])} · ${esc(recent.monitoring_slices ?? "–")} acquisition slices</div>
    <div><b>Data source</b><br>Copernicus Sentinel-1/2 via Google Earth Engine${recent.errors?.length ? ` · ${recent.errors.length} slice(s) failed` : ""}</div></div>`;
  if (!detail && event && run.detail_error && !state.event) s += note("err", `The event was found but its detailed analysis failed: ${esc(run.detail_error)}`);
  status.innerHTML = s;

  // ---------------------------------------------------------- KPIs
  const crops = (detail || {}).crops || {}, rows = crops.rows || [];
  const named = rows.filter((r) => !r.crop.startsWith("Other") && r.affected_ha >= 1);
  const mainCrop = named.length ? named.reduce((a, b) => (b.affected_ha > a.affected_ha ? b : a)).crop : null;
  const level = detail ? overallRisk(rows) : null;
  const pct = detail && detail.total_cropland_ha ? 100 * detail.agricultural_flood_ha / detail.total_cropland_ha : null;
  body.innerHTML = kpis([
    ["k-blue", "🌊", "Total flooded area", detail ? ha(detail.flood_ha) : "—", detail ? `New water vs same-track baseline · ${detail.scale_m} m` : "No analyzed event"],
    ["k-green", "🌱", "Affected farmland", detail ? ha(detail.agricultural_flood_ha) : "—", pct !== null ? `${pct.toFixed(1)}% of district cropland (WorldCover)` : "ESA WorldCover 2021"],
    ["k-yellow", "🌽", "Main affected crop", detail ? (mainCrop || "Not identified") : "—", detail ? "ESA WorldCereal 2021 crop types" : ""],
    ["k-red", "⚠️", "Crop damage risk", level || "—", "Preliminary · flood exposure + NDVI change"],
    ["k-violet", "🛰️", "Event confidence", event ? event.confidence : "—", event ? event.status : "No confirmed event"],
  ]) + `
  <div class="grid g-dash">
    <div class="card">${panel(`Flood & Crop Impact Map${detail ? " – " + detail.district : ""}`)}<div class="map tall m-impact"></div><div class="lg-impact"></div></div>
    <div class="stack"><div class="card">${panel("Crop-wise Damage Estimate")}<div class="crop-tbl"></div></div>
      <div class="card">${panel("Affected Farmland by Crop")}<div class="chart c-donut"></div></div></div>
    <div class="stack"><div class="card">${panel("Damage Risk Map")}<div class="map short m-risk"></div><div class="lg-risk"></div></div>
      <div class="card">${panel("Key Findings & Recommendation")}<div class="findings"></div></div></div>
  </div>
  <div class="pairs" style="margin-top:1rem"></div>
  <div class="grid g2" style="margin-top:1rem">
    <div class="card">${panel("KPK overview – flood signal by district")}<div class="map m-kpk" style="height:380px"></div>
      ${legend([["Detected flood event", "#d7301f"], ["Possible flood event", "#fc8d59"], ["Signal rejected (not a flood)", "#a9a9a9"], ["No event in monitoring window", "#cfe3d6"]])}</div>
    <div class="stack"><div class="card">${panel("Rainfall evidence for the event")}<div class="chart c-rain"></div><div class="small muted rain-note"></div></div>
      <div class="card">${panel("Candidate flood events (monitoring window)")}<div class="cands"></div></div></div>
  </div>
  <details class="explore"><summary>🔎 Explore other and historical flood events</summary><div class="ex-body"></div></details>
  <div class="row" style="margin-top:1rem">
    <button class="btn primary b-pdf">⬇ Download PDF</button><button class="btn b-xls">📊 Export Excel</button>
    <button class="btn b-full" ${detail ? "" : "disabled"}>🗺 View Full Map</button></div>`;

  // crop table + donut
  if (rows.length) {
    body.querySelector(".crop-tbl").innerHTML = table([
      { label: "Crop", fmt: (r) => esc(shortCrop(r.crop)) }, { key: "affected_ha", label: "Area (ha)", num: 0 },
      { label: "ΔNDVI", fmt: (r) => signed(r.relative_ndvi_change, 2), num: 2 }, { label: "Risk", fmt: (r) => risk(r.risk) }], rows) +
      `<p class="small muted">Total flooded farmland: <b>${ha(detail.agricultural_flood_ha)}</b> · season: ${esc(crops.season)} · S2 scenes pre/post: ${esc(crops.s2_pre_scenes)}/${esc(crops.s2_post_scenes)}. ΔNDVI = flooded minus unflooded fields of the same crop. Risk = preliminary class, not a loss percentage.</p>`;
    const aff = rows.filter((r) => r.affected_ha >= 1), pal = crops.crop_palette || {};
    if (aff.length) donut(body.querySelector(".c-donut"), aff.map((r) => shortCrop(r.crop)), aff.map((r) => r.affected_ha),
      aff.map((r) => `#${pal[r.crop] || "9e9e9e"}`), `<b>${num(aff.reduce((a, r) => a + r.affected_ha, 0))} ha</b><br>affected`);
    else body.querySelector(".c-donut").outerHTML = note("info", "No flooded farmland to break down.");
  } else {
    body.querySelector(".crop-tbl").innerHTML = note("info", "Crop-wise results appear when an event has been analyzed.");
    body.querySelector(".c-donut").outerHTML = note("info", "No flooded farmland to break down.");
  }

  // findings
  const f = body.querySelector(".findings");
  if (detail && event) {
    const items = [`🌊 ~${num(detail.flood_ha)} ha of new surface water in ${esc(detail.district)} between ${esc(detail.onset_window[0])} and ${esc(detail.onset_window[1])} (Sentinel-1).`,
      `🌱 ~${num(detail.agricultural_flood_ha)} ha of farmland inside the flood extent${pct !== null ? ` (${pct.toFixed(1)}% of district cropland)` : ""}.`,
      mainCrop ? `🌽 Most affected mapped crop: ${esc(mainCrop)}.` : "🌽 Mapped crop types (WorldCereal) cover little of the flooded farmland; crop type is not identified."];
    const pr = (detail.tehsil_priorities || []).filter((p) => ["High", "Medium"].includes(p.priority));
    if (pr.length) items.push("📍 Priority areas for field inspection: " + pr.slice(0, 3).map((p) => `${esc(p.area)} (${esc(p.priority)})`).join(", ") + ".");
    const wet = detail.wettest_day_in_onset_window;
    if (wet) items.push(`🌧️ Wettest day in the onset window: ${esc(wet.date)} (${num(wet.mm)} mm, ${esc(String(detail.rainfall?.source || "").split(" (")[0])}).`);
    if (ROD_KOHI.includes(detail.district)) items.push("⛰️ This district uses rod-kohi (hill-torrent spate) irrigation: torrent water is deliberately spread on bunded fields. Satellites confirm water on farmland but cannot tell planned spate irrigation from a damaging flood — check local reports/field visits.");
    if (event.concurrent_districts?.length) items.push("🗺️ Concurrent flood signal in: " + event.concurrent_districts.map(esc).join(", ") + ".");
    items.push({ High: "✅ Field verification recommended in high-risk patches before any compensation decision.", Medium: "✅ Field verification recommended for medium/high-risk patches.",
      Low: "✅ NDVI shows no clear vegetation loss on flooded fields; spot-check flooded farmland.", "Insufficient Data": "✅ Cloud-free Sentinel-2 data are insufficient; field inspection is needed to assess crops." }[level] || "");
    f.innerHTML = items.map((x) => `<p style="margin:.3rem 0">${x}</p>`).join("");
  } else f.innerHTML = "<p>No confirmed event, so no damage assessment is produced. Review possible events below or analyze a historical period.</p>";

  // rainfall
  const rain = (detail || {}).rainfall || {};
  const series = (rain.series || []).filter((x) => x.mm !== null && x.mm !== undefined);
  if (series.length) {
    barChart(body.querySelector(".c-rain"), series.map((x) => x.date), series.map((x) => x.mm), { yTitle: "mm / day (district mean)",
      marks: [{ x: detail.before.date, label: "Before image" }, { x: detail.after.date, label: "After image" }] });
    body.querySelector(".rain-note").textContent = `Source: ${rain.source}. Rainfall is supporting evidence only; riverine floods can come from rain far upstream.`;
  } else body.querySelector(".c-rain").outerHTML = note("info", "Rainfall series appears for an analyzed event.");

  // candidates
  const events = recent.events || [];
  body.querySelector(".cands").innerHTML = events.length ? table([
    { key: "district", label: "District" }, { label: "Status", fmt: (e) => statusPill(e.status) }, { key: "confidence", label: "Confidence" },
    { key: "first_detection", label: "First detection" }, { key: "peak_date", label: "Peak" }, { key: "peak_excess_ha", label: "Excess new water (ha)", num: 0 },
    { key: "peak_ratio", label: "× normal", num: 1 }, { key: "rainfall_7d_mm", label: "7-day rain (mm)", num: 0 }, { key: "score", label: "Score", num: 0 }], events, { scroll: true }) +
    `<p class="small muted">Screening at ${esc(run.params?.screening_scale_m)} m. "Excess" = new water above each district's normal level learned from the ${esc(run.params?.background_days)} days before the window (irrigation, paddies and river beds create a recurring signal).</p>`
    : note("info", "No district exceeded the possible-event threshold in the monitoring window.");

  // KPK overview map
  districtsGeo().then((geo) => {
    const el = body.querySelector(".m-kpk"); if (!el || !window.L) return;
    const summary = Object.fromEntries((recent.district_summary || []).map((d) => [d.district, d]));
    const statusBy = {}; events.forEach((e) => { if (!statusBy[e.district]) statusBy[e.district] = e.status; });
    const map = L.map(el, { scrollWheelZoom: false }); L.tileLayer(ESRI_GRAY, { attribution: "Basemap © Esri", maxZoom: 16 }).addTo(map);
    L.geoJSON(geo, {
      style: (ft) => { const n = ft.properties.district, st = statusBy[n] || "";
        const c = st.startsWith("Detected") ? "#d7301f" : st.startsWith("Possible") ? "#fc8d59" : st.startsWith("Rejected") ? "#a9a9a9" : "#cfe3d6";
        const sel = event && n === event.district; return { fillColor: c, color: sel ? "#0f3d2e" : "#5b7a6a", weight: sel ? 3 : .8, fillOpacity: .75 }; },
      onEachFeature: (ft, layer) => { const n = ft.properties.district, sm = summary[n];
        layer.bindTooltip(`<b>${esc(n)}</b><br>${esc(statusBy[n] || "No event")}<br>${sm ? num(sm.max_excess_ha) + " ha above normal (max)" : "no observation in window"}`); },
    }).addTo(map);
    map.fitBounds([[31.2, 69.2], [36.9, 74.0]]); map.on("click", () => map.scrollWheelZoom.enable());
  }).catch(() => {});

  // live Earth Engine layers for the shown event
  const key = detail ? JSON.stringify([detail.district, detail.before?.date, detail.after?.date]) : null;
  if (detail) {
    const mI = body.querySelector(".m-impact"), mR = body.querySelector(".m-risk");
    mI.innerHTML = `<div style="padding:1rem"><span class="spin"></span> Loading live Earth Engine layers…</div>`;
    const cached = state.layers.get(key);
    (cached ? Promise.resolve(cached) : post("/api/events/layers", { detail })).then((L) => {
      state.layers.set(key, L); mI.innerHTML = "";
      const impact = [{ url: L.flood_blue, name: "Flooded area", opacity: .85 }, { url: L.cropland, name: "Farmland (crops)", opacity: .55 }, { url: L.flooded_cropland, name: "Flooded farmland", opacity: .95 }];
      makeMap(mI, detail.geometry, impact, { control: true });
      body.querySelector(".lg-impact").innerHTML = legend([["District boundary", "#ffffff"], ["Flooded area", "#2f7de1"], ["Farmland (crops)", "#f2c12e"], ["Flooded farmland", "#f26b1d"]]);
      makeMap(mR, detail.geometry, [{ url: L.risk, name: "Damage risk", opacity: .95 }]);
      const ra = crops.risk_area_ha || {};
      body.querySelector(".lg-risk").innerHTML = legend(["High", "Medium", "Low", "Insufficient Data"].map((k) => [`${k} · ${num(ra[k] || 0)} ha`, RISK_COLORS[k]]));
      drawPairs(body.querySelector(".pairs"), detail, L);
      body.querySelector(".b-full").onclick = () => fullMap(detail.geometry, [...impact, { url: L.risk, name: "Damage risk", opacity: .95 }], `${detail.district} – flood & crop impact`);
    }).catch((e) => { mI.innerHTML = note("err", `Live Earth Engine map layers could not be generated: ${esc(e.message)}`); });
  } else {
    body.querySelector(".m-impact").outerHTML = note("info", "No analyzed flood event to map.");
    body.querySelector(".m-risk").outerHTML = note("info", "Risk map appears when an event has been analyzed.");
  }

  // exports
  const exp = (fmt) => async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    try {
      const req = state.event ? { result: { district: detail?.district, status: event?.status, window: run.monitoring_window, detail, event, computed_at: new Date().toISOString() }, title: "dashboard event" }
        : run.run_id ? { run_id: run.run_id } : null;
      if (!req) throw new Error("Save the run first (refresh).");
      await download(`/api/report/${fmt}`, req, `ZarailensAI - KPK event.${fmt}`);
    } catch (e) { toast(`Export failed: ${e.message}`, true); } finally { b.disabled = false; }
  };
  body.querySelector(".b-pdf").addEventListener("click", exp("pdf"));
  body.querySelector(".b-xls").addEventListener("click", exp("xlsx"));

  drawExplore(body.querySelector(".ex-body"), run, () => drawRun(status, body, run, m, refresh));
}

function drawPairs(el, d, L) {
  const t = (s) => `Sentinel-1 VV · acquired ${esc(String(s.time_utc || s.date).slice(0, 16).replace("T", " "))} UTC · ${esc(String(s.orbit || "").toLowerCase())} orbit ${esc(s.relative_orbit)} · ${(s.ids || [1]).length} frame(s) · AOI coverage ${num(s.coverage)}%`;
  const r = d.rule || {};
  el.innerHTML = `<div class="card">${panel("Satellite images – before and after (actual acquisitions)")}
    <div class="grid g3">
      <div><b>Before flood</b><div class="map short"></div><div class="tiny muted">${t(d.before)}</div></div>
      <div><b>After flood</b><div class="map short"></div><div class="tiny muted">${t(d.after)}</div></div>
      <div><b>Flood detection result</b><div class="map short"></div><div class="tiny muted">Green = new water · rule: VV change &lt; ${esc(r.change_db)} dB, VV &lt; ${esc(r.water_db)} dB, VH &lt; ${esc(r.vh_water_db)} dB; permanent water (JRC ≥90%), slope &gt; ${esc(r.max_slope_deg)}°, HAND &gt; ${esc(r.max_hand_m)} m and elevation &gt; ${esc(r.max_elevation_m)} m excluded · baseline: ${(d.baseline?.dates || []).length}-date same-track median</div></div>
    </div>
    <p style="margin:.8rem 0 .3rem"><b>Sentinel-2 natural colour – what the ground looked like</b></p>
    <div class="grid g3">
      <div><b>Before (optical)</b><div class="map short s2b"></div><div class="tiny muted">${L.s2_before ? "Sentinel-2 dates: " + esc(L.s2_before.dates.join(", ")) + " · clouds masked" : ""}</div></div>
      <div><b>After (optical)</b><div class="map short s2a"></div><div class="tiny muted">${L.s2_after ? "Sentinel-2 dates: " + esc(L.s2_after.dates.join(", ")) + " · clouds masked" : ""}</div></div>
      <div><b>How to read these</b><p class="small">Radar marks surfaces that became dark. If the optical <i>after</i> image shows dry fields where radar shows new water, the signal is wet soil or field preparation — the app then rejects it as a flood. Monsoon floods are often under clouds; then only radar and its spatial pattern are used.</p></div>
    </div></div>`;
  const maps = el.querySelectorAll(".map"); const g = d.geometry;
  makeMap(maps[0], g, [{ url: L.before, name: "Before" }], { zoomControl: false });
  makeMap(maps[1], g, [{ url: L.after, name: "After" }], { zoomControl: false });
  makeMap(maps[2], g, [{ url: L.after, name: "After" }, { url: L.flood, name: "New water", opacity: .9 }], { zoomControl: false, control: false });
  if (L.s2_before) makeMap(maps[3], g, [{ url: L.s2_before.url, name: "S2 before" }], { zoomControl: false }); else maps[3].outerHTML = note("info", "No Sentinel-2 image in this window (clouds or no pass).");
  if (L.s2_after) makeMap(maps[4], g, [{ url: L.s2_after.url, name: "S2 after" }], { zoomControl: false }); else maps[4].outerHTML = note("info", "No Sentinel-2 image in this window (clouds or no pass).");
}

function drawExplore(el, run, redraw) {
  const options = [];
  (run.recent?.events || []).forEach((e) => options.push(["Monitoring window", e]));
  (run.historical || []).forEach((h) => (h.events || []).forEach((e) => options.push([h.label || "Historical", e])));
  const today = new Date().toISOString().slice(0, 10);
  el.innerHTML = `
    ${options.length ? `<div class="form"><label class="f">Show another candidate event<select class="sel">${options.map(([lab, e], i) =>
      `<option value="${i}">${esc(lab)} · ${esc(e.district)} · ${esc(e.status)} · peak ${esc(e.peak_date)} · ${num(e.peak_excess_ha)} ha excess</option>`).join("")}</select></label>
      <button class="btn primary b-show">Analyse and show</button></div>` : note("info", "No other candidate events in the stored run.")}
    ${state.event ? `<p><button class="btn b-back">↩ Back to the automatically selected event</button></p>` : ""}
    <p style="margin-top:1rem"><b>Search a past period across KPK</b> (same automatic method)</p>
    <div class="form"><label class="f">Start<input type="date" class="hs" min="2015-01-01" max="${today}" value="2022-08-01"></label>
      <label class="f">End<input type="date" class="he" min="2015-01-02" max="${today}" value="2022-09-15"></label>
      <button class="btn b-hist">Search this period</button></div><div class="ex-prog"></div>`;
  const prog = el.querySelector(".ex-prog");
  const show = async (event, label) => {
    const p = progress(prog, `Analysing ${event.district} (1–4 min)…`);
    try { state.detail = await post("/api/events/analyze", { event }); state.event = event; state.source = label; p.done(); redraw(); window.scrollTo({ top: 0, behavior: "smooth" }); }
    catch (e) { p.fail(e); }
  };
  el.querySelector(".b-show")?.addEventListener("click", () => { const [lab, e] = options[+el.querySelector(".sel").value]; show(e, lab); });
  el.querySelector(".b-back")?.addEventListener("click", () => { state.event = null; state.detail = null; redraw(); });
  el.querySelector(".b-hist").addEventListener("click", async () => {
    const start = el.querySelector(".hs").value, end = el.querySelector(".he").value;
    if (!start || !end || end <= start) return toast("Choose a start date before the end date.", true);
    const p = progress(prog, `Searching KPK ${start} → ${end} in real Sentinel-1 data…`);
    try {
      const found = await discover({ start, end, onProgress: (f, t) => p.set(f, t) });
      p.done();
      if (!found.selected) { prog.innerHTML = note("info", `No detected flood event in ${esc(start)} → ${esc(end)}. ${found.events.length} candidate(s) found.`); return; }
      await show(found.selected, `Historical search ${start} → ${end}`);
    } catch (e) { p.fail(e); }
  });
}
