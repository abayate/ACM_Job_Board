import json
import unittest
from pathlib import Path
from unittest.mock import Mock

import employer_degree as employer


URL = "https://www.metacareers.com/jobs/1126899676481179"


def page(title, qualifications="", **extra):
    record = {"@type": "JobPosting", "title": title, "qualifications": qualifications, **extra}
    return '<script type="application/ld+json">' + json.dumps(record) + '</script>'


class EmployerDegreeTests(unittest.TestCase):
    def test_recorded_meta_employer_record(self):
        fixture = json.loads((Path(__file__).parent / "tests/fixtures/meta_1126899676481179.json").read_text())
        markup = '<script type="application/ld+json">' + json.dumps(fixture["record"]) + '</script>'
        result = employer.lookup(fixture["source_url"], {}, Mock(return_value=markup), now=100)
        self.assertEqual(result["degree"], fixture["expected_degree"])
        self.assertIn("Ph.D. degree", result["evidence"])

    def test_meta_case_missing_from_feed(self):
        fetch = Mock(return_value=page(
            "Research Scientist Intern, Computer Vision - Video Intelligence (PhD)",
            "Currently has or is in the process of obtaining a Ph.D. degree in Computer Science",
        ))
        result = employer.lookup(URL, {}, fetch, now=100)
        self.assertEqual(result["degree"], "grad")
        self.assertEqual(result["status"], "verified")
        self.assertIn("PhD", result["title"])
        self.assertEqual(fetch.call_args.args[0], "https://www.metacareers.com/profile/job_details/1126899676481179/")

    def test_eligibility_cases(self):
        cases = [
            ("Software Intern", "Currently pursuing a Ph.D. degree in computing", "grad"),
            ("Software Intern", "Must have a Master's degree", "grad"),
            ("Software Intern", "Bachelor's degree required. PhD preferred.", "bach"),
            ("Software Intern", "PhD preferred", None),
            ("Software Intern", "A PhD is not required", None),
            ("Software Intern", "No PhD degree required", None),
            ("Intern - PhD not required", "", None),
            ("Research Intern - BS/MS/PhD", "", "bach"),
            ("Research Intern - Bachelor's / PhD", "", "bach"),
            ("Pre-Doctoral Fellow", "", None),
            ("Research Intern", "Currently pursuing a Bachelor's, Master's, or PhD degree", "bach"),
            ("Software Engineering Intern", "Currently enrolled in a full-time, degree-seeking program and in the process of obtaining a Bachelors or Masters degree in computer science or a related field", "bach"),
            ("Software Intern", "Experience working with PhD researchers", None),
            ("Intern (Ph.D.)", "", "grad"),
        ]
        for title, qualifications, expected in cases:
            with self.subTest(title=title, qualifications=qualifications):
                result = employer.lookup(URL, {}, Mock(return_value=page(title, qualifications)), now=100)
                self.assertEqual(result["degree"], expected)

    def test_original_title_metadata_fallback(self):
        result = employer.lookup(URL, {}, Mock(return_value='<meta content="Research Intern (PhD)" property="og:title">'), now=100)
        self.assertEqual(result["degree"], "grad")

    def test_unsupported_and_invalid_urls_are_not_fetched(self):
        fetch = Mock()
        for url in ("https://example.com/jobs/1", "https://metacareers.com.evil.test/jobs/1",
                    "http://metacareers.com/jobs/1", "https://metacareers.com/jobs/search",
                    "https://metacareers.com:444/jobs/1", "https://user@metacareers.com/jobs/1"):
            with self.subTest(url=url):
                self.assertIsNone(employer.lookup(url, {}, fetch, now=100))
        fetch.assert_not_called()

    def test_success_is_cached_and_expires(self):
        cache = {}
        fetch = Mock(return_value=page("Intern (PhD)"))
        employer.lookup(URL, cache, fetch, now=100)
        employer.lookup(URL, cache, fetch, now=101)
        self.assertEqual(fetch.call_count, 1)
        employer.lookup(URL, cache, fetch, now=100 + employer.VERIFIED_TTL + 1)
        self.assertEqual(fetch.call_count, 2)

    def test_failure_is_retryable_and_does_not_guess(self):
        cache = {}
        fetch = Mock(side_effect=[TimeoutError(), page("Intern (PhD)")])
        result = employer.lookup(URL, cache, fetch, now=100)
        self.assertEqual(result["status"], "fetch_failed")
        self.assertIsNone(result["degree"])
        employer.lookup(URL, cache, fetch, now=101)
        self.assertEqual(fetch.call_count, 1)
        result = employer.lookup(URL, cache, fetch, now=100 + employer.RETRY_TTL + 1)
        self.assertEqual(result["degree"], "grad")

    def test_no_budget_uses_cache_but_does_not_fetch(self):
        fetch = Mock(return_value=page("Intern (PhD)"))
        cache = {}
        result = employer.lookup(URL, cache, fetch, now=100, allow_fetch=False)
        self.assertEqual(result["status"], "deferred")
        fetch.assert_not_called()
        employer.lookup(URL, cache, fetch, now=100)
        result = employer.lookup(URL, cache, fetch, now=101, allow_fetch=False)
        self.assertEqual(result["degree"], "grad")
        self.assertEqual(fetch.call_count, 1)

    def test_malformed_and_unrelated_pages_remain_unverified(self):
        for markup in ('<html>Login required</html>', '<script type="application/ld+json">invalid</script>',
                       '<script type="application/ld+json">{"@type":"Organization","title":"PhD"}</script>'):
            with self.subTest(markup=markup):
                result = employer.lookup(URL, {}, Mock(return_value=markup), now=100)
                self.assertEqual(result["status"], "unverified")
                self.assertIsNone(result["degree"])


if __name__ == "__main__":
    unittest.main()
