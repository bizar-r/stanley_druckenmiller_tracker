"""SEC EDGAR collection for Duquesne Family Office (Stanley Druckenmiller)."""
import json
import os
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import requests

CIK = 1536411
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FILINGS_DIR = DATA_DIR / "filings"

# SEC wants a "Name email" contact in the User-Agent and also refuses some email
# domains outright (users.noreply.github.com among them); gmail.com is accepted.
# Set it via the SEC_USER_AGENT secret, e.g. "Your Name you@gmail.com".
USER_AGENT = os.environ.get("SEC_USER_AGENT") or "Druckenmiller Tracker"

FORMS_13F = ("13F-HR", "13F-HR/A")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})


def _get(url):
    for attempt in range(5):
        r = _session.get(url, timeout=30)
        if r.status_code == 200:
            time.sleep(0.15)  # stay well under SEC's 10 req/s limit
            return r
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
            time.sleep(2 ** attempt)
            continue
        break
    if r.status_code == 403 and "Undeclared Automated Tool" in r.text:
        raise SystemExit(
            "SEC rejected the User-Agent. Set the SEC_USER_AGENT secret to "
            '"Your Name you@gmail.com"; SEC refuses some email domains.'
        )
    r.raise_for_status()


def folder_url(accession):
    return f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accession.replace('-', '')}/"


def index_url(accession):
    return folder_url(accession) + f"{accession}-index.htm"


def list_filings():
    """Return (entity name, every filing in the EDGAR submissions feed)."""
    sub = _get(f"https://data.sec.gov/submissions/CIK{CIK:010d}.json").json()
    tables = [sub["filings"]["recent"]]
    for extra in sub["filings"].get("files", []):
        tables.append(_get(f"https://data.sec.gov/submissions/{extra['name']}").json())

    filings = []
    for t in tables:
        for i, acc in enumerate(t["accessionNumber"]):
            filings.append({
                "accession": acc,
                "form": t["form"][i],
                "filed": t["filingDate"][i],
                "period": t["reportDate"][i] or None,
                "url": index_url(acc),
            })
    filings.sort(key=lambda f: (f["filed"], f["accession"]), reverse=True)
    return sub.get("name", "Duquesne Family Office LLC"), filings


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    for child in el.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text(el, name):
    found = _find(el, name)
    return found.text.strip() if found is not None and found.text else None


def _num(value):
    return int(float(value.replace(",", ""))) if value else 0


def _normalize_units(holdings):
    """Scale thousands-denominated reports to dollars; idempotent.

    SEC switched 13F values from thousands to dollars in 2023, but some filers
    (Duquesne included) kept reporting thousands, so infer the unit from the
    median value per share instead of the filing date.
    """
    per_share = sorted(h["value"] / h["shares"] for h in holdings if h["share_type"] == "SH" and h["shares"])
    if per_share and per_share[len(per_share) // 2] < 1:
        for h in holdings:
            h["value"] *= 1000
    return holdings


def _parse_info_table(root):
    rows = []
    for item in root:
        if _local(item.tag) != "infoTable":
            continue
        rows.append({
            "name": _text(item, "nameOfIssuer"),
            "title_of_class": _text(item, "titleOfClass"),
            "cusip": (_text(item, "cusip") or "").upper(),
            "value": _num(_text(item, "value")),
            "shares": _num(_text(item, "sshPrnamt")),
            "share_type": _text(item, "sshPrnamtType"),
            "put_call": (_text(item, "putCall") or "").upper() or None,
        })
    return rows


def load_13f(filing):
    """Parse one 13F filing's information table, cached under data/filings/."""
    cache = FILINGS_DIR / f"{filing['accession']}.json"
    if cache.exists():
        parsed = json.loads(cache.read_text())
        _normalize_units(parsed["holdings"])
        return parsed

    base = folder_url(filing["accession"])
    items = _get(base + "index.json").json()["directory"]["item"]
    xml_names = [i["name"] for i in items if i["name"].lower().endswith(".xml")]

    amendment_type = None
    holdings = []
    has_table = False
    for name in xml_names:
        root = ET.fromstring(_get(base + name).content)
        tag = _local(root.tag)
        if tag == "edgarSubmission":
            amendment_type = (_text(root, "amendmentType") or "").upper() or None
        elif tag == "informationTable":
            has_table = True
            holdings += _parse_info_table(root)
    _normalize_units(holdings)

    parsed = {
        "accession": filing["accession"],
        "form": filing["form"],
        "filed": filing["filed"],
        "period": filing["period"],
        "amendment_type": amendment_type,
        "has_table": has_table,
        "holdings": holdings,
    }
    FILINGS_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(parsed, indent=1))
    return parsed


def position_key(row):
    return row["cusip"] + (f"-{row['put_call']}" if row["put_call"] else "")


def _aggregate(rows):
    """Merge rows for the same security (13F splits them by manager/discretion)."""
    merged = {}
    for row in rows:
        key = position_key(row)
        if key in merged:
            merged[key]["value"] += row["value"]
            merged[key]["shares"] += row["shares"]
        else:
            merged[key] = dict(row, key=key)
    return merged


def build_periods(filings):
    """Resolve originals + amendments into one holdings set per report period, oldest first."""
    by_period = defaultdict(list)
    for f in filings:
        if f["form"] in FORMS_13F and f["period"]:
            by_period[f["period"]].append(f)

    periods = []
    for period in sorted(by_period):
        rows, used = None, []
        for f in sorted(by_period[period], key=lambda f: (f["filed"], f["accession"])):
            try:
                parsed = load_13f(f)
            except Exception as exc:  # one bad filing shouldn't sink the whole run
                print(f"  ! skipped {f['accession']}: {exc}")
                continue
            if not parsed["has_table"]:
                continue
            if rows is None or f["form"] == "13F-HR" or parsed["amendment_type"] == "RESTATEMENT":
                rows, used = list(parsed["holdings"]), [f]
            else:  # "NEW HOLDINGS" amendment adds to the original report
                rows += parsed["holdings"]
                used.append(f)
        if rows is None:
            continue
        periods.append({
            "period": period,
            "filed": used[0]["filed"],
            "filings": [{"accession": f["accession"], "form": f["form"], "filed": f["filed"], "url": f["url"]} for f in used],
            "positions": _aggregate(rows),
        })
    return periods
