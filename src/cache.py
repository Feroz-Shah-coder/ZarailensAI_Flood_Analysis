"""Small disk cache helpers for serialized analysis metadata."""
import hashlib, json
from pathlib import Path

def cache_key(payload) -> str: return hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()
def cache_json(key, value=None):
    folder=Path('data/processed/cache'); folder.mkdir(parents=True,exist_ok=True); path=folder/f'{key}.json'
    if value is None: return json.loads(path.read_text()) if path.exists() else None
    path.write_text(json.dumps(value,default=str)); return value
