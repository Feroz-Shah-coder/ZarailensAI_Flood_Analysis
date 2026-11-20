// Long Earth Engine work as a chain of short API requests (each well inside Vercel's function time limit).
import { post, meta } from "./api.js";
import { pool } from "./ui.js";

const BATCH = 6;          // Sentinel-1 slices per screening request
const PARALLEL = 1;       // screening requests at once (each already runs 3 Earth Engine jobs)

/** Screen a window (KPK-wide or some districts) and return scored rows + candidate events. */
export async function screen({ start, end, districts = null, onProgress = () => {}, from = 0, span = 0.7 }) {
  onProgress(from, "Listing Sentinel-1 acquisitions in Earth Engine");
  const plan = await post("/api/screen/plan", { start, end, districts });
  const batches = [];
  for (let i = 0; i < plan.keys.length; i += BATCH) batches.push(plan.keys.slice(i, i + BATCH));
  let done = 0; const rows = []; const errors = [];
  await pool(batches, PARALLEL, async (keys) => {
    let out;
    for (let attempt = 0; attempt < 3; attempt++) {
      try { out = await post("/api/screen/run", { keys, start: plan.start, end: plan.end, background_start: plan.background_start, districts }); break; }
      catch (e) { if (attempt === 2) { errors.push(`${keys[0]?.[0]}…: ${e.message}`); out = { rows: [], errors: [] }; } else await new Promise((r) => setTimeout(r, 4000 * (attempt + 1))); }
    }
    rows.push(...out.rows); errors.push(...out.errors);
    done += keys.length;
    onProgress(from + span * done / Math.max(plan.keys.length, 1), `Measured ${done}/${plan.keys.length} Sentinel-1 acquisition slices`);
  });
  const ev = await post("/api/screen/events", { rows });
  return { plan, rows, errors, ...ev };
}

/** Same method as the former run_monitoring "discover_events": screen, assess every candidate, rank. */
export async function discover({ start, end, districts = null, onProgress = () => {}, from = 0, span = 1 }) {
  const s = await screen({ start, end, districts, onProgress, from, span: span * 0.8 });
  onProgress(from + span * 0.82, `Checking rainfall and Sentinel-2 evidence for ${s.events.length} candidate event(s)`);
  let n = 0;
  const assessed = await pool(s.events, 2, async (event) => {
    const a = await post("/api/events/assess", { event });
    n++; onProgress(from + span * (0.82 + 0.15 * n / Math.max(s.events.length, 1)), `Assessed ${n}/${s.events.length}: ${event.district}`);
    return a;
  });
  const fin = await post("/api/screen/finalize", { events: assessed, scored: s.scored });
  return { window: [s.plan.start, s.plan.end], background_window: [s.plan.background_start, s.plan.start],
           slices: s.plan.keys.length, monitoring_slices: s.plan.monitoring_slices, errors: s.errors,
           events: fin.events, selected: fin.selected, backgrounds: s.backgrounds, district_summary: s.district_summary };
}

/** Full KPK dashboard refresh (same result format as the previous automatic monitoring run). */
export async function dashboardRefresh(onProgress = () => {}) {
  const m = await meta();
  const run = { refreshed_at: new Date().toISOString(), params: m.params };
  try {
    onProgress(0.01, "Checking the latest Sentinel-1 data available in Earth Engine");
    const latest = await post("/api/latest", {});
    run.latest_acquisition = latest.latest_acquisition;
    if (!latest.latest_acquisition) {
      return { ...run, status: "no_data", message: "No Sentinel-1 acquisitions over KPK were returned for the last 60 days." };
    }
    run.monitoring_window = latest.monitoring_window;
    const recent = await discover({ start: latest.start, end: latest.end, onProgress, from: 0.02, span: 0.6 });
    run.recent = recent; run.historical = [];
    let selected = recent.selected, source = "recent";
    if (!selected) {
      const wins = latest.fallback_windows || [];
      for (let i = 0; i < wins.length && !selected; i++) {
        const w = wins[i];
        const found = await discover({ start: w.start, end: w.end, onProgress: (f, t) => onProgress(f, `${w.label}: ${t}`),
                                       from: 0.62 + 0.28 * i / wins.length, span: 0.28 / wins.length });
        found.label = w.label; run.historical.push(found);
        if (found.selected) { selected = found.selected; source = w.label; }
      }
    }
    run.selected = selected; run.selected_source = source;
    if (selected) {
      onProgress(0.92, `Analysing ${selected.district}: before/after pair, farmland, crops, NDVI (1–4 min)`);
      try { run.detail = await post("/api/events/analyze", { event: selected }); }
      catch (e) { run.detail = null; run.detail_error = e.message; }
    }
    run.status = selected && source === "recent" ? "event" : selected ? "historical_event" : "no_event";
    run.message = source === "recent" && selected ? null : "No confirmed recent flood event detected in the monitoring window.";
    onProgress(1, "Monitoring run complete");
  } catch (e) {
    run.status = "error"; run.message = e.message;
  }
  return run;
}

/** One district and window, analysed like a reported event (Sentinel era). Returns the catalog result format. */
export async function districtWindow({ district, start, end, onProgress = () => {} }) {
  const s = await screen({ start, end, districts: [district], onProgress, from: 0.02, span: 0.45 });
  const rows = s.scored.filter((r) => r.district === district).sort((a, b) => a.date.localeCompare(b.date));
  const result = {
    era: "sentinel", window: [start, end], errors: s.errors,
    timeline: rows.map((r) => ({ date: r.date, track: `${r.orbit} ${r.relative_orbit}`, new_water_ha: +(+r.flood_ha).toFixed(1),
      excess_ha: +(+r.excess_ha).toFixed(1), x_normal: +(+r.ratio).toFixed(2), phase: r.phase })),
    acquisitions_in_window: rows.filter((r) => r.phase === "monitoring").length,
  };
  const candidates = s.events.filter((e) => e.district === district);
  if (!candidates.length) {
    return { ...result, status: "No flood signal detected",
             message: "No Sentinel-1 acquisition in this window showed new water clearly above the district's normal level." };
  }
  const strongest = candidates.reduce((a, b) => (b.peak_excess_ha > a.peak_excess_ha ? b : a));
  onProgress(0.5, "Checking rainfall, Sentinel-2 optical water and seasonal water");
  const fin = await post("/api/screen/finalize", {
    events: [await post("/api/events/assess", { event: { ...strongest, concurrent_districts: [] } })], scored: s.scored });
  const assessed = fin.events[0];
  Object.assign(result, { status: assessed.status, event: assessed, other_candidates: candidates.filter((c) => c !== strongest) });
  onProgress(0.62, "Retrieving the before/after pair and measuring flood, farmland, crops and NDVI (1–4 min)");
  try { result.detail = await post("/api/events/analyze", { event: assessed }); }
  catch (e) { result.detail_error = e.message; }
  return result;
}
