"""
Niche gap report: what do users across a whole app category keep complaining about?

Usage:
    python gaps.py "receipt scanner"                        # keyword themes, no API key
    python gaps.py "receipt scanner" --exclude rewards      # drop off-niche apps
    python gaps.py "receipt scanner" --llm                  # Claude groups complaints
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

import scan

LLM_MODEL = "claude-haiku-5-5"
# ponytail: list prices for prompts under 100K tokens; Haiku 5.5 bills more above that.
PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6
QUOTES_PER_THEME = 3
# "can't / not working" matches almost every complaint; shown last so it can't outrank real themes.
CATCH_ALL = "missing/broken function"

LLM_PROMPT = """These are recent 1-3 star Google Play reviews of apps in the "{niche}" category.
Group the concrete complaints into themes.

- A theme is one specific problem a competing app could fix, e.g. "CSV export locked behind subscription", not "bad app".
- Add a review id to every theme it clearly supports. Skip reviews with no concrete complaint (vague, praise, off-topic).
- At most 15 themes. Labels under 8 words. Description: one sentence on what users report.

Reviews (id: stars: text):
{reviews}"""


def top_apps(term, n, min_installs=10_000, exclude=(), hits=60):
    """Apps in Google's own result order, so the sample matches what a searcher sees."""
    found = scan.search(term, lang=scan.CONFIG["lang"], country=scan.CONFIG["country"], n_hits=hits)
    excluded = [e.lower() for e in exclude]
    picked = []
    for a in found:
        name = f"{a.get('appId', '')} {a.get('title', '')}".lower()
        if not a.get("appId") or any(e in name for e in excluded):
            continue
        if scan.parse_installs(a.get("installs")) < min_installs:
            continue
        picked.append({"appId": a["appId"], "title": str(a.get("title", "")),
                       "score": a.get("score"), "installs": str(a.get("installs", ""))})
    return picked[:n]


def collect(term, n_apps, per_app, days, min_installs, exclude):
    apps = top_apps(term, n_apps, min_installs, exclude)
    ref = 0
    for i, a in enumerate(apps, 1):
        a.update(scan.get_app_details(a["appId"]))
        a["reviews"], a["review_errors"] = scan.get_reviews(a["appId"], per_app, days)
        for r in a["reviews"]:
            ref += 1
            r["ref"] = f"r{ref}"
        print(f"  {i}. {a['title'][:40]}: {len(a['reviews'])} reviews")
    return {"niche": term, "observed_at": datetime.now(timezone.utc).isoformat(),
            "review_days": days, "reviews_per_app": per_app,
            "locale": {"lang": scan.CONFIG["lang"], "country": scan.CONFIG["country"]},
            "apps": apps}


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
        hits.sort(key=lambda ar: ar[1]["at"], reverse=True)
        quotes, quoted_apps = [], set()
        for a, r in hits:
            if a["appId"] not in quoted_apps and len(quotes) < QUOTES_PER_THEME:
                quoted_apps.add(a["appId"])
                quotes.append((a, r))
        ranked.append({**t, "apps": {a["appId"] for a, _ in hits}, "reviews": len(hits), "quotes": quotes})
    ranked.sort(key=lambda t: (t.get("catch_all", False), -len(t["apps"]), -t["reviews"]))
    return ranked


def short(text, limit=160):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def money_label(a):
    if a.get("free") is False and a.get("price"):
        return f"paid {a.get('currency') or ''} {a['price']}".strip()
    if a.get("offersIAP"):
        return f"IAP {a.get('iap_price') or ''}".strip()
    return "free" if a.get("free") else "unknown"


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
        f"Observed {data['observed_at'][:10]} on Google Play ({data['locale']['lang']}/{data['locale']['country']}). "
        f"{n_apps} apps, {len(reviews)} reviews (1-3 stars, last {data['review_days']} days).",
        f"Grouping: {how}. {share:.0f}% of reviews matched no theme.",
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
            lines += [f"> \"{short(r['text'])}\"  ", f"> {', '.join(source + [r['at'][:10]])}", ""]
    lines += ["", "## Apps sampled", "",
              "| App | Rating | Installs | Monetization | Reviews |",
              "|-----|--------|----------|--------------|---------|"]
    for a in data["apps"]:
        score = f"{a['score']:.1f}" if a.get("score") else "?"
        lines.append(f"| {scan.md_cell(a['title'])} | {score} | {a['installs']} | "
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
    p = argparse.ArgumentParser(description="Rank the complaints an app niche shares on Google Play.")
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
        data = collect(args.niche, args.apps, args.reviews, args.review_days, args.min_installs, exclude)
        if not data["apps"]:
            print("No apps matched. Lower --min-installs or check --exclude.")
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
