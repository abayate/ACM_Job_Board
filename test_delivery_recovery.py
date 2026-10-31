"""Run the real pipeline against temporary state and simulated Discord failures."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import job_bot as bot
from test_job_bot import listing


class DeliveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.seen = self.temp / "seen.json"
        self.cache = self.temp / "cache.json"
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch("socket.socket", side_effect=AssertionError("Tests must stay offline")))
        self.stack.enter_context(patch.object(bot.time, "sleep"))
        self.stack.enter_context(patch.object(bot, "discover_internship_repo", return_value="test"))
        self.stack.enter_context(patch.multiple(
            bot, SEEN_FILE=str(self.seen), DEGREE_CACHE_FILE=str(self.cache),
            WEBHOOKS={c: "https://example.org/" + c for c in bot.CHANNELS},
            DRY_RUN=False, DRY_RUN_SAVE=False, BACKFILL_DAYS="", BACKFILL_SINCE="",
            ONLY_CHANNELS=set(), EMPLOYER_LOOKUP=True, MAX_EMPLOYER_LOOKUPS=40,
            DEGREE_LOOKUP=False, BOOTSTRAP_POST_COUNT=7, MAX_POSTS_PER_RUN=40,
            MAX_JOB_AGE_DAYS=7, EMBEDS_PER_MESSAGE=5,
        ))
        self.stack.enter_context(patch.object(bot.employer_degree, "fetch_public_text", return_value="<h1>Intern</h1><h2>Requirements</h2><p>Bachelor's degree</p>"))
        self.jobs = [listing("Intern " + str(i), ["PhD"], job_id=str(i)) for i in range(9)]
        for i, job in enumerate(self.jobs):
            job["date_posted"] -= 60 * (9 - i)
            job["url"] = "https://careers.example.org/jobs/" + str(i)

    def run_bot(self, delivery):
        # Fresh input objects on each run, as with real source downloads.
        with patch.object(bot, "fetch_source", side_effect=[copy.deepcopy(self.jobs), []]), patch.object(bot, "post_to_webhook", side_effect=delivery) as post:
            result = bot.main()
        return result, post

    def test_failed_deliveries_remain_retryable_in_all_run_modes(self):
        for mode in ("normal", "bootstrap", "backfill", "since", "days"):
            for partial in (False, True):
                with self.subTest(mode=mode, partial=partial):
                    self.seen.unlink(missing_ok=True)
                    self.cache.unlink(missing_ok=True)
                    if mode != "bootstrap":
                        self.seen.write_text("{}")
                    config = {
                        "BACKFILL_DAYS": "all" if mode == "backfill" else "7" if mode == "days" else "",
                        "BACKFILL_SINCE": "2020-01-01" if mode == "since" else "",
                    }
                    selected = self.jobs[2:] if mode == "bootstrap" else self.jobs
                    delivered = selected[:5] if partial else []
                    seeded = self.jobs[:2] if mode == "bootstrap" else []
                    calls = iter([None, RuntimeError("Discord unavailable")] if partial else [RuntimeError("Discord unavailable")])

                    def failing_delivery(url, payload):
                        self.assertEqual(url, "https://example.org/bachelors")
                        outcome = next(calls)
                        if outcome:
                            raise outcome

                    with patch.multiple(bot, **config):
                        with self.assertRaisesRegex(RuntimeError, "Discord unavailable"):
                            self.run_bot(failing_delivery)
                    state = json.loads(self.seen.read_text())
                    self.assertEqual(set(state) - {"_headers"}, {j["id"] for j in delivered + seeded})
                    self.assertEqual(len(json.loads(self.cache.read_text())), len(selected))
                    missing = {j["url"] for j in selected if j not in delivered}

                    # A subsequent scheduled run retries just the unsent roles.
                    result, post = self.run_bot(None)
                    self.assertEqual(result, 0)
                    resent = [embed["url"] for call in post.call_args_list for embed in call.args[1]["embeds"]]
                    self.assertEqual(set(resent), missing)
                    self.assertEqual(len(resent), len(missing))
                    self.assertEqual(set(json.loads(self.seen.read_text())) - {"_headers"}, {j["id"] for j in self.jobs})

    def test_dry_run_preserves_existing_state_and_cache_bytes(self):
        self.seen.write_text('{"_headers": {}}\n')
        self.cache.write_text('{"historical": "unknown"}\n')
        before_seen, before_cache = self.seen.read_bytes(), self.cache.read_bytes()
        with patch.object(bot, "DRY_RUN", True):
            result, post = self.run_bot(None)
        self.assertEqual(result, 0)
        post.assert_not_called()
        self.assertEqual(self.seen.read_bytes(), before_seen)
        self.assertEqual(self.cache.read_bytes(), before_cache)

    def test_dry_run_first_run_creates_no_state_files(self):
        with patch.object(bot, "DRY_RUN", True):
            result, post = self.run_bot(None)
        self.assertEqual(result, 0)
        post.assert_not_called()
        self.assertFalse(self.seen.exists())
        self.assertFalse(self.cache.exists())

    def test_source_failure_cannot_modify_state_or_send_anything(self):
        self.seen.write_text('{"existing": {"first_seen": "2026-10-30"}}')
        self.cache.write_text("{}")
        before = self.seen.read_bytes()
        with patch.object(bot, "fetch_source", side_effect=TimeoutError()), patch.object(bot, "post_to_webhook") as post:
            with self.assertRaises(TimeoutError):
                bot.main()
        post.assert_not_called()
        self.assertEqual(self.seen.read_bytes(), before)
        self.assertEqual(self.cache.read_text(), "{}")

    def test_atomic_write_failure_preserves_previous_valid_state(self):
        self.seen.write_text('{"existing": {}}')
        with patch.object(bot.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                bot._save_json(str(self.seen), {"new": {}}, "test")
        self.assertEqual(json.loads(self.seen.read_text()), {"existing": {}})

    def test_backfill_of_already_seen_roles_is_an_intentional_repost(self):
        self.seen.write_text(json.dumps({j["id"]: bot.seen_entry(j) for j in self.jobs}))
        with patch.object(bot, "BACKFILL_DAYS", "all"):
            result, post = self.run_bot(None)
        self.assertEqual(result, 0)
        self.assertEqual(sum(len(call.args[1]["embeds"]) for call in post.call_args_list), len(self.jobs))

    def test_unavailable_employer_page_keeps_failure_visible_in_resolver(self):
        self.seen.write_text("{}")
        # An incomplete response must not abort the other listings in the run.
        with patch.object(bot.employer_degree, "fetch_public_text", side_effect=TimeoutError()):
            result, post = self.run_bot(None)
        self.assertEqual(result, 0)
        self.assertTrue(post.called)
        # Existing PhD feed tags still protect these jobs when verification fails.
        self.assertTrue(all(call.args[0] == "https://example.org/grad" for call in post.call_args_list))
        self.assertTrue(all(v["status"] == "fetch_failed" for v in json.loads(self.cache.read_text()).values()))
