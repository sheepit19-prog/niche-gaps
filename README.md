# niche-gaps

[![tests](https://github.com/sheepit19-prog/niche-gaps/actions/workflows/tests.yml/badge.svg)](https://github.com/sheepit19-prog/niche-gaps/actions/workflows/tests.yml)

Pick an app category on Google Play, the App Store or both. niche-gaps samples the top apps, reads their recent 1-3 star reviews and ranks the complaints **by how many competing apps share them**.

One app with a bug is noise. The same complaint across most of a category is a gap someone could build into.

## Example

`python gaps.py "receipt scanner" --exclude "rewards,cash for receipts,earn"`, then the same 76 reviews regrouped with `--input <saved json> --llm`.

**With Claude** ([full report](examples/receipt-scanner-llm.md)):

```
8 apps, 76 reviews (1-3 stars, last 180 days). 22% of reviews matched no theme. About $0.003.

| # | Theme                                      | Apps | Reviews |
|---|--------------------------------------------|------|---------|
| 1 | App freezes, crashes, or lags              | 4/8  | 8       |
| 2 | Confusing, frequently redesigned interface | 4/8  | 8       |
| 3 | Mandatory account signup before any use    | 4/8  | 7       |
| 4 | Unresponsive support, only AI bot          | 4/8  | 6       |
| 5 | Cannot cancel subscription                 | 4/8  | 4       |
| 6 | Subscription price seen as excessive       | 3/8  | 5       |
| 7 | Scanned amounts, tax and dates misread     | 3/8  | 5       |
```

**With keywords** ([full report](examples/receipt-scanner.md)): 47% of reviews matched no theme, and the top theme was a generic `price/ads` (5/8 apps). Claude split that into cancellation, price and paywall complaints and found themes keywords can't name, like misread tax lines.

Reading it: 7 of the 8 apps are rated 4.3 or higher and few negative reviews come in, so every theme here rests on a handful of reviews. Across two Claude runs the same four themes reached 4 of 8 apps: crashes, forced signup, unreachable support and subscriptions that can't be cancelled. That is a lead to check, not a conclusion.

## How it works

1. **Sample.** Search Google Play for the term and keep the top apps in Google's own order, so the sample is what a searcher sees. `--min-installs` drops tiny apps, `--exclude` drops off-niche ones (receipt scanning also returns cash-back apps).
2. **Collect.** For each app, take the newest 1, 2 and 3 star reviews from the last 180 days, balanced across star levels so 1-star rants don't dominate. Raw reviews are saved to JSON.
3. **Group and rank.** Assign reviews to themes, then rank themes by distinct apps first, review count second.

Grouping has two modes:

| | Keywords (default) | `--llm` |
|---|---|---|
| Needs | nothing | your own `ANTHROPIC_API_KEY` |
| Themes | generic: price, export, crash, login, sync... | niche-specific, e.g. "Scanned amounts, tax and dates misread" |
| Cost | free | under a cent for the example above (Claude Haiku; tokens and cost printed in the report) |

## App Store source

`--store ios` reads Apple's public search and customer review feeds (no key), `--store both` samples each store.

- **No star filter and no installs.** The feed is newest first, 50 reviews a page, at most 10 pages per app and country. 1-3 star reviews and the recency window are filtered in code, and paging stops once a page is older than the window. `--min-ratings` replaces `--min-installs` as the size filter.
- **One country is too quiet.** Low-star reviews are sparse on the App Store, so reviews from `us, gb, ca, au` are merged (`--countries`) and deduplicated by review id. Each quote shows its storefront.
- **Reviewer names are never read.** Only text, stars, date, version and storefront are kept.
- **One request per second, no retries.** A page that fails ends that country and is listed under fetch errors in the report.
- **The feed has outages.** At times Apple returns an empty feed with HTTP 200 for every app, the largest ones included. Each run first asks for one very large app's reviews; if that comes back empty, App Store reviews are skipped and the report says so under fetch errors. A single app's empty feed is also listed there, so a zero is not read as "no complaints".
- **Both stores.** A product counts once only when its title and developer name match on both stores. Anything less certain stays as two apps, and the report says how many were matched. `--apps` applies to each store.

## Design decisions

- **The classifier never counts.** Keyword rules or Claude only assign review ids to themes. App counts, review counts and quotes are computed in code from those ids; ids that don't exist are dropped. A model can't inflate a number in the report.
- **Theme labels and descriptions are written by Claude and can be wrong;** the quotes under each theme are the evidence.
- **LLM themes vary between runs.** Exact labels and the long tail change; check what stays across runs before trusting a theme.
- **Unmatched reviews are reported, not hidden.** The header says what share of reviews fit no theme.
- **The catch-all goes last.** "can't / not working" matches nearly every complaint, so it is shown but never ranked above a real theme.
- **Every report states what it is not.** Reviews are user claims, the sample is not all users, an in-app purchase listing is not proof anyone pays.
- **Rerun without rescraping.** `--input results/<file>.json` regroups saved reviews, so keyword and LLM results can be compared on the same data.

## Usage

```bash
pip install -r requirements.txt

python gaps.py "receipt scanner"                         # keyword themes
python gaps.py "receipt scanner" --exclude rewards       # skip apps whose id or title contains "rewards"
python gaps.py "meal planner" --apps 15 --reviews 100    # bigger sample
python gaps.py --input results/receipt-scanner_X.json --llm   # regroup saved reviews with Claude
python gaps.py "receipt scanner" --store ios             # App Store instead of Google Play
python gaps.py "receipt scanner" --store both            # both stores, matched products count once
```

`--llm` reads `ANTHROPIC_API_KEY` from your environment. No key is stored in this repo.

Also included: `scan.py`, the scanner this project started from. It finds individual apps rated 3.0-3.8 across many categories and samples their complaints.

```bash
python scan.py --terms "expense tracker,invoice maker" --reviews 30
```

## Limits

- Google Play by default (English reviews, US store). App Store samples are smaller: expect a handful of low-star reviews per app outside the largest ones.
- Keyword themes are generic and miss niche-specific problems; that is what `--llm` is for.
- Quiet niches give small samples. The report shows the counts so you can judge.
- Uses [google-play-scraper](https://github.com/JoMingyu/google-play-scraper), an unofficial client. Meant for personal research at low volume.

## Tests

```bash
python -m unittest -v
```

Tests don't touch the network: Google Play, the App Store feeds and Claude are mocked.

## License

MIT
