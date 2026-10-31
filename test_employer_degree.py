import json
import unittest
import io
import urllib.error
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

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
        for url in ("https://localhost/jobs/1", "https://127.0.0.1/jobs/1",
                    "http://metacareers.com/jobs/1", "https://[::1]/jobs/1",
                    "https://metacareers.com:444/jobs/1", "https://user@metacareers.com/jobs/1"):
            with self.subTest(url=url):
                self.assertIsNone(employer.lookup(url, {}, fetch, now=100))
        fetch.assert_not_called()

    def test_any_public_employer_can_supply_structured_job_evidence(self):
        for url in ("https://careers.example.org/jobs/1", "https://jobs.another-company.org/opening/abc"):
            with self.subTest(url=url):
                result = employer.lookup(url, {}, Mock(return_value=page("Research Intern (PhD)")), now=100)
                self.assertEqual(result["degree"], "grad")

    def test_description_sections_separate_required_and_preferred_degrees(self):
        cases = [
            ('<h2>Minimum Qualifications</h2><ul><li>PhD in computer science</li></ul>', 'grad'),
            ('<h2>Requirements</h2><p>Bachelor’s degree in computing</p><h2>Preferred Qualifications</h2><p>PhD in AI</p>', 'bach'),
            ('<h2>Preferred Qualifications</h2><p>PhD in AI</p>', None),
            ('<h2>Requirements</h2><p>Currently pursuing a BS, MS or PhD in computing</p>', 'bach'),
            ('<h2>Minimum Qualifications</h2><p>PhD or equivalent practical experience</p>', None),
        ]
        for description, expected in cases:
            with self.subTest(description=description):
                result = employer.lookup('https://careers.example.org/job/1', {}, Mock(return_value=page('Research Intern', description=description)), now=100)
                self.assertEqual(result['degree'], expected)

    def test_common_ats_providers_use_original_titles_and_requirements(self):
        cases = [
            ('https://job-boards.greenhouse.io/example/jobs/123',
             {'title':'Research Intern', 'content':'<h2>Requirements</h2><p>PhD in computing</p>'}),
            ('https://jobs.lever.co/example/abc',
             {'text':'Research Intern', 'lists':[{'text':'Requirements', 'content':'<li>PhD in computing</li>'}]}),
            ('https://jobs.ashbyhq.com/example/abc/application',
             {'jobs':[{'id':'unrelated','title':'BS intern'}, {'id':'abc','title':'Research Intern (PhD)', 'descriptionHtml':''}]}),
            ('https://jobs.smartrecruiters.com/Example/123-research-intern',
             {'name':'Research Intern', 'jobAd':{'sections':{'qualifications':{'title':'Qualifications','text':'<p>PhD in computing</p>'}}}}),
            ('https://example.wd5.myworkdayjobs.com/en-US/Careers/job/US/Intern_123',
             {'jobPostingInfo':{'title':'Research Intern (PhD)', 'jobDescription':''}}),
        ]
        for url, record in cases:
            with self.subTest(url=url):
                fetch = Mock(return_value=json.dumps(record))
                result = employer.lookup(url, {}, fetch, now=100)
                self.assertEqual(result['degree'], 'grad')

    def test_ambiguous_multiple_job_records_do_not_choose_a_random_degree(self):
        records = [{'@type':'JobPosting','title':'Research Intern (PhD)'},
                   {'@type':'JobPosting','title':'Undergraduate Intern'}]
        markup = '<script type="application/ld+json">' + json.dumps(records) + '</script>'
        result = employer.lookup('https://careers.example.org/job/1', {}, Mock(return_value=markup), now=100)
        self.assertIsNone(result['degree'])

    def test_public_fetch_retries_and_limits_response_size(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'original posting'
        opener = Mock()
        opener.open.side_effect = [TimeoutError(), response]
        with patch.object(employer, '_check_public_destination'), patch.object(employer.urllib.request, 'build_opener', return_value=opener), patch.object(employer.time, 'sleep'):
            self.assertEqual(employer.fetch_public_text('https://example.org/job/1'), 'original posting')
            self.assertEqual(opener.open.call_count, 2)
            response.__enter__.return_value.read.return_value = b'x' * (4 * 1024 * 1024 + 1)
            opener.open.side_effect = None
            opener.open.return_value = response
            with self.assertRaises(ValueError):
                employer.fetch_public_text('https://example.org/job/1')
            opener.open.side_effect = urllib.error.HTTPError('https://example.org', 404, 'gone', {}, io.BytesIO())
            with self.assertRaises(urllib.error.HTTPError):
                employer.fetch_public_text('https://example.org/job/1')

    def test_private_dns_and_redirects_are_rejected(self):
        with patch.object(employer.socket, 'getaddrinfo', return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError):
                employer._check_public_destination('https://careers.example.org/job/1')
        with patch.object(employer.socket, 'getaddrinfo', return_value=[(2,1,6,'',('8.8.8.8',443))]):
            employer._check_public_destination('https://careers.example.org/job/1')
        request = employer.urllib.request.Request('https://careers.example.org/job/1')
        with self.assertRaises(ValueError):
            employer._PublicRedirect().redirect_request(request, None, 302, 'redirect', {}, 'http://127.0.0.1/admin')

    def test_qualification_sections_do_not_leak_into_other_sections(self):
        markup = '<h1>Software Intern</h1><nav>Must have PhD</nav><h2>Requirements</h2><p>Bachelor’s degree</p><h2>About our team</h2><p>Our colleagues have PhDs.</p><footer>PhD required</footer>'
        self.assertEqual(employer.parse_page(markup)[1], 'bach')
        graph = '<script type="application/ld+json">{"@graph":[{"@type":["Thing","JobPosting"],"title":"Intern","educationRequirements":"PhD degree"}]}</script>'
        self.assertEqual(employer.parse_page(graph)[1], 'grad')

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
