"""Create honest summary PDF, Excel, and GeoJSON exports."""
from pathlib import Path
from src.utils import ExportError

TABLE_KEYS=('crop_rows','candidate_events','rainfall_series')
CROP_HEADERS=[('crop','Crop'),('affected_ha','Flooded area (ha)'),('crop_area_ha','Crop area in AOI (ha)'),
              ('ndvi_change_flooded','NDVI change, flooded'),('ndvi_change_unflooded','NDVI change, unflooded'),
              ('relative_ndvi_change','Relative NDVI change'),('ndvi_valid_fraction','NDVI valid fraction'),('risk','Damage risk')]
EVENT_HEADERS=[('district','District'),('status','Status'),('confidence','Confidence'),('first_detection','First detection'),
               ('peak_date','Peak acquisition'),('peak_excess_ha','Excess new water (ha)'),('peak_ratio','× background'),
               ('rainfall_7d_mm','7-day rainfall (mm)'),('score','Score')]

def _cell(value):
    if value is None: return 'Unavailable'
    if isinstance(value,float): return round(value,3)
    return value if isinstance(value,(int,str)) else str(value)

def excel_report(summary, output):
    try:
        from openpyxl import Workbook
        wb=Workbook(); ws=wb.active; ws.title='Summary'
        ws.append(['Metric','Value'])
        for k,v in summary.items():
            if k not in TABLE_KEYS: ws.append([k,str(v)])
        for key,title,headers in (('crop_rows','Crop damage risk',CROP_HEADERS),('candidate_events','Candidate events',EVENT_HEADERS)):
            if summary.get(key):
                sheet=wb.create_sheet(title); sheet.append([h for _,h in headers])
                for row in summary[key]: sheet.append([_cell(row.get(f)) for f,_ in headers])
        if summary.get('rainfall_series'):
            sheet=wb.create_sheet('Rainfall'); sheet.append(['Date','Area-mean rainfall (mm)'])
            for row in summary['rainfall_series']: sheet.append([row.get('date'),_cell(row.get('mm'))])
        acquisitions=wb.create_sheet('Acquisitions')
        acquisitions.append(['Role','Requested time','Actual acquisition','Source','Orbit','Relative orbit'])
        for role,requested,actual in [('Before',summary.get('requested_before'),summary.get('before_acquisition')),('After',summary.get('requested_after'),summary.get('after_acquisition'))]:
            acquisitions.append([role,requested,actual,summary.get('satellite_source'),summary.get('orbit'),summary.get('relative_orbit')])
        parameters=wb.create_sheet('Parameters')
        parameters.append(['Parameter','Value'])
        for name in ('change_db','water_db','baseline_days','resolution_m','confidence'):
            if name in summary: parameters.append([name,summary[name]])
        agriculture=wb.create_sheet('Agricultural breakdown')
        agriculture.append(['Measure','Value','Source / note'])
        agriculture.append(['Mapped cropland exposed (ha)',summary.get('ag_ha'),summary.get('cropland_source','unavailable')])
        agriculture.append(['Total mapped cropland in AOI (ha)',summary.get('total_cropland_ha'),summary.get('cropland_source','unavailable')])
        agriculture.append(['Mapped cropland exposed (%)',summary.get('ag_pct'),summary.get('cropland_source','unavailable')])
        agriculture.append(['Crop type','Unavailable','WorldCover class 40 is generic cropland'])
        agriculture.append(['Confirmed crop damage','Unavailable','Requires field validation'])
        priority=wb.create_sheet('Priority areas')
        priority.append(['Location','Priority','Flooded area (ha)','Cropland exposed (ha)','Confidence','Reason'])
        priority.append([summary.get('location'),summary.get('inspection_priority'),summary.get('flood_ha'),summary.get('ag_ha'),summary.get('confidence'),'Satellite signal; field inspection required'])
        limitations=wb.create_sheet('Limitations')
        limitations.append(['Limitation'])
        for note in summary.get('caveats',[]): limitations.append([str(note)])
        wb.save(output)
    except Exception as exc: raise ExportError(f"Excel export failed: {exc}") from exc

def pdf_report(summary, output, images: dict[str,bytes] | None = None):
    try:
        from io import BytesIO
        from xml.sax.saxutils import escape
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        styles=getSampleStyleSheet()
        styles.add(ParagraphStyle(name='ReportTitle',parent=styles['Title'],textColor=colors.HexColor('#155c4a'),spaceAfter=12))
        styles.add(ParagraphStyle(name='Metric',parent=styles['BodyText'],leading=14,spaceAfter=5))
        destination=output if hasattr(output,'write') else str(output)
        document=SimpleDocTemplate(destination,pagesize=letter,rightMargin=.65*inch,leftMargin=.65*inch,topMargin=.6*inch,bottomMargin=.6*inch)
        story=[Paragraph('ZarailensAI - Satellite analysis report',styles['ReportTitle'])]
        story.append(Paragraph('Actual observations and computed results for the selected area. Satellite indicators require field validation.',styles['BodyText']))
        story.append(Spacer(1,12))
        for k,v in summary.items():
            if k in TABLE_KEYS: continue
            text=f'<b>{escape(str(k))}:</b> {escape(str(v))}'
            story.append(Paragraph(text,styles['Metric']))
        small=ParagraphStyle(name='Cell',parent=styles['BodyText'],fontSize=7.5,leading=9)
        for key,title,headers in (('crop_rows','Crop-wise flood exposure and damage risk',CROP_HEADERS),('candidate_events','Candidate flood events',EVENT_HEADERS)):
            if not summary.get(key): continue
            story.append(Spacer(1,10)); story.append(Paragraph(title,styles['Heading2']))
            data=[[Paragraph(f'<b>{escape(h)}</b>',small) for _,h in headers]]
            data+=[[Paragraph(escape(str(_cell(row.get(f)))),small) for f,_ in headers] for row in summary[key]]
            table=Table(data,repeatRows=1)
            table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e6f0eb')),
                ('GRID',(0,0),(-1,-1),.4,colors.HexColor('#b8cfc4')),('VALIGN',(0,0),(-1,-1),'TOP')]))
            story.append(table)
        for title,payload in (images or {}).items():
            story.append(Spacer(1,10))
            story.append(Paragraph(escape(str(title)),styles['Heading2']))
            preview=Image(BytesIO(payload),width=6.8*inch,height=3.7*inch)
            preview.hAlign='CENTER'; story.append(preview)
        document.build(story)
    except Exception as exc: raise ExportError(f"PDF export failed: {exc}") from exc

def geojson_report(geometry, output):
    import json
    try: Path(output).write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','properties':{},'geometry':geometry}]}))
    except Exception as exc: raise ExportError(f"GeoJSON export failed: {exc}") from exc
