"""ZarailensAI: Earth Engine backed, no-fabrication flood analysis UI.

Entry point: page config, shared session state, header bar and navigation.
Run with:  streamlit run app.py
"""
import streamlit as st
from config.settings import settings

st.set_page_config(page_title="ZarailensAI",page_icon="🌾",layout="wide")

st.markdown("""<style>
.block-container{padding-top:3.4rem;padding-bottom:2rem;max-width:1500px}
.fi-header{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;
  background:linear-gradient(100deg,#0f3d2e,#17533f);color:#fff;border-radius:14px;padding:.8rem 1.3rem;margin-bottom:1rem}
.fi-brand{display:flex;align-items:center;gap:.8rem}
.fi-logo{font-size:2.1rem;line-height:1}
.fi-title{font-size:1.55rem;font-weight:700;letter-spacing:.2px;margin:0}
.fi-sub{font-size:.9rem;opacity:.85;margin:0}
.fi-badge{background:#1d6a4c;border:1px solid #2f8a63;border-radius:10px;padding:.4rem .9rem;font-weight:600;font-size:.9rem}
.fi-kpi{border-radius:12px;padding:.85rem 1rem;display:flex;gap:.8rem;align-items:center;min-height:92px;border:1px solid rgba(0,0,0,.05)}
.fi-kpi .ic{font-size:1.9rem}
.fi-kpi .lb{font-size:.82rem;color:#3b4a43;margin:0}
.fi-kpi .vl{font-size:1.45rem;font-weight:700;margin:0;color:#17241e;line-height:1.2}
.fi-kpi .nt{font-size:.72rem;color:#5b6a63;margin:0}
.k-blue{background:#e5f0fd}.k-green{background:#e4f4e8}.k-yellow{background:#fdf5dc}.k-red{background:#fde8e8}.k-violet{background:#efeafc}.k-grey{background:#eef1ef}
.fi-panel{background:#0f3d2e;color:#fff;font-weight:600;padding:.5rem .9rem;border-radius:8px;margin-bottom:.6rem}
.fi-status{border-radius:12px;padding:.8rem 1rem;border:1px solid #d5e2da;background:#fff}
.fi-pill{display:inline-block;border-radius:999px;padding:.12rem .65rem;font-size:.78rem;font-weight:600;margin-right:.35rem}
.p-detected{background:#fde2e1;color:#a61b1b}.p-possible{background:#fff1d6;color:#8a5a00}.p-none{background:#e4f4e8;color:#1f6b45}.p-hist{background:#e5f0fd;color:#1f4f99}
.fi-legend span{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:middle}
.fi-legend div{font-size:.8rem;margin:.1rem 0}
.fi-pill{margin-bottom:.3rem}
/* ---------- responsive layout ---------- */
.fi-kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.75rem;margin:.25rem 0 1rem}
.fi-kpi-grid .fi-kpi{min-width:0}.fi-kpi .vl{overflow-wrap:anywhere}
/* Tablets / small laptops: let multi-column rows wrap instead of squeezing maps and tables. */
@media (max-width:1200px){
  [data-testid="stHorizontalBlock"]{flex-wrap:wrap!important}
  [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{flex:1 1 340px!important;min-width:min(100%,340px)!important}
  .st-key-fi_compact [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{flex:1 1 200px!important;min-width:min(100%,200px)!important}
}
/* Phones */
@media (max-width:640px){
  .block-container{padding-left:.7rem!important;padding-right:.7rem!important;padding-top:3.6rem}
  .fi-header{padding:.6rem .8rem;border-radius:10px}
  .fi-logo{font-size:1.5rem}.fi-title{font-size:1.2rem}.fi-sub{font-size:.75rem}
  .fi-badge{font-size:.72rem;padding:.25rem .55rem}
  .fi-kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:.5rem}
  .fi-kpi{min-height:0;padding:.6rem .65rem;gap:.5rem}.fi-kpi .ic{font-size:1.4rem}
  .fi-kpi .vl{font-size:1.1rem}.fi-kpi .lb{font-size:.72rem}.fi-kpi .nt{display:none}
  .fi-panel{font-size:.9rem;padding:.4rem .7rem}
  [data-testid="stHorizontalBlock"]{gap:.5rem!important}
}
@media (max-width:380px){.fi-kpi-grid{grid-template-columns:1fr}}
</style>""",unsafe_allow_html=True)

st.markdown("""<div class="fi-header"><div class="fi-brand"><div class="fi-logo">🌱💧</div>
<div><p class="fi-title">ZarailensAI</p><p class="fi-sub">Agricultural Flood Impact Assessment</p></div></div>
<div class="fi-badge">FYP – Khyber Pakhtunkhwa (KPK)</div></div>""",unsafe_allow_html=True)

# Session state shared across pages (manual-analysis keys preserved from the original app).
for key,default in {"search_results":[],"selected_result":None,"aoi":None,"aoi_source":None,"aoi_name":None,
                    "area_km2":None,"candidates":{},"sentinel_gallery":None,"sentinel_gallery_key":None,
                    "gallery_layers":None,"gallery_layers_key":None,"gallery_impact":None,"report_artifacts":None,
                    "report_artifacts_key":None,"year_search_key":None,"imagery_mode":None,"event_result":None,
                    "analysis_summary":None,"monitoring_run":None,"dashboard_event":None,"dashboard_detail":None,
                    "dashboard_report":None,"historical_search":None,"prefill_district":None,"crop_assessment":None}.items():
    if key not in st.session_state: st.session_state[key]=default

page=st.navigation([
    st.Page("app_pages/dashboard.py",title="Dashboard",icon=":material/home:",default=True),
    st.Page("app_pages/monitoring.py",title="Automatic Flood Monitoring",icon=":material/satellite_alt:"),
    st.Page("app_pages/flood_events.py",title="Flood Events 2010–2026",icon=":material/flood:"),
    st.Page("app_pages/flood_event_detail.py",title="Flood event details",icon=":material/water_damage:",visibility="hidden"),
    st.Page("app_pages/new_analysis.py",title="New Analysis",icon=":material/play_arrow:"),
    st.Page("app_pages/history.py",title="Analysis History",icon=":material/history:"),
    st.Page("app_pages/field_verification.py",title="Field Verification",icon=":material/assignment_turned_in:"),
    st.Page("app_pages/reports.py",title="Reports",icon=":material/description:"),
    st.Page("app_pages/about.py",title="About",icon=":material/info:"),
],position="sidebar")
with st.sidebar:
    st.caption(f"Earth Engine project: {settings.ee_project or 'not configured'}")
page.run()
