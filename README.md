# niche-gaps

[![tests](https://github.com/sheepit19-prog/niche-gaps/actions/workflows/tests.yml/badge.svg)](https://github.com/sheepit19-prog/niche-gaps/actions/workflows/tests.yml)

Pick an app category on Google Play. niche-gaps samples the top apps, reads their recent 1-3 star reviews and ranks the complaints **by how many competing apps share them**.

One app with a bug is noise. The same complaint across most of a category is a gap someone could build into.

## Example

`python gaps.py "receipt scanner" --exclude "rewards,cash for receipts,earn"` ([full report](examples/receipt-scanner.md)):

```
8 apps, 76 reviews (1-3 stars, last 180 days). Keyword grouping: 46% of reviews matched no theme.

| # | Theme               | Apps | Reviews |
|---|---------------------|------|---------|
| 1 | price/ads           | 5/8  | 24      |
| 2 | battery/performance | 2/8  | 4       |
| 3 | login/account       | 2/8  | 3       |

> "Its a subscription app. Big no."
> Receipt Scanner: Easy Expense, 1 star, 2026-10-17
```

Reading it: 7 of the 8 apps are rated 4.3 or higher and few negative reviews come in. The complaint most apps share is not a missing feature, it is pushback on subscriptions and pricing. With 76 reviews that is a lead to check, not a conclusion, and a quiet niche is a result too.

## How it works

1. **Sample.** Search Google Play for the term and keep the top apps in Google's own order, so the sample is what a searcher sees. `--min-installs` drops tiny apps, `--exclude` drops off-niche ones (receipt scanning also returns cash-back apps).
2. **Collect.** For each app, take the newest 1, 2 and 3 star reviews from the last 180 days, balanced across star levels so 1-star rants don't dominate. Raw reviews are saved to JSON.
3. **Group and rank.** Assign reviews to themes, then rank themes by distinct apps first, review count second.

Grouping has two modes:

| | Keywords (default) | `--llm` |
|---|---|---|
| Needs | nothing | your own `ANTHROPIC_API_KEY` |
| Themes | generic: price, export, crash, login, sync... | niche-specific, e.g. "OCR misreads faded receipts" |
| Cost | free | a few cents per report (Claude Haiku; tokens and cost printed in the report) |

## Design decisions

- **The classifier never counts.** Keyword rules or Claude only assign review ids to themes. App counts, review counts and quotes are computed in code from those ids; ids that don't exist are dropped. A model can't inflate a number in the report.
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
```

`--llm` reads `ANTHROPIC_API_KEY` from your environment. No key is stored in this repo.

Also included: `scan.py`, the scanner this project started from. It finds individual apps rated 3.0-3.8 across many categories and samples their complaints.

```bash
python scan.py --terms "expense tracker,invoice maker" --reviews 30
```

## Limits

- Google Play only, English reviews, US store by default.
- Keyword themes are generic and miss niche-specific problems; that is what `--llm` is for.
- Quiet niches give small samples. The report shows the counts so you can judge.
- Uses [google-play-scraper](https://github.com/JoMingyu/google-play-scraper), an unofficial client. Meant for personal research at low volume.

## Tests

```bash
python -m unittest -v
```

Tests don't touch the network: Google Play and Claude are mocked.

## License

MIT
