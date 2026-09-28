"""Daily job: pull EDGAR, map tickers, price-adjust, write docs/data.json for the dashboard."""
import json
from datetime import date, datetime, timedelta, timezone

from . import prices, sec, tickers

OUTPUT = sec.ROOT / "docs" / "data.json"


def _next_quarter_end(d):
    for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
        q = date(d.year, month, day)
        if q > d:
            return q
    return date(d.year + 1, 3, 31)


def _label(pos, ticker_map):
    info = ticker_map.get(pos["cusip"]) or {}
    return info.get("ticker")


def _change(cur, prev):
    if prev is None:
        return "new", None
    if cur["shares"] > prev["shares"]:
        kind = "add"
    elif cur["shares"] < prev["shares"]:
        kind = "reduce"
    else:
        kind = "same"
    pct = (cur["shares"] - prev["shares"]) / prev["shares"] * 100 if prev["shares"] else None
    return kind, pct


def main():
    print("Fetching EDGAR submissions...")
    entity, filings = sec.list_filings()
    print(f"  {len(filings)} filings for {entity}")

    periods = sec.build_periods(filings)
    if not periods:
        raise SystemExit("No parsable 13F information tables found")
    print(f"  {len(periods)} 13F periods: {periods[0]['period']} .. {periods[-1]['period']}")

    cusips = {p["cusip"] for period in periods for p in period["positions"].values()}
    print(f"Resolving {len(cusips)} CUSIPs...")
    ticker_map = tickers.resolve(cusips)

    latest = periods[-1]
    prev = periods[-2] if len(periods) > 1 else None
    total = sum(p["value"] for p in latest["positions"].values())

    print("Pricing latest positions...")
    positions = []
    est_total = 0
    for key, pos in latest["positions"].items():
        ticker = _label(pos, ticker_map)
        prev_pos = prev["positions"].get(key) if prev else None
        kind, pct = _change(pos, prev_pos)
        quote = prices.since(ticker, latest["period"]) if ticker else None
        price_change = (quote[1] / quote[0] - 1) if quote else None
        est_value = pos["value"] * (1 + price_change) if price_change is not None else pos["value"]
        est_total += est_value
        positions.append({
            "key": key,
            "name": pos["name"],
            "ticker": ticker,
            "cusip": pos["cusip"],
            "title_of_class": pos["title_of_class"],
            "put_call": pos["put_call"],
            "shares": pos["shares"],
            "share_type": pos["share_type"],
            "value": pos["value"],
            "weight": pos["value"] / total if total else 0,
            "change": kind,
            "share_change_pct": pct,
            "prev_shares": prev_pos["shares"] if prev_pos else None,
            "price_change_pct": price_change * 100 if price_change is not None else None,
            "price_as_of": quote[2] if quote else None,
            "est_value": est_value,
        })
    for p in positions:
        p["est_weight"] = p["est_value"] / est_total if est_total else 0
    positions.sort(key=lambda p: p["value"], reverse=True)

    exits = []
    if prev:
        for key, pos in prev["positions"].items():
            if key not in latest["positions"]:
                exits.append({
                    "key": key,
                    "name": pos["name"],
                    "ticker": _label(pos, ticker_map),
                    "put_call": pos["put_call"],
                    "prev_shares": pos["shares"],
                    "prev_value": pos["value"],
                })
        exits.sort(key=lambda p: p["prev_value"], reverse=True)

    history = []
    for period in periods:
        period_total = sum(p["value"] for p in period["positions"].values())
        top = sorted(period["positions"].values(), key=lambda p: p["value"], reverse=True)[:10]
        history.append({
            "period": period["period"],
            "filed": period["filed"],
            "total_value": period_total,
            "count": len(period["positions"]),
            "filings": period["filings"],
            "top": [{
                "label": _label(p, ticker_map) or p["name"],
                "put_call": p["put_call"],
                "weight": p["value"] / period_total if period_total else 0,
            } for p in top],
        })

    position_history = {}
    for pos in positions:
        series = []
        for period in periods:
            match = period["positions"].get(pos["key"])
            series.append({
                "period": period["period"],
                "value": match["value"] if match else 0,
                "shares": match["shares"] if match else 0,
            })
        position_history[pos["key"]] = series

    latest_period = date.fromisoformat(latest["period"])
    next_period = _next_quarter_end(latest_period)
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entity": entity,
        "cik": sec.CIK,
        "latest": {
            "period": latest["period"],
            "filed": latest["filed"],
            "filings": latest["filings"],
            "total_value": total,
            "est_total_value": est_total,
            "count": len(positions),
            "positions": positions,
            "exits": exits,
            "prev_period": prev["period"] if prev else None,
        },
        "next_13f": {
            "period": next_period.isoformat(),
            "due": (next_period + timedelta(days=45)).isoformat(),
        },
        "history": history,
        "position_history": position_history,
        "recent_filings": filings[:40],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"Wrote {OUTPUT.relative_to(sec.ROOT)} ({len(positions)} positions, period {latest['period']})")


if __name__ == "__main__":
    main()
