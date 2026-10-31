"""Offline integration checks for input filtering, delivery and recovery."""
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import job_bot as bot
from test_job_bot import listing
from test_employer_degree import page, URL


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(bot.time, "sleep"))
        self.stack.enter_context(patch.object(bot, "EMPLOYER_LOOKUP", False))

    def test_source_filters_keep_only_current_relevant_us_jobs(self):
        base = dict(id="ok", url="https://example.com", title="Intern", company_name="Example",
                    active=True, locations=["New York, NY"], category="Software", terms=["Summer 2027"])
        data = [base, {**base, "active": False}, {**base, "is_visible": False},
                {**base, "url": ""}, {**base, "category": "Quant"},
                {**base, "locations": ["London, UK"]}, {**base, "terms": ["Summer 2025"]}]
        stats = dict(category=0, non_us=0, expired_term=0)
        with patch.object(bot, "http_json", return_value=data):
            jobs = bot.fetch_source("internship", "test", date(2026, 10, 30), stats)
        self.assertEqual([j["id"] for j in jobs], ["ok"])
        self.assertEqual(stats, dict(category=1, non_us=1, expired_term=1))
        self.assertTrue(jobs[0]["is_local"])
        self.assertFalse(bot.is_us_location(""))
        self.assertTrue(bot.is_us_location("Remote"))
        self.assertTrue(bot.is_local_location("NYC"))
        self.assertTrue(bot.term_is_current(["N/A"], date(2026, 10, 30)))
        self.assertTrue(bot.term_is_current(["rolling"], date(2026, 10, 30)))
        self.assertIsNone(bot.display_category(None))
        with patch.multiple(bot, CATEGORY_BLOCKLIST={"software"}), patch.object(bot, "http_json", return_value=[base]):
            self.assertEqual(bot.fetch_source("internship", "test", date(2026, 10, 30), stats), [])

    def test_http_retries_transient_errors_but_not_missing_pages(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        for failure in (TimeoutError(), urllib.error.HTTPError("https://example.com", 503, "unavailable", {}, None)):
            with patch.object(bot.urllib.request, "urlopen", side_effect=[failure, response]) as fetch:
                self.assertEqual(bot.http_json("https://example.com"), {"ok": True})
                self.assertEqual(fetch.call_count, 2)
            with patch.object(bot.urllib.request, "urlopen", side_effect=failure):
                with self.assertRaises(type(failure)):
                    bot.http_text("https://example.com", retries=2)
        with patch.object(bot.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("https://example.com", 404, "gone", {}, None)) as fetch:
            with self.assertRaises(urllib.error.HTTPError):
                bot.http_text("https://example.com")
            self.assertEqual(fetch.call_count, 1)

    def test_source_discovery_and_fallback(self):
        with patch.object(bot, "http_json", return_value=[{"name":"Summer2026-Internships"}, {"name":"Summer2027-Internships"}, {"name":"other"}]):
            self.assertEqual(bot.discover_internship_repo(), "Summer2027-Internships")
        with patch.object(bot, "http_json", side_effect=TimeoutError()):
            self.assertEqual(bot.discover_internship_repo(), bot.FALLBACK_INTERNSHIP_REPO)

    def test_delivery_retries_and_preserves_undelivered_state(self):
        response = MagicMock()
        for failure in (TimeoutError(), urllib.error.HTTPError("https://example.com", 503, "error", {}, None),
                        urllib.error.HTTPError("https://example.com", 429, "rate limit", {}, io.BytesIO(b'{"retry_after":0}'))):
            with patch.object(bot.urllib.request, "urlopen", side_effect=[failure, response]) as send:
                bot.post_to_webhook("https://example.com?token=test", {"content":"test"})
                self.assertEqual(send.call_count, 2)
        with patch.object(bot.urllib.request, "urlopen", side_effect=TimeoutError()):
            with self.assertRaises(RuntimeError):
                bot.post_to_webhook("https://example.com", {})
        with patch.object(bot.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("https://example.com", 400, "bad", {}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                bot.post_to_webhook("https://example.com", {})
        job = listing("Intern", ["Bachelor's"])
        job.update(degree="bach", degree_src="feed")
        seen = {}
        with patch.object(bot, "DRY_RUN", False), patch.dict(bot.WEBHOOKS, bachelors="https://example.com"), patch.object(bot, "post_to_webhook", side_effect=RuntimeError()):
            with self.assertRaises(RuntimeError):
                bot.send_jobs("bachelors", [job], seen, {})
        self.assertNotIn(job["id"], seen)

    def test_legacy_ats_lookup_and_cache(self):
        fixtures = [
            ("https://jobs.lever.co/example/123", {"descriptionPlain":"Bachelor's degree required", "lists":[]}),
            ("https://boards.greenhouse.io/example/jobs/123", {"content":"<p>Bachelor's degree required</p>"}),
            ("https://jobs.ashbyhq.com/example/123", {"jobs":[{"id":"123", "descriptionPlain":"Bachelor's degree required"}]}),
        ]
        for url, payload in fixtures:
            with self.subTest(url=url), patch.object(bot, "http_json", return_value=payload):
                self.assertIn("Bachelor", bot.fetch_description(url, {}))
        with patch.object(bot, "http_json", side_effect=TimeoutError()):
            self.assertIsNone(bot.fetch_description(fixtures[0][0], {}))
        self.assertIsNone(bot.fetch_description("https://example.com", {}))
        self.assertIsNone(bot.degree_from_description("too short"))
        padding = "Build reliable software for our product teams. " * 6
        self.assertEqual(bot.degree_from_description(padding + "A Bachelor's degree is required."), "bach")
        self.assertEqual(bot.degree_from_description(padding + "A PhD degree is required."), "grad")
        self.assertIsNone(bot.degree_from_description(padding))
        job = listing("Intern", [])
        job["url"] = fixtures[0][0]
        cache = {}
        with patch.object(bot, "DEGREE_LOOKUP", True), patch.object(bot, "fetch_description", return_value=padding + "A PhD degree is required.") as fetch:
            bot.resolve_degrees([job], cache)
            self.assertEqual(job["degree"], "grad")
            bot.resolve_degrees([job], cache)
            self.assertEqual(fetch.call_count, 1)

    def test_selection_preserves_freshness_and_local_quota(self):
        jobs = [dict(listing("Intern", [], job_id=str(i)), date_posted=i, is_local=i < 5) for i in range(10)]
        selected = bot.select_to_post(jobs, 5)
        self.assertEqual({j["id"] for j in selected}, {"2", "3", "4", "8", "9"})
        self.assertEqual(len(bot.select_to_post(jobs[:5], 4)), 4)
        self.assertEqual(bot.select_to_post(jobs, 0), jobs)

    def test_state_migration_and_pruning(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "seen.json"
            path.write_text(json.dumps({"_headers":{"internship":"2026-10-30", "new_grad":"2026-10-29"}, "old":{"first_seen":"2020-01-01"}}))
            with patch.object(bot, "SEEN_FILE", str(path)):
                seen = bot.load_seen()
            self.assertEqual(seen["_headers"]["bachelors"], "2026-10-30")
            self.assertEqual(bot.prune_seen(seen, set(), date(2026, 10, 30)), 1)
            with patch.object(bot, "PRUNE_AFTER_DAYS", 0):
                self.assertEqual(bot.prune_seen(seen, set(), date(2026, 10, 30)), 0)

    def test_meta_end_to_end_and_run_modes(self):
        for mode in ("normal", "bootstrap", "backfill", "since", "days", "dry_run"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
                path = Path(temp) / "seen.json"
                if mode != "bootstrap":
                    path.write_text("{}")
                job = listing("Research Scientist Intern - Computer Vision - Video Intelligence", [], job_id="meta")
                job.update(url=URL, is_local=True, terms=["Summer 2027"], sponsorship="US Citizen", locations=["NYC"]*7)
                stack.enter_context(patch.multiple(bot, SEEN_FILE=str(path), DEGREE_CACHE_FILE=str(Path(temp)/"cache.json"),
                    WEBHOOKS={c:"https://example.com/"+c for c in bot.CHANNELS}, DRY_RUN=mode=="dry_run", DRY_RUN_SAVE=False,
                    BACKFILL_DAYS="all" if mode=="backfill" else "7" if mode=="days" else "",
                    BACKFILL_SINCE="2026-01-01" if mode=="since" else "", ONLY_CHANNELS={"grad"},
                    EMPLOYER_LOOKUP=True, MAX_EMPLOYER_LOOKUPS=40, DEGREE_LOOKUP=False, MAX_POSTS_PER_RUN=40))
                stack.enter_context(patch.object(bot, "discover_internship_repo", return_value="test"))
                stack.enter_context(patch.object(bot, "fetch_source", side_effect=[[job], []]))
                stack.enter_context(patch.object(bot.employer_degree, "fetch_public_text", return_value=page("Research Scientist Intern (PhD)")))
                post = stack.enter_context(patch.object(bot, "post_to_webhook"))
                self.assertEqual(bot.main(), 0)
                if mode=="dry_run":
                    post.assert_not_called()
                    self.assertEqual(path.read_text(), "{}")
                else:
                    self.assertEqual(post.call_count, 1)
                    self.assertEqual(post.call_args.args[0], "https://example.com/grad")
                    self.assertEqual(post.call_args.args[1]["flags"], bot.SUPPRESS_NOTIFICATIONS)
                    self.assertIn("meta", json.loads(path.read_text()))

    def test_invalid_configuration_and_empty_source_do_not_post(self):
        with patch.multiple(bot, WEBHOOKS={}, DRY_RUN=False):
            self.assertEqual(bot.main(), 1)
        for since, days in (("invalid", ""), ("", "invalid")):
            with patch.multiple(bot, DRY_RUN=True, BACKFILL_SINCE=since, BACKFILL_DAYS=days):
                self.assertEqual(bot.main(), 1)
        with patch.multiple(bot, DRY_RUN=True, BACKFILL_SINCE="", BACKFILL_DAYS=""), patch.object(bot, "discover_internship_repo", return_value="test"), patch.object(bot, "fetch_source", return_value=[]):
            self.assertEqual(bot.main(), 1)
