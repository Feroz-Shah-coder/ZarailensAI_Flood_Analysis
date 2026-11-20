// Field Verification: tehsil inspection priorities and ground observations (the ground truth any damage model needs).
import { api, post } from "../api.js";
import { esc, note, panel, table, risk, toast, ESRI_IMAGERY } from "../ui.js";
import { state } from "../state.js";

const FLOODING = ["Yes – standing water", "Yes – recent water marks/silt", "No"];
const CROPS = ["Maize", "Rice", "Sugarcane", "Wheat", "Cotton", "Vegetables", "Fodder", "Orchard", "Fallow/none", "Other"];
const DAMAGE = ["None", "Low (<25% of plants affected)", "Medium (25–50%)", "High (>50%)", "Total loss"];

export async function render(el) {
  let run = state.run;
  if (!run) { try { run = (await api("/api/dashboard")).run; state.run = run; } catch { run = null; } }
  const event = state.event || run?.selected, detail = state.event ? state.detail : run?.detail;
  const today = new Date().toISOString().slice(0, 10);
  const sel = (cls, opts) => `<select class="${cls}">${opts.map((o) => `<option>${esc(o)}</option>`).join("")}</select>`;
  el.innerHTML = `${panel("Field Verification")}
    <h4>Inspection priorities</h4><div class="prio"></div>
    <h4 style="margin-top:1.2rem">Record a field observation</h4>
    <form class="card fobs"><div class="form">
      <label class="f">District<input name="district" value="${esc(event?.district || "")}" required></label>
      <label class="f">Latitude<input name="latitude" type="number" step="0.00001" min="29" max="37.5" value="34.0" required></label>
      <label class="f">Longitude<input name="longitude" type="number" step="0.00001" min="69" max="74.5" value="71.5" required></label>
      <label class="f">Observation date<input name="observed_on" type="date" max="${today}" value="${today}" required></label>
      <label class="f">Flooding observed?${sel("flooding_observed", FLOODING)}</label>
      <label class="f">Crop in field${sel("crop", CROPS)}</label>
      <label class="f">Damage observed${sel("damage_observed", DAMAGE)}</label>
      <label class="f">Observer<input name="observer"></label></div>
      <label class="f" style="margin-top:.8rem">Notes<textarea name="notes"></textarea></label>
      <div class="row" style="margin-top:.8rem"><button class="btn primary" type="submit">Save observation</button>
        <span class="small muted">Tip: click the map below to fill in the coordinates.</span></div></form>
    <h4 style="margin-top:1.2rem">Recorded observations</h4><div class="map obs-map"></div><div class="obs" style="margin-top:.8rem"></div>`;

  const rows = detail?.tehsil_priorities || [];
  el.querySelector(".prio").innerHTML = detail && event ? `<p class="small muted">Event: ${esc(event.district)} · ${esc(event.status)} · peak acquisition ${esc(event.peak_date)}. Priority = High when ≥ 50 ha of flooded farmland shows a high-risk NDVI drop; Medium when ≥ 50 ha is medium/high risk or ≥ 500 ha of farmland is flooded.</p>` +
    (rows.length ? table([{ label: "Tehsil", fmt: (t) => esc(t.area) }, { label: "Priority", fmt: (t) => risk(t.priority) }, { key: "flood_ha", label: "Flooded area (ha)", num: 0 },
      { key: "flooded_cropland_ha", label: "Flooded farmland (ha)", num: 0 }, { key: "high_risk_ha", label: "High-risk farmland (ha)", num: 0 }, { key: "medium_risk_ha", label: "Medium-risk farmland (ha)", num: 0 }], rows) +
      `<p class="tiny muted">Tehsil boundaries: geoBoundaries PAK ADM3 (2017 census units, ODbL 1.0).</p>` : note("info", esc(detail.tehsil_priority_note || "No tehsil-level priorities are available for this event.")))
    : note("info", "No analyzed event is available. Open the Dashboard first.");

  const form = el.querySelector(".fobs");
  const obsMap = window.L ? L.map(el.querySelector(".obs-map"), { scrollWheelZoom: false }).setView([34.0, 71.8], 7) : null;
  let marker = null, layer = null;
  if (obsMap) {
    L.tileLayer(ESRI_IMAGERY, { attribution: "Basemap © Esri", maxZoom: 18 }).addTo(obsMap);
    obsMap.on("click", (e) => {
      form.latitude.value = e.latlng.lat.toFixed(5); form.longitude.value = e.latlng.lng.toFixed(5);
      marker ? marker.setLatLng(e.latlng) : (marker = L.marker(e.latlng).addTo(obsMap));
    });
  }
  const load = async () => {
    const obs = await api("/api/field");
    el.querySelector(".obs").innerHTML = obs.length ? table([{ label: "Date", fmt: (o) => esc(o.observed_on) }, { key: "district", label: "District" },
      { key: "latitude", label: "Lat", num: 5 }, { key: "longitude", label: "Lon", num: 5 }, { key: "flooding_observed", label: "Flooding" }, { key: "crop", label: "Crop" },
      { key: "damage_observed", label: "Damage" }, { key: "observer", label: "Observer" }, { key: "notes", label: "Notes", wrap: true }], obs, { scroll: true }) +
      `<p><a class="btn" href="/api/field.csv" download>⬇ Download observations (CSV)</a></p>` : `<p class="small muted">No field observations recorded yet.</p>`;
    if (obsMap) {
      layer && layer.remove();
      layer = L.layerGroup(obs.map((o) => L.circleMarker([o.latitude, o.longitude], { radius: 6, color: "#fff", weight: 2, fillColor: "#d7301f", fillOpacity: .9 })
        .bindTooltip(`${esc(o.observed_on)} · ${esc(o.crop)} · ${esc(o.damage_observed)}`))).addTo(obsMap);
      if (obs.length) obsMap.fitBounds(L.latLngBounds(obs.map((o) => [o.latitude, o.longitude])).pad(0.3), { maxZoom: 12 });
    }
  };
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = Object.fromEntries(new FormData(form).entries());
    ["flooding_observed", "crop", "damage_observed"].forEach((k) => { fd[k] = form.querySelector(`.${k}`).value; });
    fd.event_key = event ? `${event.district}|${event.peak_date}` : null;
    const btn = form.querySelector("button[type=submit]"); btn.disabled = true;
    try { await post("/api/field", fd); toast("Observation saved."); form.notes.value = ""; await load(); }
    catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  });
  await load();
  setTimeout(() => obsMap && obsMap.invalidateSize(), 150);
  return () => { try { obsMap && obsMap.remove(); } catch {} };
}
