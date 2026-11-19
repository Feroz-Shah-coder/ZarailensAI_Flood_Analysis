"""Earth Engine initialization with actionable setup errors.

Credentials are looked up in this order:
1. ``EE_SERVICE_ACCOUNT_JSON`` (environment variable or Streamlit secret) – the
   full JSON key of a Google Cloud service account registered for Earth Engine.
   Used by online deployments (Streamlit Community Cloud, Docker/Cloud Run …).
2. The local user login created by ``earthengine authenticate`` (desktop use).
``EE_PROJECT`` may likewise come from the environment, ``.env`` or Streamlit secrets.
"""
import json
import os
from functools import lru_cache
from src.utils import EarthEngineError

def _secret(name: str) -> str | None:
    """Environment variable first, then Streamlit secrets (hosted deployments)."""
    value=os.getenv(name)
    if value: return value
    try:
        import streamlit as st
        if name in st.secrets:
            secret=st.secrets[name]
            # A TOML table is returned as a mapping; a quoted string as str.
            return json.dumps(dict(secret)) if hasattr(secret,"keys") else str(secret)
    except Exception:
        pass
    return None

def service_account_credentials(ee, key_json: str):
    """Build credentials from a downloaded service-account key (JSON text)."""
    info=json.loads(key_json)
    email=info.get("client_email")
    if not email or not info.get("private_key"):
        raise EarthEngineError("EE_SERVICE_ACCOUNT_JSON must be the complete service-account key JSON "
                               "(with client_email and private_key).")
    return ee.ServiceAccountCredentials(email,key_data=key_json)

@lru_cache(maxsize=1)
def initialize(project: str | None = None):
    project = project or _secret('EE_PROJECT')
    try:
        import ee
        key = _secret('EE_SERVICE_ACCOUNT_JSON')
        if key and key.strip() not in ("","{}"):
            ee.Initialize(service_account_credentials(ee,key), project=project)
        else:
            ee.Initialize(project=project)
        # Retry transient HTTP 429 "Too many concurrent aggregations" with exponential back-off
        # (the project's concurrent-request quota is shared by every open browser tab and run).
        ee.data.setMaxRetries(8)
        return ee
    except Exception as exc:
        raise EarthEngineError("Earth Engine is not configured. Set EE_PROJECT, enable the Earth Engine API, then run `earthengine authenticate`; for deployment configure EE_SERVICE_ACCOUNT_JSON. " + str(exc)) from exc
