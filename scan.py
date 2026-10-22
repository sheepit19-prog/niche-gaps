"""
3-star app scanner for Google Play: finds mediocre apps and samples their complaints.

Usage:
    python scan.py                          # default filters
    python scan.py --min 3.0 --max 3.7     # rating range
    python scan.py --installs 100000        # min installs
    python scan.py --terms todo,notes,diet  # your own search terms
    python scan.py --reviews 30             # reviews per app

Output:
    results/scan_YYYYMMDD_HHMMSS.json
    results/scan_YYYYMMDD_HHMMSS.md        # human-readable report
"""

import sys, os, json, argparse, re
from collections import Counter
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')

try:
    from google_play_scraper import search, app, reviews, Sort
except ImportError:
    print("google-play-scraper is not installed. Run:")
    print("  pip install -r requirements.txt")
    sys.exit(1)


# === AYARLAR (komut satirindan override edilebilir) ===
CONFIG = {
    "min_rating": 3.0,
    "max_rating": 3.8,
    "min_installs": 500,
    "max_installs": 100000,
    "min_ratings_count": 20,
    "reviews_per_app": 10,
    "lang": "en",
    "country": "us",
    "hits_per_term": 100,
}

SEARCH_TERMS = [
    # Productivity
    "notes", "todo", "planner", "calendar", "habit", "tracker", "journal",
    "timer", "pomodoro", "focus", "reminder", "checklist", "organizer",
    # Education
    "study", "learn", "flashcard", "quiz", "language", "course", "tutor",
    # Finance
    "budget", "expense", "bill", "invoice", "receipt", "finance app", "money",
    # Health & Fitness
    "workout", "fitness", "meditation", "yoga", "running", "diet", "sleep",
    "water tracker", "fasting", "exercise",
    # Tools
    "scan", "pdf", "photo editor", "video editor",
    "music", "podcast", "radio", "weather", "news", "map", "travel",
    "chat", "vpn", "backup", "sync", "file transfer",
    # Lifestyle
    "recipe", "cooking", "shopping", "dating",
]

# Discovery preset; paid/IAP listings are signals, not verified sales.
REVENUE_TERMS = [
    # Finance / business: candidate categories, payment willingness unverified
    "expense tracker", "receipt scanner", "mileage tracker", "invoice maker",
    "bookkeeping", "budget planner", "small business", "inventory", "time tracking",
    "subscription tracker", "tax", "freelance",
    # Documents / prosumer tools
    "pdf editor", "document scanner", "esign", "contract",
    # Scheduling / appointments: service businesses pay
    "appointment", "booking", "scheduling", "crm",
    # Health verticals with real subscriptions
    "meal planner", "macro tracker", "workout plan", "period tracker", "symptom tracker",
    # Niche tools people actually pay for
    "habit tracker", "sleep tracker", "language learning",
]

_PRICE_PATTERN = re.compile(
    r"\b(pay|paid|payment|subscription|subscribe|price|expensive|cost|money|premium|"
    r"purchase|trial|ads?|advert\w*|paywall|charge\w*|refund\w*)\b|[$€£]", re.I
)
# Conservative English hints. Unknown language/context stays unclassified.
_PROBLEM_PATTERNS = {
    "crash/startup": r"\b(crash\w*|won'?t open|can'?t open|unable to open|not opening)\b",
    "sync/data loss": r"\b(sync\w*|lost data|data loss|disappear\w*|keeps disconnecting)\b",
    "export": r"\b(export\w*|download\w*)\b",
    "login/account": r"\b(log ?in|sign ?in|password|logs? out|reset password)\b",
    "notifications": r"\b(notification\w*|reminders?|alerts?)\b",
    "battery/performance": r"\b(battery|overheat\w*|slow|freez\w*)\b",
    "missing/broken function": r"\b(not work\w*|doesn'?t work|can'?t|cannot|unable|no way|bug\w*|broken)\b",
}


def complaint_type(text):
    t = (text or "").lower()
    price = bool(_PRICE_PATTERN.search(t))
    problem = bool(problem_topics(t))
    if price and problem:
        return "mixed (manual review)"
    if price:
        return "price/ads (hint)"
    return "problem (hint)" if problem else "unclassified"


def problem_topics(text):
    return [name for name, pattern in _PROBLEM_PATTERNS.items()
            if re.search(pattern, text or "", re.I)]


def review_summary(items):
    # One review contributes at most once per topic; these are not confirmed problems.
    return dict(Counter(topic for item in items for topic in set(item["topics"])))


def md_cell(value):
    return " ".join(str(value).split()).replace("|", "/")


def parse_installs(s):
    try:
        return int(str(s).replace("+", "").replace(",", ""))
    except (ValueError, AttributeError):
        return 0


def fmt(n):
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.0f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def scan(min_rating, max_rating, min_installs, max_installs, hits_per_term, terms):
    results = []
    seen_ids = set()

    for term in terms:
        try:
            apps = search(term, lang=CONFIG["lang"], country=CONFIG["country"], n_hits=hits_per_term)
            for a in apps:
                sid = a.get("appId", "")
                score = a.get("score")
                if sid in seen_ids or score is None:
                    continue
                seen_ids.add(sid)

                ratings_count = a.get("ratings", 0) or 0
                installed = a.get("installs", "0") or "0"

                if not (min_rating <= score <= max_rating):
                    continue
                # search() may return 0 for ratings; skip filter if 0
                if ratings_count > 0 and ratings_count < CONFIG["min_ratings_count"]:
                    continue
                if parse_installs(installed) < min_installs:
                    continue
                if max_installs and parse_installs(installed) > max_installs:
                    continue

                results.append({
                    "title": str(a.get("title", "")),
                    "score": score,
                    "ratings": ratings_count,
                    "installs": str(installed),
                    "appId": str(sid),
                    "developer": str(a.get("developer", "")),
                    "found_via": term,
                })
        except Exception as exc:
            print(f"Warning: search failed for {term!r}: {exc}", file=sys.stderr)
            continue

    # Sort by rating ascending, lowest-rated (most dissatisfied) first
    results.sort(key=lambda x: x["score"])
    return results


def get_reviews(app_id, count, days=180, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    buckets = []
    errors = []
    seen = set()
    # Fetch all three strata independently, then round-robin to avoid 1-star dominance.
    for star_filter in [1, 2, 3]:
        bucket = []
        try:
            revs, _ = reviews(
                app_id,
                lang=CONFIG["lang"], country=CONFIG["country"],
                sort=Sort.NEWEST, count=count,
                filter_score_with=star_filter,
            )
            for r in revs:
                content = (r.get("content", "") or "").strip()
                at = r.get("at")
                if not isinstance(at, datetime):
                    continue
                # Library returns naive dates; normalize consistently to UTC for filtering.
                at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
                identity = r.get("reviewId") or (content, at.isoformat())
                if not content or at < cutoff or at > now or identity in seen:
                    continue
                seen.add(identity)
                bucket.append({
                    "review_id": r.get("reviewId"), "score": r.get("score"),
                    "text": content, "at": at.isoformat(),
                    "version": r.get("reviewCreatedVersion") or r.get("appVersion"),
                    "classification": complaint_type(content), "topics": problem_topics(content),
                })
        except Exception as exc:
            errors.append(f"{star_filter}-star: {exc}")
        buckets.append(bucket)
    selected = []
    while any(buckets) and len(selected) < count:
        for bucket in buckets:
            if bucket and len(selected) < count:
                selected.append(bucket.pop(0))
    return selected, errors


def get_app_details(app_id):
    """Store pricing is observable; actual paying customer count is unknown."""
    try:
        d = app(app_id, lang=CONFIG["lang"], country=CONFIG["country"])
        free = d.get("free")
        iap = bool(d.get("offersIAP"))
        return {
            "free": free,
            "offersIAP": iap,
            "price": d.get("price") or 0,
            "currency": d.get("currency"),
            "iap_price": d.get("inAppProductPrice"),
            "genre": d.get("genre", ""),
            "has_payment_option": (free is False) or iap,
            "payment_evidence": "not verified",
            "updated": d.get("updated"),
            "current_version": d.get("version"),
        }
    except Exception as exc:
        return {"details_error": str(exc), "payment_evidence": "not verified"}


def main():
    parser = argparse.ArgumentParser(description="3-Star Google Play App Scanner")
    parser.add_argument("--min", type=float, default=None, help="Min rating (default: 3.0; revenue: 3.8)")
    parser.add_argument("--max", type=float, default=None, help="Max rating (default: 3.8; revenue: 4.4)")
    parser.add_argument("--installs", type=int, default=None, help="Min installs (default: 500; revenue: 10K)")
    parser.add_argument("--max-installs", type=int, default=None, help="Max installs (default: 100K; revenue: 1M; 0=no limit)")
    parser.add_argument("--reviews", type=int, default=CONFIG["reviews_per_app"], help="Reviews per app (default: 10)")
    parser.add_argument("--terms", type=str, default=None, help="Comma-separated search terms")
    parser.add_argument("--output", type=str, default=None, help="Output dir (default: results/)")
    parser.add_argument("--no-reviews", action="store_true", help="Skip review fetching (faster)")
    parser.add_argument("--review-days", type=int, default=180, help="Review recency window in days (default: 180)")
    parser.add_argument("--revenue", action="store_true",
                        help="Discovery preset: paid/IAP listings; does not verify revenue")
    args = parser.parse_args()

    for name, default, revenue_default in [
        ("min", 3.0, 3.8), ("max", 3.8, 4.4),
        ("installs", 500, 10000), ("max_installs", 100000, 1000000),
    ]:
        if getattr(args, name) is None:
            setattr(args, name, revenue_default if args.revenue else default)
    if not 0 <= args.min <= args.max <= 5:
        parser.error("rating range must satisfy 0 <= min <= max <= 5")
    if args.installs < 0 or args.max_installs < 0 or (args.max_installs and args.max_installs < args.installs):
        parser.error("invalid install range")
    if args.reviews < 1 or args.review_days < 1:
        parser.error("--reviews and --review-days must be positive")

    terms = SEARCH_TERMS
    if args.terms:
        terms = [t.strip() for t in args.terms.split(",")]
        terms = [t for t in terms if t]
        if not terms:
            parser.error("--terms must contain a search term")
    elif args.revenue:
        terms = REVENUE_TERMS

    print(f"=== 3-Star App Scanner ===")
    max_txt = fmt(args.max_installs) if args.max_installs else "∞"
    print(f"Rating: {args.min} - {args.max}  |  Installs: {fmt(args.installs)} - {max_txt}  |  Terms: {len(terms)}")
    print(f"Searching...")

    apps = scan(args.min, args.max, args.installs, args.max_installs, CONFIG["hits_per_term"], terms)
    print(f"Found {len(apps)} candidates.\n")

    if not apps:
        print("No apps found. Try lowering --installs or raising --max-installs.")
        return

    # Bound enrichment to 40 candidates; this is not an exhaustive market census.
    if args.revenue:
        apps.sort(key=lambda x: parse_installs(x["installs"]), reverse=True)
        print("Enriching candidates with monetization data...")
        kept = []
        for a in apps[:40]:
            a.update(get_app_details(a["appId"]))
            if a.get("details_error"):
                print(f"Warning: details unavailable for {a['appId']}: {a['details_error']}", file=sys.stderr)
            if a.get("has_payment_option"):
                kept.append(a)
            if len(kept) >= 20:
                break
        apps = kept
        print(f"{len(apps)} paid/IAP listings kept; sales NOT verified.\n")
        if not apps:
            print("No paid/IAP listings matched in the enriched sample. Check warnings or widen filters.")
            return

    # Fetch reviews
    if not args.no_reviews:
        print(f"Fetching reviews for top candidates...")
        for i, a in enumerate(apps[:15]):
            try:
                r, errors = get_reviews(a["appId"], args.reviews, args.review_days)
                a["complaints"] = r
                a["review_errors"] = errors
                a["topic_hints"] = review_summary(r)
                a["review_status"] = "partial/error" if errors else ("sampled" if r else "no recent reviews in fetched sample")
                print(f"  {i+1}. {a['title'][:40]}: {len(r)} reviews")
            except Exception as e:
                a["complaints"] = []
                a["review_status"] = "error"
                a["review_errors"] = [str(e)]
                print(f"  {i+1}. {a['title'][:40]}: error: {e}")

    for a in apps:
        a.setdefault("review_status", "not fetched")
        a.setdefault("payment_evidence", "not verified")
        a["source_url"] = f"https://play.google.com/store/apps/details?id={a['appId']}"
        a["observed_at"] = datetime.now(timezone.utc).isoformat()
        a["review_window_days"] = args.review_days
        a["locale"] = {"lang": CONFIG["lang"], "country": CONFIG["country"]}

    # Save results
    out_dir = args.output or "results"
    os.makedirs(out_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    json_path = os.path.join(out_dir, f"scan_{timestamp}.json")
    md_path = os.path.join(out_dir, f"scan_{timestamp}.md")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(apps, f, ensure_ascii=False, indent=2)

    # Markdown report
    title_hdr = "Revenue-Oriented App Scan" if args.revenue else "3-Star App Scan"
    lines = [
        f"# {title_hdr} ({timestamp})",
        f"",
        f"**Filters:** rating {args.min}-{args.max} | {fmt(args.installs)}-{max_txt} installs | {len(terms)} search terms"
        + (" | paid/IAP listings (sales unverified)" if args.revenue else ""),
        f"**Found:** {len(apps)} candidates",
        f"**Review window:** last {args.review_days} days; balanced 1/2/3-star sample, not representative of all users.",
        "**Decision:** discovery only. Installs are not customers; IAP is not payment proof.",
        "Topic counts are English keyword hints within one app, not confirmed recurring problems.",
        "Search failures are printed to stderr; detail enrichment covers at most 40 candidates, reviews at most 15.",
        f"",
    ]

    def money_label(a):
        if a.get("free") is False and a.get("price"):
            return f"{a.get('currency') or 'currency unknown'} {a['price']} paid"
        if a.get("offersIAP"):
            return f"IAP {a.get('iap_price') or ''}".strip()
        return "not checked"

    def pick_complaint(a):
        cs = a.get("complaints", [])
        feat = [c for c in cs if c["classification"] == "problem (hint)"]
        chosen = (feat or cs or [None])[0]
        if not chosen:
            return "", ""
        return chosen["classification"], md_cell(chosen["text"][:80])

    if args.revenue:
        lines += [
            f"| # | App | Rating | Installs | Monetization | Complaint (type) |",
            f"|---|-----|--------|----------|--------------|------------------|",
        ]
        for i, a in enumerate(apps[:20]):
            ctype, ctext = pick_complaint(a)
            lines.append(f"| {i+1} | {md_cell(a['title'][:30])} | {a['score']:.2f} | {a['installs']} | "
                         f"{md_cell(money_label(a))} | {ctype}: {ctext} |")
    else:
        lines += [
            f"| # | App | Rating | Installs | Key Complaints |",
            f"|---|-----|--------|----------|----------------|",
        ]
        for i, a in enumerate(apps[:20]):
            complaints = a.get("complaints", [])
            top_c = md_cell(complaints[0]["text"][:80]) if complaints else ""
            lines.append(f"| {i+1} | {md_cell(a['title'][:30])} | {a['score']:.2f} | {a['installs']} | {top_c} |")

    for a in apps[:20]:
        lines += ["", f"## {md_cell(a['title'])}", "", f"Source: {a['source_url']}",
                  f"Review status: {a['review_status']}",
                  f"Current version: {md_cell(a.get('current_version') or 'unknown')}",
                  f"Last update (store timestamp): {a.get('updated') or 'unknown'}",
                  "Payment evidence: not verified",
                  f"Topic hints: {md_cell(json.dumps(a.get('topic_hints', {}), ensure_ascii=False))}"]
        for error in a.get("review_errors", []):
            lines.append(f"- Fetch error: {md_cell(error)}")
        for c in a.get("complaints", []):
            lines += [f"- {c['at']} | {c['score']} stars | version {md_cell(c['version'] or 'unknown')}"
                      f" | {c['classification']} | id {md_cell(c['review_id'] or 'unknown')}",
                      f"  {md_cell(c['text'])}"]

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # Terminal summary
    print(f"\n{'='*70}")
    for i, a in enumerate(apps[:10]):
        c = a.get("complaints", [])
        print(f"\n{i+1}. {a['title']}")
        print(f"   ★ {a['score']:.2f}  |  {a['installs']} installs  |  {a['developer']}")
        print(f"   appId: {a['appId']}")
        if c:
            print(f"   Top complaint: {c[0]['text'][:120]}")

    print(f"\n---\nSaved: {json_path}\n       {md_path}")


if __name__ == "__main__":
    main()
