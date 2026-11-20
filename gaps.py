"""
Niche gap report: what do users across a whole app category keep complaining about?

Usage:
    python gaps.py "receipt scanner"                        # keyword themes, no API key
    python gaps.py "receipt scanner" --exclude rewards      # drop off-niche apps
    python gaps.py "receipt scanner" --llm                  # Claude groups complaints
    python gaps.py "receipt scanner" --store both           # Google Play and App Store
    python gaps.py --input results/receipt-scanner_X.json --llm   # regroup saved reviews

Output:
    results/<niche>_<timestamp>.json      # sampled apps and reviews
    results/<niche>_<timestamp>[_llm].md  # ranked gap report
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone

import ios
import scan

LLM_MODEL = "claude-haiku-5-5"
# ponytail: list prices for prompts under 100K tokens; Haiku 5.5 bills more above that.
PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6
QUOTES_PER_THEME = 3
QUOTE_CHARS = 160
# "can't / not working" matches almost every complaint; shown last so it can't outrank real themes.
CATCH_ALL = "missing/broken function"

LLM_PROMPT = """These are recent 1-3 star app store reviews of apps in the "{niche}" category.
Group the concrete complaints into themes.

- A theme is one specific problem a competing app could fix, e.g. "CSV export locked behind subscription", not "bad app".
- Add a review id to every theme it clearly supports. Skip reviews with no concrete complaint (vague, praise, off-topic).
- At most 15 themes. Labels under 8 words. Description: one sentence on what users report.
- No numbers, prices or counts in labels or descriptions; the report computes and quotes those itself.

Reviews (id: stars: text):
{reviews}"""


def top_apps(term, n, min_installs=10_000, exclude=(), hits=60, store="play",
             min_ratings=500, countries=ios.COUNTRIES):
    """Apps in the store's own result order, so the sample matches what a searcher sees."""
    excluded = [e.lower() for e in exclude]
    if store == "ios":
        # Apple publishes no installs; the rating count is the size filter instead.
        found = [a for a in ios.search(term, country=countries[0], n_hits=hits) if a["ratings"] >= min_ratings]
    else:
        found = [{"appId": a["appId"], "store": "play", "title": str(a.get("title", "")),
                  "developer": str(a.get("developer", "")), "score": a.get("score"),
                  "installs": str(a.get("installs", ""))}
                 for a in scan.search(term, lang=scan.CONFIG["lang"], country=scan.CONFIG["country"], n_hits=hits)
                 if a.get("appId") and scan.parse_installs(a.get("installs")) >= min_installs]
    return [a for a in found if not any(e in f"{a['appId']} {a['title']}".lower() for e in excluded)][:n]


_COMPANY_WORDS = re.compile(r"\b(inc|llc|ltd|limited|gmbh|corp|co|company|pty|bv|srl|ab)\b")


def same_product(a, b):
    """True only when both the title before any subtitle and the developer name agree."""
    def key(text):
        return re.sub(r"[^a-z0-9]+", "", text)
    title_a, title_b = (key(re.split(r"[:\-–|(]", x["title"].lower())[0]) for x in (a, b))
    dev_a, dev_b = (key(_COMPANY_WORDS.sub("", x.get("developer", "").lower())) for x in (a, b))
    return (bool(title_a) and title_a == title_b and min(len(dev_a), len(dev_b)) >= 3
            and (dev_a.startswith(dev_b) or dev_b.startswith(dev_a)))


def merge_stores(play_apps, ios_apps):
    """A product listed on both stores counts once. Unsure matches stay separate."""
    left = list(ios_apps)
    merged = []
    for p in play_apps:
        twin = next((i for i in left if same_product(p, i)), None)
        if twin:
            left.remove(twin)
            # The App Store listing is kept on the record so a wrong match can be spotted in the JSON.
            p = {**p, "store": "both", "ios_id": twin["ios_id"], "ratings": twin["ratings"],
                 "ios_title": twin["title"], "ios_developer": twin.get("developer", "")}
        merged.append(p)
    return merged + left


def collect(term, n_apps, per_app, days, min_installs, exclude, store="play",
            min_ratings=500, countries=ios.COUNTRIES):
    def pick(s):
        return top_apps(term, n_apps, min_installs, exclude, store=s, min_ratings=min_ratings, countries=countries)
    apps = merge_stores(pick("play"), pick("ios")) if store == "both" else pick(store)
    ref = 0
    ios_up = not any(a.get("ios_id") for a in apps) or ios.feed_is_up()
    if not ios_up:
        print("  App Store review feed is down (probe app returned no reviews): skipping App Store reviews.")
    for i, a in enumerate(apps, 1):
        a["reviews"], a["review_errors"] = [], []
        if a["store"] != "ios":
            a.update(scan.get_app_details(a["appId"]))
            a["reviews"], a["review_errors"] = scan.get_reviews(a["appId"], per_app, days)
        if a.get("ios_id") and not ios_up:
            a["review_errors"].append("App Store review feed was down, reviews not fetched")
        elif a.get("ios_id"):
            reviews, errors = ios.get_reviews(a["ios_id"], per_app, days, countries)
            a["reviews"] += reviews
            a["review_errors"] += errors
        for r in a["reviews"]:
            ref += 1
            r["ref"] = f"r{ref}"
        print(f"  {i}. {a['title'][:40]}: {len(a['reviews'])} reviews")
    return {"niche": term, "observed_at": datetime.now(timezone.utc).isoformat(),
            "review_days": days, "reviews_per_app": per_app, "store": store,
            "locale": {"lang": scan.CONFIG["lang"], "country": scan.CONFIG["country"]},
            "ios_countries": list(countries), "apps": apps}


def all_reviews(data):
    return [(a, r) for a in data["apps"] for r in a["reviews"]]


def regex_themes(data):
    """Generic English keyword hints; niche-specific problems need --llm."""
    groups = {}
    for _, r in all_reviews(data):
        topics = set(scan.problem_topics(r["text"]))
        if scan._PRICE_PATTERN.search(r["text"]):
            topics.add("price/ads")
        for t in topics:
            groups.setdefault(t, []).append(r["ref"])
    return [{"label": "other: can't / not working (catch-all)" if t == CATCH_ALL else t,
             "description": "", "refs": refs, "catch_all": t == CATCH_ALL}
            for t, refs in groups.items()]


def llm_themes(data, client=None):
    """The model only assigns review ids to themes; every count is computed in rank()."""
    import anthropic
    from pydantic import BaseModel

    class Theme(BaseModel):
        label: str
        description: str
        refs: list[str]

    class Themes(BaseModel):
        themes: list[Theme]

    client = client or anthropic.Anthropic()
    lines = "\n".join(f"{r['ref']}: {r['score']}: {' '.join(r['text'].split())}" for _, r in all_reviews(data))
    response = client.messages.parse(
        model=LLM_MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": LLM_PROMPT.format(niche=data["niche"], reviews=lines)}],
        output_format=Themes,
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"LLM grouping stopped early: {response.stop_reason}")
    usage = response.usage
    cost = usage.input_tokens * PRICE_IN + usage.output_tokens * PRICE_OUT
    themes = [t.model_dump() for t in response.parsed_output.themes]
    return themes, {"model": LLM_MODEL, "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens, "cost_usd": round(cost, 4)}


def rank(themes, data):
    """Count distinct apps and reviews per theme from review ids; unknown ids are dropped."""
    lookup = {r["ref"]: (a, r) for a, r in all_reviews(data)}
    ranked = []
    for t in themes:
        hits = [lookup[ref] for ref in dict.fromkeys(t["refs"]) if ref in lookup]
        if not hits:
            continue
        # Quotes: reviews short enough to show in full come first, so a reader can check the theme.
        hits.sort(key=lambda ar: ar[1]["at"], reverse=True)
        hits.sort(key=lambda ar: len(" ".join(ar[1]["text"].split())) > QUOTE_CHARS)
        quotes, quoted_apps = [], set()
        for a, r in hits:
            if a["appId"] not in quoted_apps and len(quotes) < QUOTES_PER_THEME:
                quoted_apps.add(a["appId"])
                quotes.append((a, r))
        ranked.append({**t, "apps": {a["appId"] for a, _ in hits}, "reviews": len(hits), "quotes": quotes})
    ranked.sort(key=lambda t: (t.get("catch_all", False), -len(t["apps"]), -t["reviews"]))
    return ranked


def short(text, limit=QUOTE_CHARS):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def money_label(a):
    if a.get("free") is False and a.get("price"):
        return f"paid {a.get('currency') or ''} {a['price']}".strip()
    if a.get("offersIAP"):
        return f"IAP {a.get('iap_price') or ''}".strip()
    if a.get("store") == "ios" and a.get("free"):
        return "free to download (IAP not listed)"
    return "free" if a.get("free") else "unknown"


def store_label(data):
    play = f"Google Play ({data['locale']['lang']}/{data['locale']['country']})"
    app_store = f"the App Store ({', '.join(data.get('ios_countries', []))})"
    return {"play": play, "ios": app_store, "both": f"{play} and {app_store}"}[data.get("store", "play")]


def report(data, ranked, method):
    reviews = all_reviews(data)
    n_apps = len(data["apps"])
    classified = {ref for t in ranked for ref in t["refs"]}
    matched = sum(1 for _, r in reviews if r["ref"] in classified)
    share = 100 * (len(reviews) - matched) / len(reviews) if reviews else 0
    if method.get("model"):
        how = (f"Claude ({method['model']}) assigned reviews to themes; "
               f"{method['input_tokens']:,} in / {method['output_tokens']:,} out tokens, about ${method['cost_usd']:.3f}")
    else:
        how = "generic English keyword hints (no LLM)"
    lines = [
        f"# Niche gap report: {data['niche']}",
        "",
        f"Observed {data['observed_at'][:10]} on {store_label(data)}. "
        f"{n_apps} apps, {len(reviews)} reviews (1-3 stars, last {data['review_days']} days).",
        f"Grouping: {how}. {share:.0f}% of reviews matched no theme.",
    ]
    if data.get("store") == "both":
        both = sum(1 for a in data["apps"] if a.get("store") == "both")
        lines.append(f"{both} of {n_apps} apps were matched across both stores by title and developer and count once. "
                     "The rest are counted per store, so a product whose two listings differ may appear twice.")
    lines += [
        "",
        "## Themes, ranked by how many apps share them",
        "",
        "| # | Theme | Apps | Reviews |",
        "|---|-------|------|---------|",
    ]
    lines += [f"| {i} | {scan.md_cell(t['label'])} | {len(t['apps'])}/{n_apps} | {t['reviews']} |"
              for i, t in enumerate(ranked, 1)]
    for i, t in enumerate(ranked, 1):
        lines += ["", f"### {i}. {t['label']}", ""]
        if t.get("description"):
            lines += [t["description"], ""]
        for a, r in t["quotes"]:
            source = [a["title"], f"{r['score']} star" + ("" if r["score"] == 1 else "s")]
            source += [f"v{r['version']}"] if r.get("version") else []
            source += [f"App Store {r['country']}"] if r.get("store") == "ios" else []
            lines += [f"> \"{short(r['text'])}\"  ", f"> {', '.join(source + [r['at'][:10]])}", ""]
    lines += ["", "## Apps sampled", "",
              "| App | Store | Rating | Size | Monetization | Reviews |",
              "|-----|-------|--------|------|--------------|---------|"]
    for a in data["apps"]:
        score = f"{a['score']:.1f}" if a.get("score") else "?"
        size = " / ".join(filter(None, [a.get("installs") and f"{a['installs']} installs",
                                        a.get("ratings") and f"{scan.fmt(a['ratings'])} iOS ratings"]))
        lines.append(f"| {scan.md_cell(a['title'])} | {a.get('store', 'play')} | {score} | {size or '?'} | "
                     f"{scan.md_cell(money_label(a))} | {len(a['reviews'])} |")
    lines += ["", "## What this is not", "",
              "- Reviews are user claims. The sample is the newest reviews per star level, not all users.",
              "- Counts come from review ids assigned to each theme, computed in code, not estimated by a model.",
              "- No sales or demand data. A complaint many apps share is a lead to verify, not a market.",
              "- In-app purchase listed is a payment option, not proof anyone pays."]
    errors = [f"{a['title']}: {e}" for a in data["apps"] for e in a.get("review_errors", [])]
    if errors:
        lines += ["", "Fetch errors (samples are partial):"] + [f"- {scan.md_cell(e)}" for e in errors]
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description="Rank the complaints an app niche shares on Google Play or the App Store.")
    p.add_argument("--store", choices=["play", "ios", "both"], default="play", help="Review source (default: play)")
    p.add_argument("--min-ratings", type=int, default=500, help="App Store: skip apps with fewer ratings (default: 500)")
    p.add_argument("--countries", default=",".join(ios.COUNTRIES),
                   help="App Store: storefronts to merge reviews from (default: us,gb,ca,au)")
    p.add_argument("niche", nargs="?", help='Search term, e.g. "receipt scanner"')
    p.add_argument("--apps", type=int, default=12, help="Apps to sample (default: 12)")
    p.add_argument("--reviews", type=int, default=60, help="Reviews per app, balanced 1/2/3 stars (default: 60)")
    p.add_argument("--review-days", type=int, default=180, help="Recency window in days (default: 180)")
    p.add_argument("--min-installs", type=int, default=10_000, help="Skip smaller apps (default: 10K)")
    p.add_argument("--exclude", default="", help="Comma-separated words; apps whose id or title contain one are skipped")
    p.add_argument("--llm", action="store_true", help="Group with Claude (needs ANTHROPIC_API_KEY)")
    p.add_argument("--input", help="Reuse a saved reviews JSON instead of scraping")
    p.add_argument("--output", default="results", help="Output dir (default: results/)")
    args = p.parse_args()
    if not args.niche and not args.input:
        p.error("give a niche search term or --input")
    if min(args.apps, args.reviews, args.review_days) < 1:
        p.error("--apps, --reviews and --review-days must be positive")

    os.makedirs(args.output, exist_ok=True)
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
        base = os.path.splitext(args.input)[0]
    else:
        print(f"Sampling {args.apps} apps for {args.niche!r}...")
        exclude = [e.strip() for e in args.exclude.split(",") if e.strip()]
        countries = [c.strip().lower() for c in args.countries.split(",") if c.strip()] or list(ios.COUNTRIES)
        data = collect(args.niche, args.apps, args.reviews, args.review_days, args.min_installs, exclude,
                       args.store, args.min_ratings, countries)
        if not data["apps"]:
            print("No apps matched. Lower --min-installs or --min-ratings, or check --exclude.")
            return
        slug = re.sub(r"[^a-z0-9]+", "-", args.niche.lower()).strip("-")
        base = os.path.join(args.output, f"{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Saved reviews: {base}.json")

    if args.llm:
        import anthropic
        try:
            themes, method = llm_themes(data)
        except (anthropic.AnthropicError, RuntimeError) as exc:
            print(f"LLM grouping failed: {exc}\nReviews are saved; rerun with --input {base}.json")
            raise SystemExit(1)
    else:
        themes, method = regex_themes(data), {}
    md_path = base + ("_llm" if args.llm else "") + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report(data, rank(themes, data), method))
    print(f"Saved report: {md_path}")


if __name__ == "__main__":
    main()
