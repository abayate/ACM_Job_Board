"""Degree eligibility contracts, independent of the recorded audit examples."""
import contextlib
import http.client
import io
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

import employer_degree as employer
import job_bot as bot
from test_employer_degree import page
from test_job_bot import listing


class ReleaseBoundaryTests(unittest.TestCase):
    def test_required_degree_logic_across_markup_and_providers(self):
        cases = [
            ("Bachelor's degree in computing or a related field and enrollment in a master's program", "grad"),
            ("Bachelor's degree and current enrollment in a master's program", "grad"),
            ("Master's degree and a bachelor's degree in computing", "grad"),
            ("Bachelor's degree or a master's degree in computing", "bach"),
            ("Master's degree or a bachelor's degree in computing", "bach"),
            ("Currently pursuing BS/MS/PhD in computing", "bach"),
            ("Currently pursuing a Bachelor's degree OR currently pursuing a Master's degree", "bach"),
            ("Bachelor's degree and/or master's degree", "bach"),
            ("Currently pursuing an M.S. in Computer Science", "grad"),
            ("Currently pursuing an M.A. in Statistics", "grad"),
            ("Currently pursuing an MS", "grad"),
            ("Must work in Boston, MA", None),
            ("Required work location: Jackson, MS.", None),
            ("Enrolled in a graduate program", "grad"),
            ("Must be a graduate student", "grad"),
            ("Currently pursuing a Doctorate degree", "grad"),
            ("Currently enrolled in an MBA program", "grad"),
            ("Able to master these workflows and use MS Office", None),
            ("Must master the art of debugging", None),
            ("Bachelor's degree required and a PhD is preferred", "bach"),
            ("PhD preferred, Bachelor's degree required", "bach"),
            ("PhD required and Python experience preferred", "grad"),
            ("PhD in math and computer science preferred", None),
            ("PhD not required and a Bachelor's degree is required", "bach"),
            ("PhD is not mandatory", None),
            ("Bachelor's degree or Master's degree preferred", None),
            ("PhD or equivalent professional experience", "equivalent"),
            ("Equivalent research experience or PhD", "equivalent"),
            ("PhD or an equivalent track record of research", "equivalent"),
        ]
        # The same wording must mean the same thing regardless of ATS or
        # whether the employer publishes HTML or structured qualifications.
        for requirement, expected in cases:
            markup = '<h2>Requirements</h2><p>' + requirement + '</p>'
            formats = [
                ("html", '<h1>Intern</h1>' + markup),
                ("html", page("Intern", educationRequirements=markup)),
                ("greenhouse", json.dumps({"title": "Intern", "content": markup})),
                ("lever", json.dumps({"text": "Intern", "lists": [{"text": "Requirements", "content": "<li>" + requirement + "</li>"}]})),
                ("workday", json.dumps({"jobPostingInfo": {"title": "Intern", "jobDescription": markup}})),
            ]
            for provider, raw in formats:
                with self.subTest(requirement=requirement, provider=provider, structured="ld+json" in raw):
                    self.assertEqual(employer.parse_response(provider, raw)[1], expected)

    def test_separate_mandatory_conditions_win_over_degree_alternatives(self):
        for first in ("Bachelor's or Master's degree", "PhD or equivalent experience", "Bachelor's degree"):
            for second in ("Must be admitted to a graduate degree program", "Currently pursuing a doctoral degree"):
                with self.subTest(first=first, second=second):
                    markup = '<h2>Required Education and/or Experience</h2><ul><li>' + first + '</li><li>' + second + '</li></ul>'
                    self.assertEqual(employer.parse_page(markup)[1], "grad")

    def test_inline_spacing_does_not_change_degree_alternatives(self):
        for space in (" ", "&nbsp;", "\u00a0", "<span>&nbsp;</span>"):
            with self.subTest(space=space):
                markup = "<h2>Requirements</h2><p>Currently pursuing a bachelor's" + space + "or master's degree</p>"
                self.assertEqual(employer.parse_page(markup)[1], "bach")

    def test_labeled_pathways_are_alternatives(self):
        markup = """<h2>Basic Qualifications</h2>
        <p>Undergraduate: Currently pursuing a bachelor's degree.</p>
        <p>Graduate: Currently pursuing a master's degree.</p>
        <p>Graduate + PhD: Currently pursuing a PhD.</p>"""
        self.assertEqual(employer.parse_page(markup)[1], "bach")
        self.assertEqual(employer.parse_page(markup + '<p>Security: All applicants must be admitted to a graduate program.</p>')[1], "grad")

    def test_conditional_graduate_pathway_does_not_exclude_current_undergraduates(self):
        markup = """<h2>Qualifications</h2><p>Working towards a Bachelor's degree.</p>
        <p>Applicants who have graduated must be entering a master's program.</p>"""
        self.assertEqual(employer.parse_page(markup)[1], "bach")

    def test_preferred_heading_ends_at_next_required_section(self):
        markup = """<h2>Who You Are (Basic Qualifications)</h2><p>Bachelor's degree.</p>
        <h2>What Will Put You Ahead</h2><p>PhD in computing.</p>"""
        self.assertEqual(employer.parse_page(markup)[1], "bach")
        self.assertEqual(employer.parse_page(markup + "<h2>Additional Required Qualifications</h2><p>Graduate school admission.</p>")[1], "grad")

    def test_experience_alternative_overrides_conflicting_feed_without_claiming_bachelors_ok(self):
        for kind, channel in (("internship", "bachelors"), ("new_grad", "newgrad")):
            job = listing("Researcher (PhD)", ["PhD"], kind)
            markup = "<h1>Researcher (PhD)</h1><h2>Requirements</h2><p>PhD or equivalent research experience</p>"
            with self.subTest(kind=kind), patch.object(bot, "EMPLOYER_LOOKUP", True), patch.object(bot.employer_degree, "fetch_public_text", return_value=markup), contextlib.redirect_stdout(io.StringIO()):
                bot.resolve_degrees([job], {})
                self.assertEqual(bot.route(job), channel)
                self.assertEqual(job["degree"], "equivalent")
                self.assertEqual(job["degree_src"], "employer")
                self.assertIn("equivalent experience", bot.degree_display(job))
                self.assertNotIn("Bachelor's OK", bot.degree_display(job))

    def test_previous_parser_cache_cannot_override_current_evidence(self):
        url = "https://careers.example.org/job/123"
        old = {"degree": "grad", "status": "verified", "expires_at": 99999999}
        cache = {"employer:v2:" + url: old}
        fetch = Mock(return_value="<h1>Intern</h1><h2>Requirements</h2><p>Bachelor's degree</p>")
        result = employer.lookup(url, cache, fetch, now=100)
        self.assertEqual(result["degree"], "bach")
        fetch.assert_called_once()
        self.assertEqual(cache["employer:v2:" + url], old)

    def test_old_legacy_cache_is_ignored(self):
        job = listing("Intern", [])
        cache = {job["id"]: "grad"}
        with patch.multiple(bot, EMPLOYER_LOOKUP=False, DEGREE_LOOKUP=False), contextlib.redirect_stdout(io.StringIO()):
            bot.resolve_degrees([job], cache)
        self.assertIsNone(job["degree"])

    def test_legacy_retrieval_preserves_required_and_preferred_sections(self):
        padding = "Build and maintain reliable software for our product teams. " * 5
        markup = padding + "<h2>Requirements</h2><p>Bachelor's degree</p><h2>Preferred Qualifications</h2><p>PhD</p>"
        fixtures = [
            ("https://boards.greenhouse.io/example/jobs/123", {"content": markup}),
            ("https://jobs.lever.co/example/123", {"descriptionPlain": padding, "lists": [
                {"text": "Requirements", "content": "<li>Bachelor's degree</li>"},
                {"text": "Preferred Qualifications", "content": "<li>PhD</li>"}]}),
            ("https://jobs.ashbyhq.com/example/123", {"jobs": [{"id": "123", "descriptionHtml": markup}]}),
        ]
        for url, payload in fixtures:
            with self.subTest(url=url), patch.object(bot, "http_json", return_value=payload):
                description = bot.fetch_description(url, {})
                self.assertEqual(bot.degree_from_description(description), "bach")

    def test_incomplete_employer_response_retries_then_recovers(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"<h1>Intern (PhD)</h1>"
        for failure in (http.client.IncompleteRead(b"partial"), http.client.BadStatusLine("bad")):
            opener = Mock()
            opener.open.side_effect = [failure, response]
            with self.subTest(failure=type(failure)), patch.object(employer, "_check_public_destination"), patch.object(employer.urllib.request, "build_opener", return_value=opener), patch.object(employer.time, "sleep"):
                result = employer.lookup("https://careers.example.org/job/1", {})
                self.assertEqual(result["degree"], "grad")
                self.assertEqual(opener.open.call_count, 2)

    def test_exhausted_incomplete_response_is_retryable_not_a_degree_guess(self):
        cache = {}
        fetch = Mock(side_effect=http.client.IncompleteRead(b"partial"))
        result = employer.lookup("https://careers.example.org/job/1", cache, fetch, now=100)
        self.assertEqual(result["status"], "fetch_failed")
        self.assertIsNone(result["degree"])
        self.assertEqual(result["expires_at"], 100 + employer.RETRY_TTL)
        employer.lookup("https://careers.example.org/job/1", cache, fetch, now=101)
        fetch.assert_called_once()

    def test_incomplete_source_response_retries_and_exhausts_cleanly(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch.object(bot.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            for failure in (http.client.IncompleteRead(b"partial"), http.client.BadStatusLine("bad")):
                with self.subTest(failure=type(failure)), patch.object(bot.urllib.request, "urlopen", side_effect=[failure, response]) as fetch:
                    self.assertEqual(bot.http_json("https://example.org"), {"ok": True})
                    self.assertEqual(fetch.call_count, 2)
                with patch.object(bot.urllib.request, "urlopen", side_effect=failure) as fetch:
                    with self.assertRaises(type(failure)):
                        bot.http_json("https://example.org", retries=2)
                    self.assertEqual(fetch.call_count, 2)
