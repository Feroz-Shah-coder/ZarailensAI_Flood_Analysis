// Automatic Flood Monitoring: 5 districts, new Sentinel-1 acquisitions only; Vercel Cron or "Run check now".
import { api, post, download } from "../api.js";
import { esc, num, kpis, note, panel, table, risk, statusPill, tabs, progress, toast, fmtTime, signed } from "../ui.js";
import { state } from "../state.js";

const VERIFICATION = ["Unverified", "Field visit planned", "Confirmed flood (field)", "Partially confirmed", "Not a flood (field)"];
let tabIndex = 0;

export async function render(el) {
  el.innerHTML = `<div class="skeleton"></div>`;
  const s = await api("/api/monitor/status");
  const events = s.events || [], alerts = (s.alerts || []).filter((a) => !a.acknowledged_at && !a.acknowledged);
  const lastOk = (s.checks || []).find((c) => ["success", "partial"].includes(c.status) && c.finished_at);
  const count = (pred) => events.filter(pred).length;
  el.innerHTML = `${panel(`Automatic Flood Monitoring – ${s.districts.join(" · ")}`)}
    ${note("info", "🕒 " + esc(s.note))}
    ${alerts.map((a) => note("err", "🔔 " + esc(a.message))).join("")}
    ${alerts.length ? `<button class="btn b-ack">✓ Mark alerts as read</button>` : ""}
    ${kpis([
      ["k-green", "🛰️", "Automatic checks", "Vercel Cron", `daily (Hobby) or every ${s.interval_hours} h (Pro) · see DEPLOYMENT.md`],
      ["k-blue", "✅", "Last successful check", lastOk ? fmtTime(lastOk.finished_at).replace(" UTC", "") : "Never", lastOk ? "UTC" : "no check yet"],
      ["k-violet", "⏱️", "Next check due", s.next_run ? fmtTime(s.next_run).replace(" UTC", "") : "—", s.next_run ? "UTC (interval from settings)" : "after the first check"],
      ["k-yellow", "📍", "Districts monitored", String(s.districts.length), s.districts.join(", ")],
      ["k-red", "⚠️", "Detected events", String(count((e) => e.event_status === "Detected flood event")),
        `${count((e) => e.event_status === "Possible flood event")} possible · ${count((e) => String(e.event_status).startsWith("Rejected"))} rejected`],
    ])}
    <div class="row"><button class="btn primary b-check">⟳ Run check now</button><button class="btn b-reload">↻ Reload</button>
      <span class="small muted">Checks each district for Sentinel-1 acquisitions newer than the last processed one (keep the page open; ~1–3 min per district).</span></div>
    <div class="m-prog"></div><div class="m-tabs" style="margin-top:1rem"></div>`;
  el.querySelector(".b-reload").addEventListener("click", () => render(el));
  el.querySelector(".b-ack")?.addEventListener("click", async () => { try { await post("/api/monitor/alerts/ack"); render(el); } catch (e) { toast(e.message, true); } });
  el.querySelector(".b-check").addEventListener("click", async (ev) => {
    ev.currentTarget.disabled = true;
    const p = progress(el.querySelector(".m-prog"), "Monitoring check running…"); const out = [];
    for (let i = 0; i < s.districts.length; i++) {
      const d = s.districts[i]; p.set(i / s.districts.length, `${d}: searching new Sentinel-1 acquisitions and assessing any candidate event`);
      try { const r = await post("/api/monitor/check", { district: d }); out.push(`${d}: ${r.message || r.status}`); (r.alerts || []).forEach((a) => toast(a, true)); }
      catch (e) { out.push(`${d}: failed – ${e.message}`); }
    }
    p.done(); el.querySelector(".m-prog").innerHTML = note("ok", out.map(esc).join("<br>")); setTimeout(() => render(el), 1500);
  });

  tabs(el.querySelector(".m-tabs"), ["Districts", "Candidate events", "Check log"], (i, body) => {
    tabIndex = i;
    if (i === 0) {
      body.innerHTML = table([{ key: "district", label: "District" }, { label: "Last processed acquisition", fmt: (r) => fmtTime(r.last_acquisition) },
        { label: "Last check", fmt: (r) => fmtTime(r.last_check_at) }, { key: "status", label: "Status" }, { key: "stored", label: "Acquisitions stored", num: 0 },
        { key: "quality", label: "Latest acquisition quality" }, { key: "error", label: "Last error", fmt: (r) => `<span class="small">${esc(r.error || "")}</span>`, wrap: true }], s.district_rows) +
        `<p class="small muted">Each Sentinel-1 acquisition (date + orbit track) is measured once and stored; checks only process acquisitions newer than the last processed one. Quality flags: partial district coverage, shallow baseline, VV-only polarisation.</p>`;
    } else if (i === 1) drawEvents(body, events, () => render(el));
    else {
      body.innerHTML = (s.checks || []).length ? table([{ key: "id", label: "Check" }, { label: "Started (UTC)", fmt: (c) => fmtTime(c.started_at) },
        { key: "trigger", label: "Trigger" }, { key: "status", label: "Status" }, { key: "new_slices", label: "New acquisitions", num: 0 },
        { key: "events_created", label: "New events", num: 0 }, { key: "events_updated", label: "Updated events", num: 0 },
        { key: "message", label: "Message", wrap: true }], s.checks) : note("info", "No monitoring check has run yet.");
    }
  }, tabIndex);
}

function drawEvents(body, events, reload) {
  if (!events.length) { body.innerHTML = note("info", "No candidate events yet. A new satellite image alone never creates an event: new water must exceed the district's normal level and pass rainfall, optical (Sentinel-2), spatial-pattern and seasonal-water checks."); return; }
  const ranked = events.filter((e) => e.event_status !== "Rejected – not a flood").sort((a, b) => (b.priority_score || 0) - (a.priority_score || 0));
  body.innerHTML = table([{ key: "event_id", label: "Event" }, { key: "district", label: "District" }, { label: "Status", fmt: (e) => statusPill(e.event_status) },
    { key: "confidence", label: "Confidence" }, { label: "Detected at", fmt: (e) => fmtTime(e.detected_at) }, { key: "first_detection", label: "First detection" },
    { key: "before_date", label: "Before image" }, { key: "after_date", label: "After image" }, { key: "flood_ha", label: "Flooded area (ha)", num: 0 },
    { key: "agricultural_exposure_ha", label: "Farmland exposed (ha)", num: 0 }, { label: "Priority", fmt: (e) => risk(e.priority) },
    { key: "verification_status", label: "Verification" }, { key: "processing_status", label: "Processing" }], events,
    { scroll: true, onRowAttr: (e) => `class="click" data-id="${esc(e.event_id)}"` }) +
    (ranked.length ? `<h4 style="margin-top:1rem">Field inspection order</h4><p class="small muted">Existing inspection_priority formula: 0.4 farmland area + 0.3 farmland share + 0.2 NDVI loss + 0.1 evidence confidence.</p>` +
      table([{ label: "Rank", fmt: (e) => String(ranked.indexOf(e) + 1) }, { key: "event_id", label: "Event" }, { key: "district", label: "District" },
        { label: "Priority", fmt: (e) => risk(e.priority) }, { key: "priority_score", label: "Score", num: 2 },
        { label: "Why", fmt: (e) => esc(Object.entries(e.data_refs?.priority_inputs || {}).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ")), wrap: true }], ranked) : "") +
    `<div class="form" style="margin-top:1rem"><label class="f">Event details<select class="esel">${events.map((e) => `<option value="${esc(e.event_id)}">${esc(e.event_id)} · ${esc(e.event_status)} · ${esc(e.district)} · peak ${esc(e.peak_date)}</option>`).join("")}</select></label></div><div class="e-detail"></div>`;
  const sel = body.querySelector(".esel");
  const show = () => drawEvent(body.querySelector(".e-detail"), events.find((e) => e.event_id === sel.value), reload);
  sel.addEventListener("change", show);
  body.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => { sel.value = tr.dataset.id; show(); sel.scrollIntoView({ behavior: "smooth" }); }));
  show();
}

function drawEvent(el, e, reload) {
  const d = e.detail, ev = e.event || {}, rejected = e.event_status === "Rejected – not a flood";
  const crops = d?.crops?.rows || [];
  el.innerHTML = `<div class="card" style="margin-top:.6rem"><div class="grid g2">
    <div><b>${esc(e.district)}</b> – ${esc(e.event_status)} · confidence ${esc(e.confidence)} · score ${num(e.score)}
      <div class="small muted">Sentinel-1 before: ${esc(e.before_date || "n/a")} · after (peak): ${esc(e.after_date)} · first detection ${esc(e.first_detection)} · tracks ${esc((e.acquisitions?.tracks || []).join(", "))}</div>
      <ul>${(ev.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>
    <div><label class="f">Ground verification status<select class="vsel">${VERIFICATION.map((v) => `<option ${v === e.verification_status ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <button class="btn b-ver" style="margin-top:.5rem">Save verification status</button>
      <p class="small muted">Field records can be added on the Field Verification page.</p></div></div>
    ${d ? kpis([["k-blue", "🌊", "Flooded area", `${num(d.flood_ha)} ha`, ""], ["k-green", "🌱", "Flooded farmland (exposure)", `${num(d.agricultural_flood_ha)} ha`, ""], ["k-red", "📍", "Inspection priority", e.priority || "—", ""]]) +
      (crops.length ? table([{ key: "crop", label: "Crop" }, { key: "affected_ha", label: "Flooded area (ha)", num: 0 }, { label: "NDVI change flooded", fmt: (r) => signed(r.ndvi_change_flooded), num: 3 },
        { label: "NDVI change unflooded", fmt: (r) => signed(r.ndvi_change_unflooded), num: 3 }, { label: "Relative change", fmt: (r) => signed(r.relative_ndvi_change), num: 3 }, { label: "Damage risk", fmt: (r) => risk(r.risk) }], crops) : "")
      : !rejected ? `<p class="small muted">Screening result only (200 m). Use "View satellite maps" to run the full analysis (1–4 min).</p>` : ""}
    <div class="row" style="margin-top:.8rem"><button class="btn primary b-maps" ${rejected ? "disabled" : ""}>🗺 View satellite maps</button>
      <button class="btn b-pdf">⬇ PDF</button><button class="btn b-xls">📊 Excel</button></div><div class="e-prog"></div></div>`;
  el.querySelector(".b-ver").addEventListener("click", async () => {
    try { await post(`/api/monitor/events/${encodeURIComponent(e.event_id)}/verification`, { status: el.querySelector(".vsel").value }); toast("Verification status saved."); reload(); }
    catch (err) { toast(err.message, true); }
  });
  el.querySelector(".b-maps").addEventListener("click", async () => {
    let detail = d;
    if (!detail) {
      const p = progress(el.querySelector(".e-prog"), "Running the full event analysis on real Earth Engine data…");
      try { detail = await post(`/api/monitor/events/${encodeURIComponent(e.event_id)}/analyze`); p.done(); } catch (err) { p.fail(err); return; }
    }
    state.event = ev; state.detail = detail; state.source = "automatic district monitoring"; location.hash = "#/";
  });
  const report = (fmt) => async () => {
    const result = { district: e.district, status: e.event_status, window: [e.first_detection, e.peak_date], event: ev, detail: d, computed_at: e.updated_at,
      message: `Event ${e.event_id} · verification: ${e.verification_status} · inspection priority: ${e.priority}` };
    try { await download(`/api/report/${fmt}`, { result, title: "automatic district monitoring" }, `ZarailensAI - ${e.event_id}.${fmt}`); } catch (err) { toast(err.message, true); }
  };
  el.querySelector(".b-pdf").addEventListener("click", report("pdf"));
  el.querySelector(".b-xls").addEventListener("click", report("xlsx"));
}
