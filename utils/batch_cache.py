import datetime
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = PROJECT_ROOT / "cache"


def ensure_cache_dir():
    CACHE_DIR.mkdir(exist_ok=True)


def _cache_file(name: str) -> Path:
    ensure_cache_dir()
    return CACHE_DIR / f"{name}.json"


def load_cache(name: str):
    path = _cache_file(name)
    if not path.exists():
        return {"last_flush": None, "entries": []}

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {"last_flush": None, "entries": []}


def save_cache(name: str, cache_data):
    path = _cache_file(name)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(cache_data, handle, indent=2)


def append_cache_entry(name: str, entry):
    cache_data = load_cache(name)
    cache_data.setdefault("entries", []).append(entry)
    save_cache(name, cache_data)
    return cache_data


def should_flush(name: str, interval_hours: int = 6) -> bool:
    cache_data = load_cache(name)
    last_flush = cache_data.get("last_flush")
    if not last_flush:
        return True

    try:
        last_flush_dt = datetime.datetime.fromisoformat(last_flush)
    except ValueError:
        return True

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
    return (now - last_flush_dt).total_seconds() >= (interval_hours * 60 * 60)


def mark_flushed(name: str, flush_time=None):
    cache_data = load_cache(name)
    now = flush_time or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
    cache_data["last_flush"] = now.isoformat()
    cache_data["entries"] = []
    save_cache(name, cache_data)
    return cache_data
