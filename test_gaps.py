import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gaps


def review(ref, text, at="2026-10-01T00:00:00+00:00", score=1, version="1.0"):
    return {"ref": ref, "text": text, "at": at, "score": score, "version": version}


def sample():
    return {"niche": "receipt scanner", "observed_at": "2026-10-22T00:00:00+00:00", "review_days": 180,
            "locale": {"lang": "en", "country": "us"},
            "apps": [
                {"appId": "a", "title": "App A", "score": 4.5, "installs": "10,000+", "offersIAP": True,
                 "reviews": [review("r1", "Export to CSV is broken", "2026-10-02T00:00:00+00:00"),
                             review("r2", "Subscription is too expensive"),
                             review("r3", "Lovely colours")]},
                {"appId": "b", "title": "App B", "score": 3.9, "installs": "50,000+", "free": True,
                 "reviews": [review("r4", "Can't export my receipts, and the $9 price is a joke", version=None)]},
            ]}


class FakeClient:
    def __init__(self, themes, stop_reason="end_turn"):
        parsed = SimpleNamespace(themes=[SimpleNamespace(model_dump=lambda t=t: t) for t in themes])
        self.response = SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason,
                                        usage=SimpleNamespace(input_tokens=20_000, output_tokens=2_000))
        self.messages = self
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class GapsTests(unittest.TestCase):
    def test_counts_come_from_ids_not_from_the_classifier(self):
        themes = [{"label": "export", "description": "", "refs": ["r1", "r4", "r4", "r99"]},
                  {"label": "price", "description": "", "refs": ["r2"]},
                  {"label": "ghost", "description": "", "refs": ["r404"]}]
        ranked = gaps.rank(themes, sample())
        self.assertEqual([t["label"] for t in ranked], ["export", "price"])
        self.assertEqual((len(ranked[0]["apps"]), ranked[0]["reviews"]), (2, 2))
        # one quote per app, newest first
        self.assertEqual([r["ref"] for _, r in ranked[0]["quotes"]], ["r1", "r4"])

    def test_more_apps_outranks_more_reviews_and_catch_all_goes_last(self):
        themes = [{"label": "one app, many reviews", "description": "", "refs": ["r1", "r2", "r3"]},
                  {"label": "catch-all", "description": "", "refs": ["r1", "r4"], "catch_all": True},
                  {"label": "two apps", "description": "", "refs": ["r2", "r4"]}]
        labels = [t["label"] for t in gaps.rank(themes, sample())]
        self.assertEqual(labels, ["two apps", "one app, many reviews", "catch-all"])

    def test_regex_themes_tag_price_and_problems(self):
        themes = {t["label"]: t["refs"] for t in gaps.regex_themes(sample())}
        self.assertEqual(sorted(themes["export"]), ["r1", "r4"])
        self.assertEqual(sorted(themes["price/ads"]), ["r2", "r4"])
        self.assertNotIn("r3", sum(themes.values(), []))

    def test_llm_cost_and_prompt(self):
        client = FakeClient([{"label": "CSV export broken", "description": "Exports fail.", "refs": ["r1", "r4"]}])
        themes, method = gaps.llm_themes(sample(), client)
        self.assertEqual(themes[0]["refs"], ["r1", "r4"])
        self.assertEqual(method["cost_usd"], round(20_000 * 0.10 / 1e6 + 2_000 * 0.50 / 1e6, 4))
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertIn("r4: 1: Can't export my receipts", prompt)
        self.assertEqual(client.calls[0]["model"], "claude-haiku-5-5")

    def test_llm_refusal_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            gaps.llm_themes(sample(), FakeClient([], stop_reason="refusal"))

    def test_top_apps_keeps_search_order_and_filters(self):
        hits = [dict(appId="com.rewards", title="Cash Rewards", installs="10,000,000+", score=4.7),
                dict(appId="small", title="Tiny", installs="500+", score=3.0),
                dict(appId="b", title="B", installs="10,000+", score=4.8),
                dict(appId="a", title="A", installs="1,000,000+", score=4.2)]
        with patch.object(gaps.scan, "search", return_value=hits):
            apps = gaps.top_apps("receipt scanner", 5, exclude=["rewards"])
        self.assertEqual([a["appId"] for a in apps], ["b", "a"])

    def test_report_from_saved_reviews_without_network(self):
        with tempfile.TemporaryDirectory() as out:
            path = Path(out) / "receipt.json"
            path.write_text(json.dumps(sample()), encoding="utf-8")
            with patch("sys.argv", ["gaps.py", "--input", str(path), "--output", out]):
                with patch.object(gaps.scan, "search") as search, contextlib.redirect_stdout(io.StringIO()):
                    gaps.main()
            search.assert_not_called()
            report = (Path(out) / "receipt.md").read_text(encoding="utf-8")
        self.assertIn("2 apps, 4 reviews", report)
        self.assertIn("| export | 2/2 | 2 |", report)
        self.assertIn("25% of reviews matched no theme", report)
        self.assertIn("App B, 1 star, 2026-10-01", report)  # unknown version omitted
        self.assertIn("No sales or demand data", report)


if __name__ == "__main__":
    unittest.main()
