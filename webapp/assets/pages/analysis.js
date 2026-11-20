// New Analysis: any KPK district and period, measured with the same method as the reported events.
import { meta, post } from "../api.js";
import { esc, note, panel, progress, toast, table, num, statusPill } from "../ui.js";
import { districtWindow } from "../pipeline.js";
import { renderResult } from "../result_view.js";

let last = null;   // keep the last result while navigating

export async function render(el) {
  const m = await meta();
  const today = new Date().toISOString().slice(0, 10);
  el.innerHTML = `${panel("New Analysis – analyze a selected area and period")}
    <div class="card"><div class="form">
      <label class="f">District<select class="d">${m.districts.map((d) => `<option ${d === (last?.district || "Nowshera") ? "selected" : ""}>${esc(d)}</option>`).join("")}</select></label>
      <label class="f">Start date<input type="date" class="s" min="2010-01-01" max="${today}" value="${last?.window?.[0] || "2022-08-15"}"></label>
      <label class="f">End date<input type="date" class="e" min="2010-01-02" max="${today}" value="${last?.window?.[1] || "2022-09-15"}"></label>
      <button class="btn primary b-go">▶ Analyze with real satellite data</button></div>
      <p class="small muted" style="margin:.6rem 0 0">From 2015: every Sentinel-1 radar acquisition in the period is measured against the district's normal level; the strongest
      signal is checked (rainfall, Sentinel-2 optical water, flood pattern, seasonal water) and analysed in full (2–8 min, keep the page open).
      Before 2015: Landsat optical water change (about 30 s). Periods up to ~4 months work best.</p></div>
    <div class="a-prog" style="margin-top:.8rem"></div><div class="a-out" style="margin-top:.8rem"></div>`;
  const out = el.querySelector(".a-out"), prog = el.querySelector(".a-prog");
  const show = (r) => {
    out.innerHTML = `<div class="row"><h3 style="margin:0">${esc(r.district)} · ${esc(r.window[0])} → ${esc(r.window[1])}</h3>${statusPill(r.status)}<span class="spacer"></span>
      <button class="btn b-save">💾 Save to Analysis History</button></div><div class="r"></div><div class="others"></div>`;
    renderResult(out.querySelector(".r"), r, { windowLabel: `New analysis ${r.window[0]} → ${r.window[1]}` });
    if (r.other_candidates?.length) out.querySelector(".others").innerHTML = `<h4 style="margin-top:1rem">Other flood signals in this period (screening level)</h4>` +
      table([{ key: "first_detection", label: "First detection" }, { key: "peak_date", label: "Peak" }, { key: "peak_excess_ha", label: "Excess new water (ha)", num: 0 }, { key: "peak_ratio", label: "× normal", num: 1 }], r.other_candidates);
    out.querySelector(".b-save").addEventListener("click", async (ev) => {
      ev.currentTarget.disabled = true;
      try { const { id } = await post("/api/analyses", { result: { ...r, label: `New analysis ${r.window[0]} → ${r.window[1]}` } }); toast(`Saved as analysis #${id}.`); }
      catch (e) { toast(e.message, true); ev.currentTarget.disabled = false; }
    });
  };
  if (last) show(last);
  el.querySelector(".b-go").addEventListener("click", async (ev) => {
    const district = el.querySelector(".d").value, start = el.querySelector(".s").value, end = el.querySelector(".e").value;
    if (!start || !end || end <= start) return toast("Choose a start date before the end date.", true);
    if ((new Date(end) - new Date(start)) / 864e5 > 366) return toast("Choose a period of at most one year.", true);
    const btn = ev.currentTarget; btn.disabled = true; out.innerHTML = "";
    const p = progress(prog, `Analysing ${district}, ${start} → ${end}…`);
    try {
      let r;
      if (start < "2015-01-01") { p.set(.2, "Searching Landsat scenes, measuring water change, farmland and NDVI"); r = await post("/api/analysis/landsat", { district, start, end }); }
      else {
        r = await districtWindow({ district, start, end, onProgress: (f, t) => p.set(f, t) });
        r.district = district; r.computed_at = new Date().toISOString();
        if (r.detail) r.geometry = r.detail.geometry; else r.geometry = await fetch(`/api/geo/district/${encodeURIComponent(district)}`).then((x) => x.json());
      }
      last = r; p.done(); show(r);
    } catch (e) { p.fail(e); } finally { btn.disabled = false; }
  });
}
