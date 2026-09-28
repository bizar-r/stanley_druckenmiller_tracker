"""CUSIP -> ticker mapping via OpenFIGI, cached in data/cusip_tickers.json."""
import json
import os
import time

import requests

from .sec import DATA_DIR

CACHE = DATA_DIR / "cusip_tickers.json"
API_KEY = os.environ.get("OPENFIGI_API_KEY")
# Without a key OpenFIGI allows 10 jobs/request and 25 requests/minute.
BATCH = 100 if API_KEY else 10
PAUSE = 0.3 if API_KEY else 2.6


def _load():
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def _pick(data):
    for match in data:
        if match.get("exchCode") == "US":
            return match
    return data[0] if data else None


def resolve(cusips):
    cache = _load()
    missing = sorted({c for c in cusips if c and c not in cache})
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-OPENFIGI-API-KEY"] = API_KEY

    for i in range(0, len(missing), BATCH):
        chunk = missing[i:i + BATCH]
        jobs = [{"idType": "ID_CUSIP", "idValue": c} for c in chunk]
        for attempt in range(4):
            try:
                r = requests.post("https://api.openfigi.com/v3/mapping", json=jobs, headers=headers, timeout=30)
            except requests.RequestException as exc:
                print(f"  ! OpenFIGI error: {exc}")
                r = None
            if r is not None and r.status_code == 200:
                break
            time.sleep(15 * (attempt + 1))
        else:
            print("  ! OpenFIGI unavailable, leaving remaining CUSIPs unmapped for now")
            break
        for cusip, result in zip(chunk, r.json()):
            match = _pick(result.get("data") or [])
            cache[cusip] = {
                "ticker": match.get("ticker"),
                "figi_name": match.get("name"),
                "security_type": match.get("securityType2") or match.get("securityType"),
            } if match else None
        time.sleep(PAUSE)

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(dict(sorted(cache.items())), indent=1))
    return cache
