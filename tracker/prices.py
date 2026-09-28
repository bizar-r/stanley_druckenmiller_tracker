"""Daily closes from Yahoo Finance's chart endpoint (no key needed)."""
import time
from datetime import datetime, timedelta, timezone

import requests

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; druckenmiller-tracker)"})


def yahoo_symbol(ticker):
    return ticker.replace("/", "-").replace(" ", "-")


def since(ticker, start_date):
    """Return (close on/before start_date, latest close, latest date) or None."""
    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    params = {
        "period1": int((start - timedelta(days=10)).timestamp()),
        "period2": int(time.time()),
        "interval": "1d",
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol(ticker)}"
    try:
        r = _session.get(url, params=params, timeout=20)
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        stamps = result["timestamp"]
        adj = result["indicators"].get("adjclose")
        closes = adj[0]["adjclose"] if adj else result["indicators"]["quote"][0]["close"]
    except Exception:
        return None
    finally:
        time.sleep(0.2)

    points = [(datetime.fromtimestamp(t, timezone.utc).date(), c) for t, c in zip(stamps, closes) if c]
    before = [c for d, c in points if d <= start.date()]
    if not before or not points:
        return None
    return before[-1], points[-1][1], points[-1][0].isoformat()
