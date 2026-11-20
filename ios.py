"""
App Store source for gaps.py, using Apple's public search and customer review feeds (no key).

Limits of the feeds: at most 10 pages of 50 reviews per app and country, newest first,
no star filter and no install counts. Reviewer names are in the feed and are never read.
"""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

COUNTRIES = ("us", "gb", "ca", "au")  # one store is too quiet; English stores are merged
MAX_PAGES = 10
DELAY = 1.0  # seconds between requests
PROBE_ID = "389801252"  # a very large app: its feed is never legitimately empty


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.load(resp)


def search(term, country="us", n_hits=60, fetch=None):
    """Apps in Apple's own result order, in the shape gaps.top_apps expects."""
    query = urllib.parse.urlencode({"term": term, "entity": "software", "country": country, "limit": n_hits})
    results = (fetch or fetch_json)(f"https://itunes.apple.com/search?{query}").get("results", [])
    return [{
        "appId": str(a["trackId"]), "ios_id": str(a["trackId"]), "store": "ios",
        "title": str(a.get("trackName", "")), "developer": str(a.get("sellerName", "")),
        "score": a.get("averageUserRating"), "ratings": a.get("userRatingCount") or 0,
        "installs": "",  # Apple does not publish installs
        "free": a.get("price") == 0, "price": a.get("price") or 0, "currency": a.get("currency"),
        "genre": a.get("primaryGenreName", ""), "current_version": a.get("version"),
    } for a in results if a.get("trackId")]


def feed_is_up(fetch=None):
    """One request. Apple's review feed has outages where every app comes back empty with HTTP 200."""
    url = f"https://itunes.apple.com/us/rss/customerreviews/page=1/id={PROBE_ID}/sortby=mostrecent/json"
    try:
        return bool((fetch or fetch_json)(url).get("feed", {}).get("entry"))
    except Exception:
        return False


def _label(entry, key):
    value = entry.get(key)
    return (value.get("label") if isinstance(value, dict) else "") or ""


def parse_entry(entry, country):
    """One feed entry as a review dict, or None. The author field is deliberately not read."""
    try:
        score = int(_label(entry, "im:rating"))
        at = datetime.fromisoformat(_label(entry, "updated")).astimezone(timezone.utc)
    except ValueError:
        return None
    title, body = _label(entry, "title").strip(), _label(entry, "content").strip()
    text = f"{title}: {body}" if title and body else body or title
    if not text:
        return None
    return {"review_id": _label(entry, "id") or None, "score": score, "text": text, "at": at,
            "version": _label(entry, "im:version") or None, "store": "ios", "country": country}


def get_reviews(app_id, count, days=180, countries=COUNTRIES, now=None, fetch=None, delay=DELAY):
    """Newest 1-3 star reviews inside the window, balanced across star levels like scan.get_reviews."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    buckets = {1: [], 2: [], 3: []}
    errors, seen = [], set()
    for country in countries:
        for page in range(1, MAX_PAGES + 1):
            url = (f"https://itunes.apple.com/{country}/rss/customerreviews/"
                   f"page={page}/id={app_id}/sortby=mostrecent/json")
            try:
                entries = (fetch or fetch_json)(url).get("feed", {}).get("entry", [])
            except Exception as exc:
                # ponytail: no retry loop; a failed page ends this country and is reported.
                errors.append(f"{country} page {page}: {exc}")
                break
            finally:
                time.sleep(delay)
            if isinstance(entries, dict):  # a single review arrives as a dict, not a list
                entries = [entries]
            if page == 1 and not entries:
                # Apple's feed is unreliable: it can be empty for an app that had reviews hours earlier.
                # Zero is not "no complaints", so it is reported.
                errors.append(f"{country}: review feed is empty")
            parsed = [r for r in (parse_entry(e, country) for e in entries) if r]
            for r in parsed:
                identity = r["review_id"] or (r["text"], r["at"])
                if r["score"] in buckets and cutoff <= r["at"] <= now and identity not in seen:
                    seen.add(identity)
                    buckets[r["score"]].append(r)
            # Feed is newest first: once a page reaches past the window, later pages are older.
            if not parsed or min(r["at"] for r in parsed) < cutoff:
                break
    for bucket in buckets.values():
        bucket.sort(key=lambda r: r["at"], reverse=True)
    selected = []
    while any(buckets.values()) and len(selected) < count:
        for bucket in buckets.values():
            if bucket and len(selected) < count:
                selected.append(bucket.pop(0))
    for r in selected:
        r["at"] = r["at"].isoformat()
    return selected, errors
