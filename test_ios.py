import contextlib
import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import gaps
import ios

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
AUTHOR = "Jane Reviewer"


def entry(rid, rating, updated, content="Export is broken", title="Bad", version="2.1"):
    return {"id": {"label": rid}, "im:rating": {"label": str(rating)}, "updated": {"label": updated},
            "title": {"label": title}, "content": {"label": content}, "im:version": {"label": version},
            "author": {"name": {"label": AUTHOR}, "uri": {"label": "https://example.invalid/u/1"}}}


class FakeFeed:
    """Serves fixed pages keyed by (country, page); anything else is an empty page."""

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        country = url.split("/")[3]
        page = int(url.split("page=")[1].split("/")[0])
        value = self.pages.get((country, page), [])
        if isinstance(value, Exception):
            raise value
        return {"feed": {"entry": value}} if value != [] else {"feed": {}}


def fetch_reviews(pages, count=60, countries=("us",)):
    feed = FakeFeed(pages)
    reviews, errors = ios.get_reviews("123", count, days=180, countries=countries, now=NOW, fetch=feed, delay=0)
    return reviews, errors, feed


class IosTests(unittest.TestCase):
    def test_single_review_arrives_as_dict_and_author_is_never_kept(self):
        reviews, errors, _ = fetch_reviews({("us", 1): entry("1", 2, "2026-11-01T10:00:00-07:00")})
        self.assertEqual(errors, [])
        self.assertEqual(reviews, [{"review_id": "1", "score": 2, "text": "Bad: Export is broken",
                                    "at": "2026-11-01T17:00:00+00:00", "version": "2.1",
                                    "store": "ios", "country": "us"}])
        self.assertNotIn(AUTHOR, json.dumps(reviews))
        self.assertNotIn("author", json.dumps(reviews))

    def test_keeps_only_1_to_3_stars_inside_the_window(self):
        reviews, _, _ = fetch_reviews({("us", 1): [
            entry("1", 5, "2026-11-10T00:00:00+00:00"),
            entry("2", 1, "2026-11-09T00:00:00+00:00"),
            entry("3", 3, "2026-01-01T00:00:00+00:00"),  # older than 180 days
        ]})
        self.assertEqual([r["review_id"] for r in reviews], ["2"])

    def test_paging_stops_at_empty_page_and_at_the_window_edge(self):
        _, _, feed = fetch_reviews({("us", 1): [entry("1", 1, "2026-11-10T00:00:00+00:00")]})
        self.assertEqual(len(feed.urls), 2)  # page 2 is empty, so page 3 is never asked for
        _, errors, _ = fetch_reviews({})
        self.assertEqual(errors, ["us: review feed is empty"])  # an empty feed is reported, not read as zero
        _, _, feed = fetch_reviews({("us", 1): [entry("1", 1, "2026-01-01T00:00:00+00:00")],
                                    ("us", 2): [entry("2", 1, "2025-12-01T00:00:00+00:00")]})
        self.assertEqual(len(feed.urls), 1)

    def test_same_review_in_two_countries_counts_once(self):
        page = [entry("1", 1, "2026-11-10T00:00:00+00:00")]
        reviews, _, _ = fetch_reviews({("us", 1): page, ("gb", 1): page}, countries=("us", "gb"))
        self.assertEqual([(r["review_id"], r["country"]) for r in reviews], [("1", "us")])

    def test_failed_page_is_reported_and_not_retried(self):
        reviews, errors, feed = fetch_reviews({("us", 1): OSError("HTTP 403"),
                                               ("gb", 1): [entry("9", 3, "2026-11-10T00:00:00+00:00")]},
                                              countries=("us", "gb"))
        self.assertEqual(errors, ["us page 1: HTTP 403"])
        self.assertEqual(sum("/us/" in u for u in feed.urls), 1)
        self.assertEqual([r["review_id"] for r in reviews], ["9"])

    def test_sample_is_balanced_across_stars_newest_first(self):
        page = [entry(f"a{i}", 1, f"2026-11-{10 + i:02d}T00:00:00+00:00") for i in range(5)]
        page += [entry("b", 2, "2026-10-01T00:00:00+00:00"), entry("c", 3, "2026-10-02T00:00:00+00:00")]
        reviews, _, _ = fetch_reviews({("us", 1): page}, count=4)
        self.assertEqual([r["review_id"] for r in reviews], ["a4", "b", "c", "a3"])

    def test_search_maps_fields_and_min_ratings_replaces_installs(self):
        results = {"results": [
            {"trackId": 1, "trackName": "Big Scanner", "sellerName": "Big Co", "averageUserRating": 4.6,
             "userRatingCount": 38000, "price": 0, "currency": "USD", "version": "3.0"},
            {"trackId": 2, "trackName": "Tiny Scanner", "sellerName": "Solo Dev", "userRatingCount": 12, "price": 2.99},
        ]}
        with patch.object(ios, "fetch_json", return_value=results):
            apps = gaps.top_apps("receipt scanner", 5, store="ios", min_ratings=500)
        self.assertEqual([(a["appId"], a["store"], a["free"], a["ratings"]) for a in apps],
                         [("1", "ios", True, 38000)])

    def test_both_stores_merge_only_when_title_and_developer_agree(self):
        play = [{"appId": "com.acme.scan", "store": "play", "title": "Acme Scan: Receipts", "developer": "Acme Inc."},
                {"appId": "com.other", "store": "play", "title": "Receipt Keeper", "developer": "Other Ltd"}]
        apple = [{"appId": "11", "ios_id": "11", "store": "ios", "title": "Acme Scan - Expense", "developer": "Acme, Inc", "ratings": 900},
                 {"appId": "22", "ios_id": "22", "store": "ios", "title": "Receipt Keeper", "developer": "Someone Else", "ratings": 700}]
        merged = gaps.merge_stores(play, apple)
        self.assertEqual([(a["appId"], a["store"]) for a in merged],
                         [("com.acme.scan", "both"), ("com.other", "play"), ("22", "ios")])
        self.assertEqual(merged[0]["ios_id"], "11")

    def test_ios_report_names_the_store_and_never_the_reviewer(self):
        found = [{"appId": "1", "ios_id": "1", "store": "ios", "title": "Big Scanner", "developer": "Big Co",
                  "score": 4.6, "ratings": 38000, "installs": "", "free": True}]
        feed = FakeFeed({("gb", 1): [entry("1", 1, "2026-11-10T00:00:00+00:00")]})
        real = ios.get_reviews
        with patch.object(gaps.ios, "search", return_value=found), \
                patch.object(gaps.ios, "feed_is_up", return_value=True), \
                patch.object(gaps.scan, "search") as play_search, \
                patch.object(gaps.ios, "get_reviews",
                             side_effect=lambda *a: real(*a, now=NOW, fetch=feed, delay=0)), \
                contextlib.redirect_stdout(io.StringIO()):
            data = gaps.collect("receipt scanner", 5, 60, 180, 10_000, [], "ios", 500, ("us", "gb"))
        play_search.assert_not_called()
        report = gaps.report(data, gaps.rank(gaps.regex_themes(data), data), {})
        self.assertIn("on the App Store (us, gb). 1 apps, 1 reviews", report)
        self.assertIn("Big Scanner, 1 star, v2.1, App Store gb, 2026-11-10", report)
        self.assertIn("| Big Scanner | ios | 4.6 | 38K iOS ratings | free to download (IAP not listed) | 1 |", report)
        self.assertNotIn(AUTHOR, report + json.dumps(data))

    def test_feed_outage_skips_app_store_reviews_and_says_so(self):
        self.assertFalse(ios.feed_is_up(fetch=lambda url: {"feed": {}}))
        self.assertTrue(ios.feed_is_up(fetch=lambda url: {"feed": {"entry": [entry("1", 5, "2026-11-10T00:00:00+00:00")]}}))
        found = [{"appId": "1", "ios_id": "1", "store": "ios", "title": "Big Scanner", "developer": "Big Co",
                  "score": 4.6, "ratings": 38000, "installs": "", "free": True}]
        with patch.object(gaps.ios, "search", return_value=found), \
                patch.object(gaps.ios, "feed_is_up", return_value=False), \
                patch.object(gaps.ios, "get_reviews") as get_reviews, \
                contextlib.redirect_stdout(io.StringIO()):
            data = gaps.collect("receipt scanner", 5, 60, 180, 10_000, [], "ios", 500, ("us",))
        get_reviews.assert_not_called()
        self.assertEqual(data["apps"][0]["review_errors"], ["App Store review feed was down, reviews not fetched"])


if __name__ == "__main__":
    unittest.main()
