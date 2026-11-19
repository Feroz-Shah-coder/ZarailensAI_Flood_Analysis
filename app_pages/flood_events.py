"""Flood Events: reported KPK floods 2010–2026 as cards; click a card for the satellite details."""
import streamlit as st
from config.settings import settings
from src import analysis_history as store
from src.flood_catalog import load_catalog, era_label, headline

catalog=load_catalog()
cache=store.catalog_analyses(path=settings.analysis_history_db)

st.markdown('<div class="fi-panel">Reported flood events in Khyber Pakhtunkhwa, 2010–2026</div>',unsafe_allow_html=True)
st.caption(catalog["source"]+" Open an event to measure it with real satellite data: underwater area, farmland, crop damage risk, "
           "before/after images and the acquisition timeline.")

def card(event: dict) -> None:
    with st.container(border=True):
        top=st.columns([1,3],vertical_alignment="center")
        top[0].markdown(f"### {event['year']}")
        top[1].markdown(f"**{event['event']}**  \n:gray[{event['period']}]")
        st.markdown(f":material/location_on: {event['areas']}")
        badges=[f":blue-badge[{'Sentinel-1/2' if event['era']=='sentinel' else 'Landsat'}]"]
        if "forecast" in event["event"].lower() or "not established" in event["event"].lower():
            badges.append(":orange-badge[Not confirmed in report]")
        if "GLOF" in event["event"]: badges.append(":violet-badge[GLOF]")
        analysed=[r for (eid,_),r in cache.items() if eid==event["id"]]
        badges.append(f":green-badge[{len(analysed)} district(s) analysed]" if analysed else ":gray-badge[Not analysed yet]")
        st.markdown(" ".join(badges))
        for r in analysed[:3]:
            h=headline(r)
            if h:
                st.caption(f"**{r['district']}**: {h['flood_ha']:,.0f} ha underwater · {h['farmland_ha']:,.0f} ha farmland · risk {h['risk']} · image {h['after']}")
            else:
                st.caption(f"**{r['district']}**: {r.get('status')}")
        if st.button("Open details",key=f"open_{event['id']}",icon=":material/open_in_new:",width="stretch"):
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
