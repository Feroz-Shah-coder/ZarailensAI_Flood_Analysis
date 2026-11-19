"""Live historical Earth Engine smoke checks. Run with pytest -m integration."""
from datetime import date
import pytest

@pytest.fixture(scope="module")
def charsadda_geometry():
    from config.settings import settings
    from src.ee_auth import initialize
    from src.location_search import gaul_boundary
    initialize(settings.ee_project)
    boundary=gaul_boundary("Charsadda",2,settings.ee_project)
    assert boundary, "Could not load the real Charsadda GAUL boundary"
    return boundary["geometry"]

@pytest.mark.integration
def test_charsadda_2010_landsat_scenes(charsadda_geometry):
    from config.settings import settings
    from src.satellite_retrieval import search_historical_candidates,optical_thumbnail
    result=search_historical_candidates(charsadda_geometry,date(2010,8,5),10,1000,settings.ee_project)
    assert any(result.get(name) for name in ("Landsat 5 TM","Landsat 7 ETM+"))
    scenes=result.get("Landsat 5 TM",[])
    if scenes:
        png=optical_thumbnail([scenes[0]["id"]],scenes[0]["spec"],charsadda_geometry,dimensions=256,project=settings.ee_project)
        assert png.startswith(b"\x89PNG")

@pytest.mark.integration
def test_charsadda_september_2014_landsat8_not_sentinel1(charsadda_geometry):
    from config.settings import settings
    from src.satellite_retrieval import SensorCatalog,search_historical_candidates
    day=date(2014,9,15)
    assert "Sentinel-1 SAR" not in [s.name for s in SensorCatalog.for_date(day)]
    result=search_historical_candidates(charsadda_geometry,day,14,1000,settings.ee_project)
    assert result.get("Landsat 8 OLI")

@pytest.mark.integration
def test_charsadda_2022_landsat_and_sentinel_scenes(charsadda_geometry):
    from config.settings import settings
    from src.satellite_retrieval import search_historical_candidates,sentinel1_candidates,optical_thumbnail
    day=date(2022,8,28)
    result=search_historical_candidates(charsadda_geometry,day,10,1000,settings.ee_project)
    assert result.get("Landsat 8 OLI") or result.get("Landsat 9 OLI-2")
    assert result.get("Sentinel-2 MSI L2A") or result.get("Sentinel-2 MSI L1C")
    optical=result.get("Sentinel-2 MSI L2A") or result.get("Sentinel-2 MSI L1C")
    if optical:
        png=optical_thumbnail([optical[0]["id"]],optical[0]["spec"],charsadda_geometry,dimensions=256,project=settings.ee_project)
        assert png.startswith(b"\x89PNG")
    radar=sentinel1_candidates(charsadda_geometry,date(2022,8,20),date(2022,9,6),settings.ee_project)
    assert radar and all(scene["date"] for scene in radar)

@pytest.mark.integration
def test_live_sentinel1_gallery_tiles_and_flood_visualization(charsadda_geometry):
    import math
    import requests
    from datetime import timedelta
    from config.settings import settings
    from src.date_selection import best_compatible_sar_pair
    from src.satellite_retrieval import sentinel1_candidates,sentinel1_gallery_tile_layers
    from shapely.geometry import shape
    before_day=date(2022,8,20); after_day=date(2022,9,5); window=15
    before=sentinel1_candidates(charsadda_geometry,before_day-timedelta(days=window),before_day+timedelta(days=window+1),settings.ee_project)
    after=sentinel1_candidates(charsadda_geometry,after_day-timedelta(days=window),after_day+timedelta(days=window+1),settings.ee_project)
    pair=best_compatible_sar_pair(before,after,before_day,after_day,minimum_coverage=50)
    assert pair, 'Earth Engine returned no chronological same-orbit Sentinel-1 pair for Charsadda'
    layers=sentinel1_gallery_tile_layers(pair[0],pair[1],charsadda_geometry,settings.ee_project)
    assert all(layers[name].startswith('https://earthengine.googleapis.com/') and '{z}/{x}/{y}' in layers[name] for name in ('before','after','flood'))
    assert len({layers[name] for name in ('before','after','flood')})==3
    center=shape(charsadda_geometry).centroid; zoom=9; scale=2**zoom
    x=int((center.x+180)/360*scale)
    latitude=math.radians(center.y)
    y=int((1-math.asinh(math.tan(latitude))/math.pi)/2*scale)
    for name in ('before','after','flood'):
        response=requests.get(layers[name].format(z=zoom,x=x,y=y),timeout=60)
        response.raise_for_status()
        assert response.headers.get('content-type','').startswith('image/'), f'{name} Earth Engine tile was not an image'
        assert response.content, f'{name} Earth Engine tile was empty'

@pytest.mark.integration
def test_live_sentinel1_full_year_scan_returns_observed_dates(charsadda_geometry):
    from config.settings import settings
    from src.satellite_retrieval import sentinel1_flood_search_year
    result=sentinel1_flood_search_year(charsadda_geometry,2022,project=settings.ee_project)
    assert result['year']==2022 and result['rows']
    assert all(row['date'].year==2022 for row in result['rows'])
    if result['peak']:
        assert result['peak']['date'].year==2022 and result['peak']['flood_ha']>=1
    else:
        assert 'No flood detected in 2022' in result['message']

@pytest.mark.integration
def test_live_sentinel1_worldcover_exposure_and_layer_tiles(charsadda_geometry):
    from datetime import timedelta
    from config.settings import settings
    from src.date_selection import best_compatible_sar_pair
    from src.satellite_retrieval import sentinel1_candidates,sentinel1_pair_agricultural_impact,sentinel1_pair_report_thumbnails
    before_day=date(2022,4,26); after_day=date(2022,5,8)
    candidates=sentinel1_candidates(charsadda_geometry,before_day-timedelta(days=5),after_day+timedelta(days=2),settings.ee_project)
    before=[scene for scene in candidates if scene['date']<after_day]
    after=[scene for scene in candidates if scene['date']==after_day]
    pair=best_compatible_sar_pair(before,after,before_day,after_day,minimum_coverage=50)
    assert pair, 'Expected a real same-track Sentinel-1 pair in the test interval'
    result=sentinel1_pair_agricultural_impact(pair[0],pair[1],charsadda_geometry,settings.ee_project)
    assert result['flood_ha']>=0 and result['total_cropland_ha']>0
    assert 0<=result['exposed_cropland_ha']<=result['total_cropland_ha']
    assert result['cropland_source'].startswith('ESA WorldCover 2021')
    assert all(result['layers'][key].startswith('https://earthengine.googleapis.com/') for key in ('cropland','exposed_cropland'))
    previews=sentinel1_pair_report_thumbnails(pair[0],pair[1],charsadda_geometry,project=settings.ee_project)
    assert set(previews)=={'Before','After','Flood detection'}
    assert all(preview.startswith(b'\x89PNG') for preview in previews.values())

@pytest.mark.integration
def test_charsadda_2001_landsat_or_no_scene(charsadda_geometry):
    from config.settings import settings
    from src.satellite_retrieval import search_historical_candidates
    result=search_historical_candidates(charsadda_geometry,date(2001,8,1),10,1000,settings.ee_project)
    assert any(result.get(name) for name in ("Landsat 5 TM","Landsat 7 ETM+")) or not any(result.values())

@pytest.mark.integration
def test_kpk_screening_recovers_august_2022_di_khan_flood():
    """Live check: the automatic method flags the documented late-August 2022 D.I. Khan flood."""
    from config.settings import settings
    from src.flood_monitoring import discover_events
    result=discover_events(date(2022,8,10),date(2022,9,15),settings,settings.ee_project)
    di_khan=[e for e in result["events"] if e["district"]=="D. I. Khan"]
    assert result["slices"]>0 and di_khan
    assert any(str(e["peak_date"]).startswith("2022-08-2") and e["status"]=="Detected flood event" for e in di_khan)

@pytest.mark.integration
def test_district_monitor_no_flood_season_and_duplicate_run(tmp_path):
    """Live: dry-season check creates no detected event or alert; a second check reprocesses nothing."""
    from config.settings import settings
    from src.district_monitor import run_monitor_check
    from src import analysis_history as store
    cfg=settings.model_copy(update={"monitored_districts":["Peshawar"]})
    db=tmp_path/"monitor.sqlite3"
    first=run_monitor_check(cfg,settings.ee_project,trigger="test",path=db,today=date(2026,3,15))
    assert first["status"]=="success" and first["new_slices"]>0
    assert not first["alerts"]
    assert all(e["event_status"]!="Detected flood event" for e in store.list_flood_events(path=db))
    second=run_monitor_check(cfg,settings.ee_project,trigger="test",path=db,today=date(2026,3,15))
    assert second["new_slices"]==0 and second["events_created"]==0
