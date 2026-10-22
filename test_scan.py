import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import scan


class ScanTests(unittest.TestCase):
    def test_positive_and_foreign_reviews_are_not_assumed_bugs(self):
        self.assertEqual(scan.complaint_type("Works exactly how you'd expect it to."), "unclassified")
        self.assertEqual(scan.complaint_type("Очень хорошее приложение"), "unclassified")
        self.assertEqual(scan.complaint_type("My address is correct"), "unclassified")

    def test_price_and_mixed_reviews(self):
        self.assertEqual(scan.complaint_type("A $15 weekly charge is ridiculous"), "price/ads (hint)")
        self.assertEqual(scan.complaint_type("Cannot export after I paid"), "mixed (manual review)")
        self.assertEqual(scan.complaint_type("App crashes on startup"), "problem (hint)")

    def test_balanced_recent_sample_preserves_evidence(self):
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)

        def fetch(*args, filter_score_with, **kwargs):
            star = filter_score_with
            rows = [dict(reviewId=f"{star}-{i}", score=star, content="App crashes repeatedly",
                         at=now - timedelta(days=i), reviewCreatedVersion="2.1") for i in range(4)]
            rows += [dict(reviewId="old", content="Old crash", at=now - timedelta(days=181)),
                     dict(reviewId="undated", content="No date", at=None),
                     dict(reviewId="future", content="Future", at=now + timedelta(days=1))]
            return rows, None

        with patch.object(scan, "reviews", side_effect=fetch):
            items, errors = scan.get_reviews("example", 6, now=now)
        self.assertEqual(errors, [])
        self.assertEqual([r["score"] for r in items], [1, 2, 3, 1, 2, 3])
        self.assertTrue(all(r["version"] == "2.1" and r["at"] for r in items))
        self.assertEqual(scan.review_summary(items)["crash/startup"], 6)

    def test_fetch_failure_keeps_other_star_samples(self):
        now = datetime.now(timezone.utc)

        def fetch(*args, filter_score_with, **kwargs):
            if filter_score_with == 1:
                raise RuntimeError("temporary failure")
            return [dict(reviewId=str(filter_score_with), score=filter_score_with,
                         content="Cannot sync", at=now.replace(tzinfo=None))], None

        with patch.object(scan, "reviews", side_effect=fetch):
            items, errors = scan.get_reviews("example", 10, now=now)
        self.assertEqual(len(items), 2)
        self.assertIn("1-star", errors[0])

    def test_duplicate_ids_are_counted_once(self):
        row = dict(reviewId="same", score=1, content="Cannot sync", at=datetime.now(timezone.utc))
        with patch.object(scan, "reviews", return_value=([row, row], None)):
            items, _ = scan.get_reviews("example", 10)
        self.assertEqual(len(items), 1)

    def test_iap_is_not_payment_evidence(self):
        with patch.object(scan, "app", return_value={"free": True, "offersIAP": True}):
            detail = scan.get_app_details("example")
        self.assertTrue(detail["has_payment_option"])
        self.assertEqual(detail["payment_evidence"], "not verified")

    def test_main_preserves_explicit_filters_and_writes_honest_report(self):
        candidate = dict(title="Test | App\nName", score=3.5, installs="10,000+",
                         appId="example", developer="test", ratings=100)
        with tempfile.TemporaryDirectory() as output:
            argv = ["scan.py", "--revenue", "--min", "3.0", "--max", "3.8",
                    "--installs", "500", "--max-installs", "100000", "--output", output]
            with patch.object(scan.sys, "argv", argv), patch.object(scan, "scan", return_value=[candidate]) as search:
                with patch.object(scan, "get_app_details", return_value={"has_payment_option": True, "offersIAP": True}):
                    with patch.object(scan, "get_reviews", return_value=([], ["1-star: failed"])):
                        with contextlib.redirect_stdout(io.StringIO()):
                            scan.main()
            self.assertEqual(search.call_args.args[:4], (3.0, 3.8, 500, 100000))
            data = json.loads(next(Path(output).glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(data[0]["review_status"], "partial/error")
            self.assertEqual(data[0]["payment_evidence"], "not verified")
            report = next(Path(output).glob("*.md")).read_text(encoding="utf-8")
            self.assertIn("sales unverified", report)
            self.assertIn("Test / App Name", report)
            self.assertIn("Fetch error: 1-star: failed", report)

    def test_invalid_range_rejected_before_network(self):
        with patch.object(scan.sys, "argv", ["scan.py", "--min", "4", "--max", "3"]):
            with patch.object(scan, "scan") as search, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    scan.main()
                self.assertEqual(error.exception.code, 2)
                search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
