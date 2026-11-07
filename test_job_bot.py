import contextlib
import io
import json
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
    def setUp(self):
        patcher = patch.object(bot.employer_degree, "fetch_public_text", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_meta_employer_evidence_overrides_missing_or_wrong_feed(self):
        markup = '<script type="application/ld+json">{"@type":"JobPosting","title":"Research Scientist Intern, Computer Vision - Video Intelligence (PhD)"}</script>'
        for degrees in ([], ["Bachelor's"]):
            with self.subTest(degrees=degrees), patch.object(bot.employer_degree, "fetch_public_text", return_value=markup):
                job = listing("Research Scientist Intern - Computer Vision - Video Intelligence", degrees)
                job["url"] = "https://www.metacareers.com/jobs/1126899676481179"
                job = self.resolve(job, {"job": "unknown"})
                self.assertEqual(bot.route(job), "grad")
                self.assertEqual(job["degree_src"], "employer")

    def test_shared_employer_verification_changes_non_meta_routing(self):
        cases = [
            ("https://careers.example.org/job/research", '<h1>Research Intern (PhD)</h1>', [], "grad"),
            ("https://boards.greenhouse.io/example/jobs/123", '{"title":"Research Intern", "content":"<h2>Requirements</h2><p>PhD in computing</p>"}', ["Bachelor's"], "grad"),
            ("https://careers.example.org/job/software", '<h1>Software Intern</h1><h2>Requirements</h2><p>Bachelors degree</p><h2>Preferred Qualifications</h2><p>PhD</p>', [], "bachelors"),
        ]
        for url, body, degrees, expected in cases:
            with self.subTest(url=url), patch.object(bot.employer_degree, 'fetch_public_text', return_value=body):
                job = listing('Intern', degrees)
                job['url'] = url
                job = self.resolve(job, {'job':'unknown'})
                self.assertEqual(bot.route(job), expected)
                self.assertEqual(job['degree_src'], 'employer')

    def test_lookup_budget_is_shared_across_employers(self):
        jobs = [listing('Research Intern', [], job_id=str(i)) for i in range(2)]
        jobs[0]['url'] = 'https://careers.one.org/job/1'
        jobs[1]['url'] = 'https://careers.two.org/job/2'
        with patch.object(bot, 'MAX_EMPLOYER_LOOKUPS', 1), patch.object(bot.employer_degree, 'fetch_public_text', return_value='<h1>Research Intern (PhD)</h1>') as fetch, contextlib.redirect_stdout(io.StringIO()):
            bot.resolve_degrees(jobs, {})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(bot.route(jobs[0]), 'grad')
        self.assertEqual(jobs[1]['degree_evidence']['status'], 'deferred')
        self.assertIsNone(jobs[1]['degree'])

    def test_feed_title_fallback_does_not_turn_preference_into_requirement(self):
        for title in ('Research Intern - PhD preferred', 'Research Intern - PhD not required', 'Research Intern - PhD desired'):
            with self.subTest(title=title):
                job = self.resolve(listing(title, []))
                self.assertIsNone(job['degree'])
                self.assertEqual(bot.route(job), 'bachelors')

    def test_recorded_toyota_posting_overrides_incorrect_bachelors_tag(self):
        fixture = json.loads((Path(__file__).parent / 'tests/fixtures/toyota_lever.json').read_text())
        job = listing('Accelerated Science Intern - Representation Learning for Molecules and Materials', ["Bachelor's"])
        job['url'] = fixture['source_url']
        with patch.object(bot.employer_degree, 'fetch_public_text', return_value=json.dumps(fixture['response'])):
            job = self.resolve(job)
        self.assertEqual(bot.route(job), 'grad')
        self.assertIn('doctoral program', job['degree_evidence']['evidence'])

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
