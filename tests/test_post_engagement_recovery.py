import unittest
from unittest.mock import Mock, patch

from outbound.engagement import browser as browser_module
from scripts import post_engagement as pe


class RecoveryTests(unittest.TestCase):
    def test_navigation_timeout_after_arrival_uses_readiness(self):
        url = "https://www.linkedin.com/in/example/"
        cdp = Mock()
        cdp.navigate.side_effect = TimeoutError("command timeout")
        cdp.evaluate.return_value = url
        with (
            patch("linkedin_helper._wait_for_linkedin_ready", return_value={"ready": True}),
            patch.object(browser_module, "append_history"),
        ):
            pe._navigate(cdp, url)
        self.assertTrue(cdp.post_engagement_navigation[-1]["ok"])
        self.assertEqual(cdp.navigate.call_args.kwargs["wait_load"], False)
        cdp.create_page_target.assert_not_called()

    def test_wrong_destination_uses_navigation_fallback(self):
        url = "https://www.linkedin.com/in/example/"
        cdp = Mock()
        cdp.navigate.side_effect = TimeoutError("command timeout")
        cdp.evaluate.side_effect = ["https://www.linkedin.com/feed/", None, url]
        with (
            patch("linkedin_helper._wait_for_linkedin_ready", return_value={"ready": True}),
            patch.object(browser_module, "append_history"),
        ):
            pe._navigate(cdp, url)
        self.assertEqual(cdp.post_engagement_navigation[0]["fallback"], "js_location")

    def test_unhydrated_activity_does_not_pass_readiness(self):
        url = "https://www.linkedin.com/in/example/recent-activity/all/"
        cdp = Mock()
        cdp.navigate.return_value = {}
        cdp.evaluate.return_value = url
        with (
            patch("linkedin_helper._wait_for_linkedin_ready", return_value={"ready": True}),
            patch("linkedin_helper._wait_for_activity_feed_state", return_value={"ready": False}),
            patch.object(browser_module, "append_history"),
        ):
            with self.assertRaisesRegex(RuntimeError, "activity_feed_not_hydrated"):
                pe._navigate(cdp, url)
        self.assertEqual(cdp.navigate.call_count, 3)

    def test_extraction_continues_past_acceptance_floor(self):
        def snapshot(n):
            return {
                "success": True,
                "expected": 100,
                "profiles": [{"url": f"https://www.linkedin.com/in/p{i}/"} for i in range(n)],
                "scroller": {"left": 0, "top": 0, "width": 400, "height": 400},
            }

        responses = (
            [{"success": True, "expected": 100}, snapshot(90), snapshot(97)]
            + [snapshot(97)] * 5
            + [True]
        )
        campaign = {
            "day": "2026-09-06",
            "candidates": [],
            "sources": [{"submitted_url": "https://www.linkedin.com/posts/example"}],
        }
        with (
            patch.object(browser_module, "_navigate"),
            patch.object(browser_module, "_evaluate_json", side_effect=responses),
            patch.object(browser_module, "save_campaign"),
            patch.object(browser_module.time, "sleep"),
        ):
            pe.collect_sources(Mock(), campaign, pe.DEFAULT_CONFIG)
        source = campaign["sources"][0]
        self.assertEqual(source["profiles_collected"], 97)
        self.assertEqual(source["stop_reason"], "stagnant")
        self.assertEqual(source["stagnant_passes"], 5)
        self.assertEqual(source["status"], "collected")


if __name__ == "__main__":
    unittest.main()
