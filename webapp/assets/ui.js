// UI helpers: escaping, formatting, KPI cards, tables, notes, tabs, progress, Leaflet maps, Plotly charts.
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(+v)) ? "–" : (+v).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d });
export const ha = (v) => (v === null || v === undefined) ? "—" : `${num(v)} ha`;
export const signed = (v, d = 3) => (v === null || v === undefined) ? "–" : `${v >= 0 ? "+" : ""}${(+v).toFixed(d)}`;
export function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso); if (Number.isNaN(d.getTime())) return esc(iso);
  return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
export function hoursSince(iso) { return iso ? (Date.now() - new Date(iso).getTime()) / 3.6e6 : Infinity; }
export const shortCrop = (c) => String(c).split(" (")[0].replace("Other temporary crops", "Other seasonal").replace("Wheat / winter cereals", "Wheat");

export const RISK_COLORS = { High: "#d7301f", Medium: "#fc8d59", Low: "#fdd49e", "Insufficient Data": "#9e9e9e" };
export const risk = (r) => r ? `<span class="risk r-${esc(String(r).split(" ")[0])}">${esc(r)}</span>` : "–";

export function statusPill(status) {
  const s = String(status || "");
  const cls = s.startsWith("Detected") ? "p-detected" : s.startsWith("Possible") ? "p-possible" : s.startsWith("Rejected") ? "p-grey"
    : s.includes("No flood") || s.startsWith("Small") ? "p-none" : s.includes("failed") ? "p-detected" : "p-hist";
  return `<span class="pill ${cls}">${esc(s || "–")}</span>`;
}

export function kpis(cards) {
  return `<div class="kpis">${cards.map(([cls, icon, label, value, note]) =>
    `<div class="kpi ${cls}"><div class="ic" aria-hidden="true">${icon}</div><div><p class="lb">${esc(label)}</p><p class="vl">${esc(value)}</p><p class="nt">${esc(note ?? "")}</p></div></div>`).join("")}</div>`;
}
export const note = (kind, html) => `<div class="note n-${kind}">${html}</div>`;
export const panel = (title) => `<div class="panel">${esc(title)}</div>`;

/** columns: [{key,label,num?:digits,fmt?:fn(row)->html, wrap?}] */
export function table(columns, rows, { onRowAttr, scroll = false } = {}) {
  if (!rows?.length) return "";
  const head = columns.map((c) => `<th class="${c.num !== undefined ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map((r, i) => `<tr ${onRowAttr ? onRowAttr(r, i) : ""}>${columns.map((c) => {
    const v = c.fmt ? c.fmt(r) : c.num !== undefined ? num(r[c.key], c.num) : esc(r[c.key] ?? "–");
    return `<td class="${c.num !== undefined ? "num" : ""} ${c.wrap ? "wrap" : ""}">${v}</td>`;
  }).join("")}</tr>`).join("");
  return `<div class="tbl ${scroll ? "scroll" : ""}"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

export function toast(msg, isError = false) {
  const box = document.getElementById("toast"); const d = document.createElement("div");
  d.textContent = msg; if (isError) d.className = "err"; box.appendChild(d);
  setTimeout(() => d.remove(), isError ? 9000 : 4500);
}

export function tabs(el, names, render, start = 0) {
  el.innerHTML = `<div class="tabs" role="tablist">${names.map((n, i) => `<button role="tab" data-i="${i}">${esc(n)}</button>`).join("")}</div><div class="tab-body"></div>`;
  const body = el.querySelector(".tab-body");
  const show = (i) => {
    $$(".tabs button", el).forEach((b) => b.classList.toggle("on", +b.dataset.i === i));
    body.innerHTML = ""; render(i, body);
  };
  $$(".tabs button", el).forEach((b) => b.addEventListener("click", () => show(+b.dataset.i)));
  show(start);
}

export function progress(el, title = "Working…") {
  el.innerHTML = `<div class="card"><div><span class="spin"></span> <b class="pt">${esc(title)}</b></div>
    <div class="progress"><div></div></div><div class="small muted pm"></div></div>`;
  const bar = el.querySelector(".progress>div"), msg = el.querySelector(".pm"), t = el.querySelector(".pt");
  return {
    set(f, text) { bar.style.width = `${Math.round(Math.min(Math.max(f, 0), 1) * 100)}%`; if (text) msg.textContent = text; },
    title(s) { t.textContent = s; },
    done() { el.innerHTML = ""; },
    fail(e) { el.innerHTML = note("err", `<b>Failed:</b> ${esc(e.message || e)}`); },
  };
}

/** Run fn over items with limited concurrency; returns results in order. */
export async function pool(items, limit, fn) {
  const out = new Array(items.length); let next = 0;
  const worker = async () => { while (next < items.length) { const i = next++; out[i] = await fn(items[i], i); } };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

// ------------------------------------------------------------------ maps
export const ESRI_IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
export const ESRI_GRAY = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}";
const maps = new Set();

/** layers: [{url, name, opacity}] (Earth Engine XYZ tiles). Fits the view to the geometry. */
export function makeMap(el, geometry, layers = [], { basemap = ESRI_IMAGERY, control = layers.length > 1, zoomControl = true, outline = "#ffffff" } = {}) {
  if (!window.L) { el.innerHTML = note("warn", "Map library did not load (offline?)."); return null; }
  const map = L.map(el, { zoomControl, attributionControl: true, scrollWheelZoom: false });
  L.tileLayer(basemap, { attribution: "Basemap © Esri", maxZoom: 18 }).addTo(map);
  const overlays = {};
  layers.forEach((l) => { overlays[l.name] = L.tileLayer(l.url, { opacity: l.opacity ?? 1, attribution: "Google Earth Engine", maxZoom: 18 }).addTo(map); });
  let bounds = null;
  if (geometry) {
    const g = L.geoJSON(geometry, { style: { color: outline, weight: 2.2, fillOpacity: 0 }, interactive: false }).addTo(map);
    bounds = g.getBounds();
  }
  if (bounds && bounds.isValid()) map.fitBounds(bounds, { padding: [8, 8] }); else map.setView([34.0, 71.8], 7);
  if (control) L.control.layers(null, overlays, { collapsed: true }).addTo(map);
  map.on("click", () => map.scrollWheelZoom.enable());
  maps.add(map);
  // maps created inside hidden/new containers need a size refresh
  setTimeout(() => { map.invalidateSize(); if (bounds && bounds.isValid()) map.fitBounds(bounds, { padding: [8, 8] }); }, 120);
  return map;
}
export function clearMaps() { maps.forEach((m) => { try { m.remove(); } catch {} }); maps.clear(); }
export const legend = (items) => `<div class="legend">${items.map(([t, c]) => `<div><span style="background:${c}"></span>${esc(t)}</div>`).join("")}</div>`;

export function fullMap(geometry, layers, title) {
  const wrap = document.createElement("div"); wrap.className = "full";
  wrap.innerHTML = `<div class="bar"><b>${esc(title)}</b><button class="btn">Close ✕</button></div><div class="map"></div>`;
  document.body.appendChild(wrap);
  const m = makeMap(wrap.querySelector(".map"), geometry, layers, { control: true });
  if (m) m.scrollWheelZoom.enable();
  const close = () => { try { m && m.remove(); } catch {} wrap.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  wrap.querySelector("button").addEventListener("click", close); document.addEventListener("keydown", onKey);
}

// ------------------------------------------------------------------ charts
const PLOT_CFG = { displayModeBar: false, responsive: true };
const baseLayout = (extra = {}) => ({ margin: { l: 44, r: 10, t: extra.title ? 34 : 10, b: 40 }, height: 260, paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)", font: { family: "Source Sans 3, Segoe UI, sans-serif", size: 12, color: "#17241e" },
  xaxis: { gridcolor: "#e6ece8" }, yaxis: { gridcolor: "#e6ece8", rangemode: "tozero" }, ...extra });

export function barChart(el, x, y, { color = "#2f7de1", title, yTitle, marks = [], hover } = {}) {
  if (!window.Plotly) { el.innerHTML = note("warn", "Chart library did not load."); return; }
  const shapes = marks.map((m) => ({ type: "line", x0: m.x, x1: m.x, yref: "paper", y0: 0, y1: 1, line: { dash: "dash", color: "#0f3d2e", width: 1 } }));
  const annotations = marks.map((m) => ({ x: m.x, y: 1, yref: "paper", text: m.label, showarrow: false, yanchor: "bottom", font: { size: 10 } }));
  Plotly.newPlot(el, [{ type: "bar", x, y, marker: { color }, hovertext: hover, hovertemplate: "%{x}<br>%{y:,.0f}<extra>%{hovertext}</extra>" }],
    baseLayout({ title: title ? { text: title, font: { size: 13 } } : undefined, yaxis: { title: yTitle, gridcolor: "#e6ece8", rangemode: "tozero" }, shapes, annotations }), PLOT_CFG);
}
export function donut(el, labels, values, colors, center) {
  if (!window.Plotly) { el.innerHTML = note("warn", "Chart library did not load."); return; }
  Plotly.newPlot(el, [{ type: "pie", labels, values, hole: .6, sort: false, textinfo: "percent", marker: { colors } }],
    baseLayout({ height: 290, margin: { l: 0, r: 0, t: 0, b: 0 }, showlegend: true, legend: { orientation: "h", y: -0.05, font: { size: 11 } },
      annotations: [{ text: center, showarrow: false, font: { size: 13 } }] }), PLOT_CFG);
}
