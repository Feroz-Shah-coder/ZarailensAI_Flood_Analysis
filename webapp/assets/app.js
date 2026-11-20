// ZarailensAI web app shell: hash router, sidebar, Earth Engine status, analyst token.
import { meta, token } from "./api.js";
import { clearMaps, esc, note } from "./ui.js";

const ROUTES = [
  { path: "", title: "Dashboard", icon: "🏠", page: "dashboard" },
  { path: "monitoring", title: "Automatic Flood Monitoring", icon: "🛰️", page: "monitoring" },
  { path: "events", title: "Flood Events 2010–2026", icon: "🌊", page: "events" },
  { path: "analysis", title: "New Analysis", icon: "▶️", page: "analysis" },
  { path: "history", title: "Analysis History", icon: "🕘", page: "history" },
  { path: "field", title: "Field Verification", icon: "📋", page: "field" },
  { path: "reports", title: "Reports", icon: "📄", page: "reports" },
  { path: "about", title: "About", icon: "ℹ️", page: "about" },
];
const nav = document.getElementById("nav"), page = document.getElementById("page");
nav.innerHTML = ROUTES.map((r) => `<a href="#/${r.path}" data-path="${r.path}"><span class="ic">${r.icon}</span>${esc(r.title)}</a>`).join("");

// mobile drawer
const scrim = document.getElementById("scrim");
const setNav = (open) => { document.body.classList.toggle("nav-open", open); scrim.hidden = !open; };
document.getElementById("menu").addEventListener("click", () => setNav(true));
scrim.addEventListener("click", () => setNav(false));

let cleanup = null, seq = 0;
async function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [path, ...rest] = hash.split("/");
  const r = ROUTES.find((x) => x.path === path) || ROUTES[0];
  const params = rest.map(decodeURIComponent);
  setNav(false);
  [...nav.children].forEach((a) => a.classList.toggle("active", a.dataset.path === r.path));
  document.title = `${params.length && r.page === "events" ? "Flood event" : r.title} · ZarailensAI`;
  if (typeof cleanup === "function") { try { cleanup(); } catch {} }
  cleanup = null; clearMaps();
  const my = ++seq;
  page.innerHTML = `<div class="skeleton"></div>`;
  try {
    const mod = await import(`./pages/${r.page === "events" && params.length ? "event_detail" : r.page}.js`);
    if (my !== seq) return;
    page.innerHTML = "";
    cleanup = await mod.render(page, params);
  } catch (e) {
    console.error(e);
    if (my === seq) page.innerHTML = note("err", `This page could not be loaded: ${esc(e.message)}`);
  }
  page.focus({ preventScroll: true }); window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
route();

// sidebar status
meta().then((m) => {
  const ee = document.getElementById("ee-status");
  ee.innerHTML = `Earth Engine project: <b>${esc(m.ee_project || "not configured")}</b><br>Storage: ${esc(m.storage)}`;
  if (m.token_required) {
    const box = document.getElementById("token-box"), input = document.getElementById("token");
    box.hidden = false; input.value = token.get();
    input.addEventListener("change", () => token.set(input.value.trim()));
  }
}).catch(() => { document.getElementById("ee-status").textContent = "Server not reachable"; });
