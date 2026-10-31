import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import job_bot as bot


def listing(title, degrees, kind="internship", job_id="job"):
    return {
        "id": job_id, "title": title, "degrees": degrees, "kind": kind,
        "company": "Example", "url": "https://example.com/job",
        "date_posted": time.time(), "is_local": False,
        "locations": ["New York, NY"], "terms": [], "category": "Software",
        "sponsorship": None, "source_name": "test",
    }


class DegreeRoutingTests(unittest.TestCase):
    def resolve(self, job, cache=None):
        with patch.object(bot, "DEGREE_LOOKUP", False), contextlib.redirect_stdout(io.StringIO()):
            bot.resolve_degrees([job], cache or {})
        return job

    def test_graduate_titles_override_conflicting_feed_and_cache(self):
        for title in (
            "Software Engineer Intern - MS/PhD", "PhD Studentship: Causal RL",
            "Research Intern - Ph.D.", "Research Intern - Ph. D.",
            "Doctoral Research Intern",
        ):
            for degrees in ([], ["Bachelor's"], ["Bachelor's", "Master's", "PhD"]):
                with self.subTest(title=title, degrees=degrees):
                    job = self.resolve(listing(title, degrees), {"job": "bach"})
                    self.assertEqual(job["degree"], "grad")
                    self.assertEqual(bot.route(job), "grad")
                    self.assertNotIn("Bachelor", bot.degree_display(job))

    def test_titles_open_to_undergraduates_keep_feed_classification(self):
        for title in (
            "Research Intern - BS/MS/PhD", "Research Intern - B.S./M.S./Ph.D.",
            "Research Intern - BSc/MSc/PhD", "Research Intern - Bachelor's/Master's/PhD",
            "Research Intern - Undergraduate or PhD", "Pre-Doctoral Fellow",
            "Predoctoral Fellow", "Pre Doctoral Fellow",
        ):
            with self.subTest(title=title):
                job = self.resolve(listing(title, ["Bachelor's", "Master's", "PhD"]))
                self.assertEqual(job["degree"], "bach")
                self.assertEqual(bot.route(job), "bachelors")
                untagged = self.resolve(listing(title, []))
                self.assertIsNone(untagged["degree"])
                self.assertEqual(bot.route(untagged), "bachelors")

    def test_unknown_and_mixed_degree_roles_remain_available(self):
        for kind, channel in (("internship", "bachelors"), ("new_grad", "newgrad")):
            for degrees, expected in (([], None), (["Bachelor's", "PhD"], "bach")):
                with self.subTest(kind=kind, degrees=degrees):
                    job = self.resolve(listing("Software Engineer", degrees, kind))
                    self.assertEqual(job["degree"], expected)
                    self.assertEqual(bot.route(job), channel)

    def test_graduate_routing_does_not_depend_on_webhook(self):
        for webhook in (None, "https://example.com/grad"):
            for kind in ("internship", "new_grad"):
                with self.subTest(webhook=webhook, kind=kind), patch.dict(bot.WEBHOOKS, {"grad": webhook}):
                    job = self.resolve(listing("Research Engineer", ["Master's", "PhD"], kind))
                    self.assertEqual(bot.route(job), "grad")

    def test_main_never_sends_graduate_roles_to_undergraduate_webhooks(self):
        for grad_webhook in (None, "https://example.com/grad"):
            with self.subTest(grad_webhook=grad_webhook), tempfile.TemporaryDirectory() as temp:
                jobs = [
                    listing("Software Engineer Intern - MS/PhD", ["Bachelor's"], job_id="grad"),
                    listing("Software Engineer Intern", ["Bachelor's"], job_id="bach"),
                ]
                seen_path = Path(temp) / "seen.json"
                seen_path.write_text("{}")
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.multiple(
                        bot, WEBHOOKS={"bachelors": "https://example.com/internships",
                                       "newgrad": "https://example.com/newgrad", "grad": grad_webhook},
                        SEEN_FILE=str(seen_path), DEGREE_CACHE_FILE=str(Path(temp) / "cache.json"),
                        DEGREE_LOOKUP=False, DRY_RUN=False, BACKFILL_DAYS="", BACKFILL_SINCE="",
                        ONLY_CHANNELS=set(), MAX_JOB_AGE_DAYS=7, MAX_POSTS_PER_RUN=40,
                        SLEEP_BETWEEN_MESSAGES=0,
                    ))
                    stack.enter_context(patch.object(bot, "discover_internship_repo", return_value="test"))
                    stack.enter_context(patch.object(bot, "fetch_source", side_effect=[jobs, []]))
                    post = stack.enter_context(patch.object(bot, "post_to_webhook"))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    self.assertEqual(bot.main(), 0)
                    by_webhook = {call.args[0]: call.args[1]["embeds"] for call in post.call_args_list}
                    intern_embeds = by_webhook["https://example.com/internships"]
                    self.assertEqual(len(intern_embeds), 1)
                    self.assertNotIn("PhD", intern_embeds[0]["title"])
                    self.assertEqual(len(post.call_args_list), 2 if grad_webhook else 1)
                    # Intentionally skipped roles must not consume the cap again next run.
                    self.assertIn("grad", bot.load_seen())


if __name__ == "__main__":
    unittest.main()
