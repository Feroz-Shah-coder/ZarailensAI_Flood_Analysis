"""Folium map construction for AOIs and optional result layers."""
import math
import folium

ESRI_IMAGERY='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
ESRI_ATTR='Basemap: Esri, Maxar, Earthstar Geographics'
SMALL_ATTRIBUTION="<style>.leaflet-control-attribution{font-size:9px!important;line-height:1.2}</style>"

def zoom_for_bounds(bounds, width_px: int = 420, height_px: int = 300, padding_px: int = 16) -> int:
    """Web-Mercator zoom that fits (min_x, min_y, max_x, max_y) into a map of the given size.

    Leaflet's fitBounds needs the map's on-screen size. Maps inside inactive
    Streamlit tabs are created with zero width, so fitBounds falls back to the
    maximum zoom and shows single radar pixels. Computing the zoom here avoids that.
    """
    min_x,min_y,max_x,max_y=bounds
    usable_w=max(width_px-2*padding_px,64); usable_h=max(height_px-2*padding_px,64)
    dx=max(max_x-min_x,1e-6)
    merc=lambda lat:math.log(math.tan(math.pi/4+math.radians(max(min(lat,85),-85))/2))
    dy=max(merc(max_y)-merc(min_y),1e-9)
    zoom_x=math.log2(usable_w*360/(256*dx))
    zoom_y=math.log2(usable_h*2*math.pi/(256*dy))
    return int(max(1,min(18,math.floor(min(zoom_x,zoom_y)))))

def framed_map(geometry, width_px: int = 420, height_px: int = 300, basemap: bool = True, **kwargs) -> folium.Map:
    """Map centred on a geometry at a zoom computed from its bounds (works inside tabs)."""
    from shapely.geometry import shape
    shp=shape(geometry); minx,miny,maxx,maxy=shp.bounds
    fmap=folium.Map(location=[(miny+maxy)/2,(minx+maxx)/2],zoom_start=zoom_for_bounds(shp.bounds,width_px,height_px),
                    tiles=None,control_scale=True,**kwargs)
    if basemap:
        folium.TileLayer(tiles=ESRI_IMAGERY,attr=ESRI_ATTR,name="Satellite basemap",overlay=False,control=False).add_to(fmap)
    fmap.get_root().header.add_child(folium.Element(SMALL_ATTRIBUTION))
    return fmap

def make_map(geometry, center=None):
    if center is None:
        from shapely.geometry import shape
        c=shape(geometry).centroid; center=[c.y,c.x]
    m=folium.Map(location=center,zoom_start=9,control_scale=True)
    folium.GeoJson(geometry,name='AOI boundary',style_function=lambda _: {'color':'#16a085','weight':3,'fillOpacity':.08}).add_to(m)
    folium.LayerControl().add_to(m)
    return m
