"""Shared errors, geometry helpers, units, and logging."""
import logging
from pathlib import Path

Path("outputs").mkdir(exist_ok=True)
logging.basicConfig(filename="outputs/zarailens.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

class ZarailensError(Exception):
    """Expected, user-displayable application error."""
class EarthEngineError(ZarailensError): pass
class InvalidLocationError(ZarailensError): pass
class MissingBoundaryError(InvalidLocationError): pass
class OutsideKPKError(InvalidLocationError): pass
class InvalidDatesError(ZarailensError): pass
class NoImageryError(ZarailensError): pass
class ProcessingError(ZarailensError): pass
class ExportError(ZarailensError): pass

def hectares(area_m2: float) -> float:
    """Convert square metres to hectares."""
    return float(area_m2) / 10_000
