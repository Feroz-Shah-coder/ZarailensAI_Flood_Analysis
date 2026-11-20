// About: method, data sources and limitations (thresholds read from the server settings).
import { meta } from "../api.js";
import { esc, num, panel } from "../ui.js";

export async function render(el) {
  const p = (await meta()).params;
  el.innerHTML = `${panel("About ZarailensAI")}<div class="card" style="max-width:980px;line-height:1.6">
  <h3>How ZarailensAI works</h3>
  <p><b>A. Automatic latest-flood monitoring (Dashboard)</b></p>
  <ol>
    <li>Finds the newest Sentinel-1 IW acquisition over Khyber Pakhtunkhwa in Google Earth Engine.</li>
    <li>Lists every acquisition <i>slice</i> (date + orbit track) in the monitoring window (${esc(p.monitoring_days)} days) and in an earlier background window (${esc(p.background_days)} days).</li>
    <li>Compares each slice with the median of the same track over the previous 60 days and maps <b>new water</b>: VV change &lt; ${esc(p.change_db)} dB, VV &lt; ${esc(p.water_db)} dB and VH &lt; ${esc(p.vh_water_db)} dB, excluding JRC permanent water (≥ 90 % occurrence), slopes &gt; ${esc(p.max_slope_deg)}°, HAND &gt; ${esc(p.max_hand_m)} m and elevation &gt; ${esc(p.max_elevation_m)} m (wet snow mimics water in radar).</li>
    <li>Measures new water per district at ${esc(p.screening_scale_m)} m, learns each district's normal level from the background window (irrigation, rice paddies and river beds produce a recurring signal) and scores <b>excess</b> water.</li>
    <li>Groups anomalous acquisitions into events, adds rainfall (CHIRPS daily, or GPM IMERG while CHIRPS is not yet published) and classifies each as <b>Detected</b> (≥ ${num(p.confirmed_event_excess_ha)} ha excess, ≥ 3× normal, plus rainfall/repeat/regional corroboration) or <b>Possible</b> (≥ ${num(p.min_event_excess_ha)} ha). Strong candidates are then <b>verified independently</b>: (a) cloud-free Sentinel-2 imagery from 3 days before to 6 days after the radar pass must show open water (MNDWI &gt; 0) on the radar "water" pixels — if ≥ 30 % of them are clear and &lt; 10 % look like water, the event is <b>Rejected – not a flood</b>; (b) when it is cloudy, at least 35 % of the radar water must lie in connected patches ≥ 10 ha, because floods form sheets while field-by-field wetting forms small strips; (c) signals that are ≥ 60 % on seasonally wet ground (JRC occurrence ≥ 10 %, e.g. reservoirs) are rejected. These rules were calibrated on the real D.I. Khan flood of 29 Aug 2022 and a false alarm in Lakki Marwat on 10 Oct 2026.</li>
    <li>Shows the latest <i>detected</i> event. If there is none, it states <i>"No confirmed recent flood event detected"</i> and searches the most recent ${esc(p.historical_fallback_years)} monsoon season(s) as clearly labelled history.</li>
    <li>For the event district it retrieves the actual same-track before/after acquisitions, maps flooding at ${esc(p.event_scale_m)} m (30 m speckle filter), overlays ESA WorldCover cropland and ESA WorldCereal crop types, and compares Cloud Score+ masked Sentinel-2 NDVI before and after.</li>
  </ol>
  <p><b>B. Automatic district monitoring</b> — Lakki Marwat, Charsadda, Nowshera, Swat and Peshawar are checked on a schedule (Vercel Cron); only acquisitions newer than the last processed one are measured, and alerts are raised only for detected events.</p>
  <p><b>C. Reported flood events 2010–2026</b> — each event in the reference list is measured per district: Sentinel-1/2 from 2015, the Landsat 5/7/8 archive before.</p>
  <p><b>D. New Analysis</b> — any KPK district and period with the same method.</p>
  <h3>Damage-risk classes</h3>
  <p>Risk is assigned per crop from the <b>relative NDVI change</b> — the change on flooded fields minus the change on unflooded fields of the same crop — so normal growth or harvest does not count as damage: High ≤ −0.15, Medium ≤ −0.07, otherwise Low; <i>Insufficient Data</i> when fewer than 30 % of flooded fields have cloud-free Sentinel-2 observations.</p>
  <h3>Data sources</h3>
  <p>Copernicus Sentinel-1 GRD and Sentinel-2 L2A (ESA), Google Cloud Score+, USGS Landsat 5/7/8 Collection 2, JRC Global Surface Water v1.4, SRTM, MERIT Hydro, ESA WorldCover 2021, ESA WorldCereal 2021, CHIRPS v2, NASA GPM IMERG V07, FAO GAUL 2025 districts, geoBoundaries tehsils (ODbL), Esri World Imagery basemap. All satellite processing runs in Google Earth Engine.</p>
  <h3>Limitations</h3>
  <ul>
    <li>Satellite data are not live: Sentinel-1 revisits each track every 6–12 days and appears in Earth Engine after a delay. Short floods that recede between passes can be missed (e.g. Charsadda, late August 2022).</li>
    <li>The detection date is an acquisition date. The flood began between the <i>before</i> and <i>after</i> acquisitions.</li>
    <li>Thresholds are heuristic and unvalidated for KPK; urban areas, wet soil, flash floods in narrow valleys and vegetation-covered water are poorly detected.</li>
    <li>WorldCereal/WorldCover describe 2021; they do not show the current season's crops. Rice, sugarcane and cotton are not separated. Before 2015 only "cropland" is mapped.</li>
    <li>Risk classes are inspection priorities, not measured losses. <b>No crop-destruction percentage or financial loss is estimated</b> because no validated model or ground-truth dataset is available. Field observations recorded on the Field Verification page are the first step toward such validation.</li>
  </ul></div>`;
}
