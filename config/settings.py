"""Application configuration and validated defaults."""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]

class Settings(BaseSettings):
    app_name: str = "ZarailensAI"
    ee_project: str | None = None
    nominatim_user_agent: str = "ZarailensAI/1.0 (contact: configure in .env)"
    max_aoi_km2: float = 5000
    max_download_pixels: int = 2_000_000
    coverage_threshold: float = 90
    analysis_history_db: Path = ROOT / "data" / "analysis_history.sqlite3"
    boundaries_dir: Path = ROOT / "data" / "boundaries"

    # Automatic KPK flood monitoring (all values can be overridden in .env).
    monitoring_days: int = 45              # recent period searched for new flood signals
    background_days: int = 60              # earlier period used to learn each district's normal signal
    screening_scale_m: int = 200           # KPK-wide screening resolution
    event_scale_m: int = 20                # resolution for the selected event's statistics
    auto_refresh_hours: float = 12         # dashboard re-checks Earth Engine after this age
    historical_fallback_years: int = 2     # monsoon seasons searched when nothing recent is confirmed
    s1_change_db: float = -3               # VV after-minus-before change threshold
    s1_water_db: float = -15               # VV open-water threshold on the after image
    s1_vh_water_db: float = -22            # VH open-water threshold (used when VH is acquired)
    s1_smoothing_m: float = 30             # focal-median speckle filter for event-scale maps
    max_slope_deg: float = 5
    max_hand_m: float = 20                 # MERIT Hydro height above nearest drainage
    max_elevation_m: float = 2000          # excludes wet-snow false positives in high mountains
    min_event_excess_ha: float = 300       # possible-event threshold above the district background
    confirmed_event_excess_ha: float = 1000
    # Automatic district monitoring service (monitor_service.py)
    monitored_districts: list[str] = ["Lakki Marwat","Charsadda","Nowshera","Swat","Peshawar"]
    monitor_interval_hours: float = 12
    monitor_lock_minutes: float = 120      # a lock older than this is treated as abandoned
    alert_webhook_url: str | None = None   # optional: POST alerts as JSON {"text": ...} (Slack/Teams/etc.)
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

settings = Settings()
