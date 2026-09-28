"""News/interview collection + Claude extraction of Druckenmiller's own stated positions.

Finds recent articles via Bing News RSS, fetches their text, and asks Claude to
pull out only views Druckenmiller himself expressed (not the reporter's, not a
rehash of the 13F). Results accumulate in data/statements.json.
"""
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

from .sec import DATA_DIR

STATEMENTS = DATA_DIR / "statements.json"
QUERIES = ('"Stanley Druckenmiller"', '"Druckenmiller" Duquesne')
LOOKBACK_DAYS = 45
MAX_NEW_PER_RUN = 15  # bounds daily API spend
MODEL = "claude-opus-5-5"

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
})


def _load():
    return json.loads(STATEMENTS.read_text()) if STATEMENTS.exists() else {"articles": []}


def _unwrap_bing(link):
    # Bing RSS links go through apiclick.aspx?...&url=<real article>
    qs = parse_qs(urlparse(link).query)
    return qs.get("url", [link])[0]


def search_news():
    """Recent articles mentioning Druckenmiller, newest first, deduped by URL."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    found = {}
    for q in QUERIES:
        url = f"https://www.bing.com/news/search?q={quote_plus(q)}&format=rss&count=50"
        try:
            r = _session.get(url, timeout=20)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as exc:
            print(f"  ! news search failed for {q}: {exc}")
            continue
        for item in root.iter("item"):
            link = _unwrap_bing(item.findtext("link") or "")
            try:
                published = parsedate_to_datetime(item.findtext("pubDate") or "")
            except (TypeError, ValueError):
                published = None
            if not link or (published and published < cutoff):
                continue
            source = next((c.text for c in item if c.tag.endswith("Source") and c.text), urlparse(link).netloc)
            found.setdefault(link, {
                "url": link,
                "title": unescape(item.findtext("title") or "").strip(),
                "snippet": unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or "")).strip(),
                "source": source,
                "published": published.date().isoformat() if published else None,
            })
        time.sleep(1)
    return sorted(found.values(), key=lambda a: a["published"] or "", reverse=True)


class _TextExtractor(HTMLParser):
    """Collects paragraph-ish text, skipping scripts, styles and navigation."""

    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form"}
    BLOCK = {"p", "li", "blockquote", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__()
        self.depth_skip = 0
        self.in_block = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth_skip += 1
        elif tag in self.BLOCK:
            self.in_block += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth_skip:
            self.depth_skip -= 1
        elif tag in self.BLOCK and self.in_block:
            self.in_block -= 1
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_block and not self.depth_skip:
            self.parts.append(data)


def fetch_text(url):
    try:
        r = _session.get(url, timeout=20)
        r.raise_for_status()
    except Exception:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(r.text)
    except Exception:
        return ""
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


VIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "asset": {"type": "string", "description": "What the view is about, e.g. 'US 10-year Treasuries', 'Japanese yen', 'Nvidia'"},
        "ticker": {"type": "string", "description": "US ticker if it is a listed security, else empty string"},
        "asset_class": {"type": "string", "enum": ["equity", "rates", "currency", "commodity", "crypto", "index", "other"]},
        "stance": {"type": "string", "enum": ["long", "short", "bullish", "bearish", "neutral", "exited"]},
        "action": {"type": "string", "enum": ["bought", "added", "sold", "trimmed", "holds", "exited", "opinion_only", "unknown"]},
        "conviction": {"type": "string", "enum": ["high", "medium", "low"]},
        "quote": {"type": "string", "description": "Verbatim supporting sentence from the article"},
        "attribution": {"type": "string", "enum": ["direct_quote", "paraphrase"]},
    },
    "required": ["asset", "ticker", "asset_class", "stance", "action", "conviction", "quote", "attribution"],
    "additionalProperties": False,
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "article_kind": {"type": "string", "enum": ["interview_or_speech", "reported_statement", "13f_coverage", "other"]},
        "summary_ko": {"type": "string"},
        "views": {"type": "array", "items": VIEW_SCHEMA},
    },
    "required": ["relevant", "article_kind", "summary_ko", "views"],
    "additionalProperties": False,
}

SYSTEM = """You extract Stanley Druckenmiller's own investment positions and market views from news articles.

Only record a view when the article attributes it to Druckenmiller himself (his words in an interview, speech, podcast or post, or a reporter paraphrasing what he said). Do not record the reporter's analysis, other investors' views, or holdings that come only from Duquesne's 13F filing — for articles that merely recap a 13F, set article_kind to "13f_coverage" and leave views empty.

Use "long"/"short" when he says he holds the position, "bullish"/"bearish" for an opinion without a stated position. Set action from what he says he did. The quote must be copied verbatim from the article text. If nothing qualifies, return relevant=false with an empty views list.

Write summary_ko as one or two Korean sentences on what he said (or why the article is not relevant)."""


def _extract(client, article, text):
    body = text if len(text) >= 400 else f"{article['title']}\n\n{article['snippet']}"
    prompt = (
        f"Title: {article['title']}\nSource: {article['source']}\nPublished: {article['published']}\n"
        f"URL: {article['url']}\n\n<article>\n{body[:40000]}\n</article>"
    )
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        print(f"  ! extraction stopped ({response.stop_reason}) for {article['url']}")
        return None
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def update():
    """Process new articles; returns the full accumulated statements store."""
    store = _load()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set - skipping news extraction")
        return store

    import anthropic  # only needed when extraction runs

    client = anthropic.Anthropic()
    seen = {a["url"] for a in store["articles"]}
    candidates = [a for a in search_news() if a["url"] not in seen]
    print(f"News: {len(candidates)} new candidate articles")

    skipped = {"relevant": False, "article_kind": "other", "summary_ko": "", "views": []}
    processed = 0
    for article in candidates:
        if processed >= MAX_NEW_PER_RUN:
            break
        text = fetch_text(article["url"])
        haystack = f"{article['title']} {article['snippet']} {text}"
        if "druckenmiller" not in haystack.lower():
            result = skipped  # recorded so it isn't fetched again tomorrow
        else:
            try:
                result = _extract(client, article, text)
            except anthropic.APIError as exc:
                print(f"  ! Claude API error for {article['url']}: {exc}")
                continue  # transient; retry on the next run
            processed += 1
            result = result or skipped
        store["articles"].append({
            **article,
            "full_text": len(text) >= 400,
            "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **result,
        })
        if result["relevant"]:
            print(f"  + {article['source']}: {article['title'][:70]} ({len(result['views'])} views)")

    store["articles"].sort(key=lambda a: a.get("published") or "", reverse=True)
    STATEMENTS.parent.mkdir(parents=True, exist_ok=True)
    STATEMENTS.write_text(json.dumps(store, ensure_ascii=False, indent=1))
    return store


def latest_views(store, today=None):
    """Most recent stated view per asset, with an age-decayed confidence."""
    today = today or datetime.now(timezone.utc).date()
    latest = {}
    for article in sorted(store["articles"], key=lambda a: a.get("published") or ""):
        if not article.get("relevant") or article.get("article_kind") == "13f_coverage":
            continue
        for view in article["views"]:
            key = (view["ticker"] or view["asset"]).strip().lower()
            latest[key] = {**view, "date": article.get("published"), "url": article["url"],
                           "source": article["source"], "title": article["title"]}
    views = []
    for view in latest.values():
        age = (today - datetime.fromisoformat(view["date"]).date()).days if view["date"] else None
        base = {"high": 1.0, "medium": 0.7, "low": 0.4}[view["conviction"]]
        if view["attribution"] == "paraphrase":
            base *= 0.8
        # Halve confidence every ~6 months since he said it.
        view["confidence"] = round(base * (0.5 ** (age / 180)) if age is not None else base * 0.5, 2)
        view["age_days"] = age
        views.append(view)
    return sorted(views, key=lambda v: v["confidence"], reverse=True)
