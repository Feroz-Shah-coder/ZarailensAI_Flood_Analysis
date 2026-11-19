from datetime import date
import numpy as np
import pytest
from src.date_selection import validate_dates, search_window, rank_candidates, available_sensors, compatible_sar_candidates, best_compatible_sar_pair
from src.utils import InvalidDatesError, hectares
from src.flood_detection import ThresholdChangeDetector, OpticalWaterChangeDetector
from src.impact_calculation import calculate_impact, damage_indicator, inspection_priority
from src.location_search import validate_polygon, match_admin_name
from src.report_generator import excel_report, pdf_report
from src.event_analysis import choose_peak, rule_based_narrative
from src.location_search import nominatim_search, osm_result_geometry
from src.satellite_retrieval import SensorCatalog, scale_landsat_reflectance, optical_thumbnail, visualization_bands, sentinel1_gallery_tile_layers, sentinel1_flood_search_year
from src.image_preprocessing import mndwi, ndwi
from src.utils import NoImageryError
from src.analysis_history import save_analysis, list_analyses, history_totals

def test_dates_and_window():
    validate_dates(date(2022,8,1),date(2022,8,10),date(2022,9,1))
    assert search_window(date(2022,8,1),5)==(date(2022,7,27),date(2022,8,7))
    with pytest.raises(InvalidDatesError): validate_dates(date(2022,8,2),date(2022,8,1))
    validate_dates(date(2015,1,1),date(2015,1,2),date(2015,2,1))

@pytest.mark.parametrize('day',[date(1917,1,1),date(1984,6,1),date(2001,1,1),date(2010,8,1),date(2014,9,1),date(2022,8,1)])
def test_historical_dates_validate_and_report_sensor_availability(day):
    validate_dates(day,day.replace(day=day.day)+__import__('datetime').timedelta(days=1),date(2026,11,15))
    available=available_sensors(day,6000)
    if day.year==1917: assert available=={'optical':[],'radar':[]}
    if day.year==1984: assert 'Landsat 5 TM' in available['optical']
    if day.year in (2001,2010): assert {'Landsat 5 TM','Landsat 7 ETM+'}.issubset(available['optical'])
    if day.year==2014: assert 'Landsat 8 OLI' in available['optical'] and not available['radar']
    if day.year==2022: assert 'Sentinel-1 SAR' in available['radar'] and 'Sentinel-2 MSI L2A' in available['optical']

def test_sensor_catalog_bands_and_scaling():
    assert SensorCatalog.for_date(date(2001,1,1))[0].name=='Landsat 5 TM'
    bands=[s.name for s in SensorCatalog.for_date(date(2014,9,1))]
    assert bands==['Landsat 8 OLI']
    assert 'Sentinel-1 SAR' not in [s.name for s in SensorCatalog.for_date(date(2014,9,1))]
    assert scale_landsat_reflectance(10000)==pytest.approx(.075)
    l8=SensorCatalog.for_date(date(2022,8,1))[0]
    assert (l8.red,l8.green,l8.blue,l8.swir1)==('SR_B4','SR_B3','SR_B2','SR_B6')
    assert visualization_bands(l8)==['SR_B4','SR_B3','SR_B2']
    l5=next(s for s in SensorCatalog.for_date(date(2010,8,1)) if s.name=='Landsat 5 TM')
    assert visualization_bands(l5)==['SR_B3','SR_B2','SR_B1']

def test_mndwi_and_optical_change_detector():
    green=np.array([[.3,.1],[.2,.2]]); swir=np.array([[.1,.3],[.2,.2]])
    index=mndwi(green,swir)
    assert index[0,0]==pytest.approx(.5) and index[0,1]==pytest.approx(-.5)
    assert ndwi(green,np.array([[.1,.3],[.3,.1]]))[0,0]==pytest.approx(.5)
    before=np.array([[-.5,-.5],[.4,-.2]]); after=np.array([[.5,-.5],[.6,.8]])
    result=OpticalWaterChangeDetector().detect(before,after,params={'pixel_area_m2':100})
    assert result.mask.tolist()==[[True,False],[False,True]] and result.area_ha==pytest.approx(.02)

def test_empty_optical_scene_list_never_makes_thumbnail():
    spec=SensorCatalog.for_date(date(2022,8,1))[0]
    with pytest.raises(NoImageryError): optical_thumbnail([],spec,{'type':'Polygon','coordinates':[]})

def test_rank_candidates():
    cs=[{'date':date(2022,8,2),'coverage':99,'polarizations':['VV']},{'date':date(2022,8,1),'coverage':90,'polarizations':['VV']}]
    assert rank_candidates(cs,date(2022,8,1))[0]==cs[1]

def test_sar_pairing_excludes_orbit_mismatches():
    before=[{'id':'b1','orbit':'DESCENDING','relative_orbit':5},{'id':'b2','orbit':'ASCENDING','relative_orbit':173}]
    after=[{'id':'a1','orbit':'ASCENDING','relative_orbit':173}]
    b,a=compatible_sar_candidates(before,after)
    assert [s['id'] for s in b]==['b2'] and [s['id'] for s in a]==['a1']
    assert compatible_sar_candidates([before[0]],[after[0]])==([],[])

def test_best_sar_pair_is_chronological_closest_and_meets_coverage():
    before=[{'id':'b-far','date':date(2022,8,1),'orbit':'ASCENDING','relative_orbit':8,'coverage':99},
            {'id':'b-near','date':date(2022,8,19),'orbit':'DESCENDING','relative_orbit':55,'coverage':92}]
    after=[{'id':'a-near','date':date(2022,9,2),'orbit':'DESCENDING','relative_orbit':55,'coverage':93},
           {'id':'a-incompatible','date':date(2022,9,3),'orbit':'ASCENDING','relative_orbit':9,'coverage':99}]
    pair=best_compatible_sar_pair(before,after,date(2022,8,20),date(2022,9,5),90)
    assert pair and (pair[0]['id'],pair[1]['id'])==('b-near','a-near')
    assert best_compatible_sar_pair(before,after,date(2022,8,20),date(2022,9,5),95) is None

def test_sentinel_gallery_uses_three_earth_engine_xyz_layers(monkeypatch):
    import src.satellite_retrieval as retrieval
    from types import SimpleNamespace
    operations=[]
    class FakeImage:
        def __init__(self,label): self.label=label
        def select(self,band): operations.append(('select',self.label,band)); return self
        def clip(self,region): return self
        def subtract(self,other): operations.append(('subtract',self.label,other.label)); return FakeImage('change')
        def lt(self,value): operations.append(('lt',self.label,value)); return FakeImage(f'lt{value}')
        def gte(self,value): operations.append(('gte',self.label,value)); return FakeImage(f'gte{value}')
        def And(self,other): operations.append(('and',self.label,other.label)); return FakeImage('and')
        def Not(self): return FakeImage('not')
        def unmask(self,value): operations.append(('unmask',self.label,value)); return self
        def selfMask(self): return self
        def multiply(self,value): return self
        def add(self,value): return FakeImage('flood-one')
        def updateMask(self,mask): operations.append(('mask',self.label,mask.label)); return self
        def mask(self): return FakeImage(f'valid-{self.label}')
        def rename(self,name): return self
        def getMapId(self,visual):
            operations.append(('visual',self.label,visual))
            index=sum(op[0]=='visual' for op in operations)
            return {'tile_fetcher':SimpleNamespace(url_format=f'https://earthengine.googleapis.com/map/live-{index}/{{z}}/{{x}}/{{y}}?token=real')}
    class ImageFactory:
        def __call__(self,value): return FakeImage(str(value))
    class FakeEE:
        Image=ImageFactory()
        Terrain=SimpleNamespace(slope=lambda image:FakeImage('slope'))
        Geometry=staticmethod(lambda geometry:geometry)
    monkeypatch.setattr(retrieval,'initialize',lambda project:FakeEE())
    before={'id':'COPERNICUS/S1_GRD/before','date':date(2022,8,1),'orbit':'ASCENDING','relative_orbit':5}
    after={'id':'COPERNICUS/S1_GRD/after','date':date(2022,8,13),'orbit':'ASCENDING','relative_orbit':5}
    result=sentinel1_gallery_tile_layers(before,after,{'type':'Polygon','coordinates':[]},project='unit-test')
    assert all(result[key].startswith('https://earthengine.googleapis.com/map/live-') and '{z}/{x}/{y}' in result[key] for key in ('before','after','flood'))
    assert len({result[key] for key in ('before','after','flood')})==3
    assert ('lt','change',-3) in operations and ('lt',after['id'],-15) in operations
    assert result['source']=='Copernicus Sentinel-1 GRD via Google Earth Engine'
    with pytest.raises(Exception,match='share orbit direction'):
        sentinel1_gallery_tile_layers(before,{**after,'relative_orbit':6},{'type':'Polygon','coordinates':[]},project='unit-test')

def test_yearly_sentinel_search_rejects_year_without_baseline_archive():
    with pytest.raises(NoImageryError,match='start in 2015'):
        sentinel1_flood_search_year({'type':'Polygon','coordinates':[]},2014,project='unit-test')

def test_flood_and_impact():
    b=np.full((2,2),-10.); a=np.array([[-18.,-12.],[-20.,-17.]])
    r=ThresholdChangeDetector().detect(b,a,params={'pixel_area_m2':100})
    assert r.mask.tolist()==[[True,False],[True,True]] and r.area_ha==.03
    impact=calculate_impact(r.mask,np.array([[1,0],[1,0]],bool))
    assert impact['Agricultural land exposed to flooding']==.02
    assert damage_indicator(np.array([[.8]]),np.array([[.4]]),np.array([[1]],bool))=='severe drop'
    assert inspection_priority(900,80,.3,.9)=='High'
    assert hectares(20000)==2

def test_polygon_validation():
    province={'type':'Polygon','coordinates':[[[70,33],[72,33],[72,35],[70,35],[70,33]]]}
    inside={'type':'Polygon','coordinates':[[[70.5,33.5],[71,33.5],[71,34],[70.5,34],[70.5,33.5]]]}
    assert validate_polygon(inside,province)<5000
    outside={'type':'Polygon','coordinates':[[[75,33],[76,33],[76,34],[75,34],[75,33]]]}
    with pytest.raises(Exception): validate_polygon(outside,province)

@pytest.mark.parametrize('query,expected',[('Khyber','Khyber'),('Kurram','Kurram'),('Bajaur','Bajaur')])
def test_merged_district_name_matching(query,expected):
    assert match_admin_name(query,['Khyber','Kurram','Bajaur','Peshawar'])==expected
    assert match_admin_name('NWFP',['North-West Frontier','N.W.F.P.','Khyber Pakhtunkhwa'])=='N.W.F.P.'

def test_reports(tmp_path):
    summary={'Location':'Example','Detected flooded area':2.5}
    x=tmp_path/'out.xlsx'; p=tmp_path/'out.pdf'
    excel_report(summary,x); pdf_report(summary,p)
    assert x.stat().st_size and p.stat().st_size

def test_analysis_history_is_persistent_idempotent_and_uses_actual_values(tmp_path):
    db=tmp_path/'history.sqlite3'
    record={'location':'Example district','geometry':{'type':'Polygon','coordinates':[]},
        'requested_before':'2022-08-01','requested_after':'2022-08-15',
        'before':{'date':'2022-07-29','time_utc':'2022-07-29T12:00:00+00:00'},
        'after':{'date':'2022-08-13','time_utc':'2022-08-13T12:00:00+00:00'},
        'summary':{'flood_ha':12.5,'ag_ha':4.25,'sensor':'Sentinel-1 SAR','confidence':'Medium'},
        'event':{'year':2022,'peak_date':'2022-08-13'}}
    first=save_analysis(record,db); second=save_analysis(record,db)
    rows=list_analyses(path=db); totals=history_totals(db)
    assert first==second and len(rows)==1
    assert rows[0]['acquired_after'].startswith('2022-08-13')
    assert rows[0]['geometry']==record['geometry']
    assert totals=={'runs':1,'flood_ha':12.5,'agricultural_ha':4.25}

def test_event_peak_and_no_signal():
    rows=[{'date':date(2022,8,1),'flood_ha':3},{'date':date(2022,8,5),'flood_ha':20},{'date':date(2022,8,7),'flood_ha':8}]
    assert choose_peak(rows)['date']==date(2022,8,5)
    assert choose_peak([{'flood_ha':.3}]) is None

def test_narrative_only_uses_supplied_values():
    text=rule_based_narrative({'location':'Test AOI','peak_date':'2022-08-05','flood_ha':12.5,'aoi_percent':2.5,'ag_ha':None,'confidence':'Low','caveats':['orbit compatible']})
    assert '12.50 ha' in text and '2022-08-05' in text and 'unavailable' in text.lower()
    assert '999' not in text and 'destroyed' in text

def test_nominatim_mock_and_point_not_boundary():
    class Response:
        status_code=200
        def raise_for_status(self): pass
        def json(self): return [{'display_name':'Peshawar, Pakistan','type':'city','lat':'34.0','lon':'71.5','geojson':{'type':'Point','coordinates':[71.5,34.0]}}]
    class Session:
        def get(self,*args,**kwargs):
            assert kwargs['params']['polygon_geojson']==1
            assert kwargs['headers']['User-Agent']=='Zarailens test contact@example.org'
            return Response()
    # isolate cache to keep this deterministic
    import src.location_search as locations
    locations._cache.pop('peshawar unit test',None)
    results=nominatim_search('Peshawar unit test','Zarailens test contact@example.org',Session())
    assert results[0]['type']=='city' and osm_result_geometry(results[0]) is None

@pytest.mark.integration
@pytest.mark.parametrize('place',['Peshawar','Charsadda','Swat','Nowshera'])
def test_live_location_search(place):
    """Live EE administrative boundary search."""
    from config.settings import settings
    from src.ee_auth import initialize
    from src.location_search import gaul_boundary
    initialize(settings.ee_project)
    assert gaul_boundary(place,2,settings.ee_project)

@pytest.mark.integration
def test_live_kpk_village_search():
    """Live OSM polygon test; skipped when OSM or this network blocks requests."""
    from config.settings import settings
    try: results=nominatim_search('Madyan, Swat, Pakistan',settings.nominatim_user_agent)
    except Exception as exc: pytest.skip(f"Nominatim unavailable on this network: {exc}")
    polygons=[osm_result_geometry(row) for row in results]
    assert any(polygons),"Nominatim returned no village boundary polygon; point results are not accepted as boundaries"
