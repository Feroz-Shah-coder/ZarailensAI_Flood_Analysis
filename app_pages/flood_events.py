"""Flood Events: reported KPK floods 2010–2026 as cards; click a card for the satellite details."""
import streamlit as st
from config.settings import settings
from src import analysis_history as store
from src.flood_catalog import load_catalog, era_label, headline, analysis_districts, thumbnail_path

catalog=load_catalog()
cache=store.catalog_analyses(path=settings.analysis_history_db)

st.markdown('<div class="fi-panel">Reported flood events in Khyber Pakhtunkhwa, 2010–2026</div>',unsafe_allow_html=True)
st.caption(catalog["source"]+" Open an event to measure it with real satellite data: underwater area, farmland, crop damage risk, "
           "before/after images and the acquisition timeline.")

def best_result(event: dict) -> dict | None:
    """Analysed district with the largest measured underwater area (falls back to any analysed one)."""
    done=[r for (eid,_),r in cache.items() if eid==event["id"]]
    return max(done,key=lambda r:(headline(r) or {}).get("flood_ha",-1),default=None)

def card(event: dict) -> None:
    analysed=[r for (eid,_),r in cache.items() if eid==event["id"]]
    best=best_result(event)
    with st.container(border=True,key=f"fcard_{event['id'].replace('-','_')}"):
        thumb=thumbnail_path(event["id"],best["district"]) if best else None
        if thumb and thumb.exists():
            st.image(str(thumb),width="stretch")
            h=headline(best)
            st.caption(f":material/satellite_alt: {best['district']} · "+(f"{h['after']} · blue = underwater area" if h else "satellite view, no flood signal"))
        else:
            st.markdown('<div class="fi-thumb-empty">🛰️<br>Satellite picture appears after analysis</div>',unsafe_allow_html=True)
        st.markdown(f"#### {event['year']} · {event['event']}")
        st.markdown(f":gray[{event['period']}]  \n:material/location_on: {event['areas']}")
        badges=[f":blue-badge[{'Sentinel-1/2' if event['era']=='sentinel' else 'Landsat'}]"]
        if "forecast" in event["event"].lower() or "not established" in event["event"].lower():
            badges.append(":orange-badge[Not confirmed in report]")
        if "GLOF" in event["event"]: badges.append(":violet-badge[GLOF]")
        total=len(analysis_districts(event))
        badges.append(f":green-badge[{len(analysed)}/{total} districts analysed]" if analysed else ":gray-badge[Not analysed yet]")
        st.markdown(" ".join(badges))
        measured=sorted([(r,headline(r)) for r in analysed if headline(r)],key=lambda x:-x[1]["flood_ha"])
        if measured:
            st.markdown(f"**{sum(h['flood_ha'] for _,h in measured):,.0f} ha** underwater · "
                        f"**{sum(h['farmland_ha'] for _,h in measured):,.0f} ha** farmland flooded  \n"
                        f":gray[sum over {len(measured)} district(s) with a measured flood]")
            for r,h in measured[:3]:
                st.caption(f"**{r['district']}**: {h['flood_ha']:,.0f} ha underwater · {h['farmland_ha']:,.0f} ha farmland · risk {h['risk'] or '—'}")
        elif analysed:
            st.caption("Satellite result: "+", ".join(sorted({r.get('status','') for r in analysed})))
        if st.button("Open details",key=f"open_{event['id']}",icon=":material/open_in_new:",width="stretch",type="primary"):
            st.session_state.catalog_event_id=event["id"]
            st.switch_page("app_pages/flood_event_detail.py")

groups={"2010–2014 (Landsat era)":lambda e:e["year"]<=2014,"2015–2019":lambda e:2015<=e["year"]<=2019,
        "2020–2023":lambda e:2020<=e["year"]<=2023,"2024–2026":lambda e:e["year"]>=2024}
for tab,(name,rule) in zip(st.tabs(list(groups)),groups.items()):
    with tab:
        events=[e for e in catalog["events"] if rule(e)]
        cols=st.columns(3)
        for i,event in enumerate(events):
            with cols[i%3]: card(event)
        if name.startswith("2010"):
            st.caption("Sentinel-1 radar starts in October 2014 and Sentinel-2 in mid-2015, so these events use the Landsat 5/7/8 optical archive "
                       "(16-day revisit; clouds often hide monsoon floods; Landsat 7 has striped gaps after 2003).")
