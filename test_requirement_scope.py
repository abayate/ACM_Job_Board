"""Eligibility contracts for the scope of alternatives and preferences."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import employer_degree as employer
import job_bot as bot
from test_employer_degree import page
from test_job_bot import listing


# Include the three review reproductions and nearby controls. These labels are
# constructed behavioral contracts, not a sample for estimating live accuracy.
CASES = (
    ("PhD required and a cloud certification or equivalent experience.", "grad"),
    ("PhD and a cloud certification or equivalent experience required.", "grad"),
    ("A cloud certification or equivalent experience and a PhD required.", "grad"),
    ("PhD or equivalent research experience and a cloud certification required.", "equivalent"),
    ("Equivalent research experience or PhD and a cloud certification required.", "equivalent"),
    ("PhD or equivalent research experience and enrollment in a master's program required.", "grad"),
    ("PhD required, and Python experience preferred.", "grad"),
    ("PhD required and Python experience not required.", "grad"),
    ("Must have a Bachelor's, Master's, or PhD degree, and Python experience preferred.", "bach"),
    ("Must have a Bachelor's, Master's, or PhD degree and Python experience preferred.", "bach"),
    ("Must have a PhD, Master's, or Bachelor's degree, but Python experience preferred.", "bach"),
    ("Must have a Bachelor's, Master's, or PhD degree, Python experience preferred.", "bach"),
    ("Python experience preferred, but must have a Bachelor's, Master's, or PhD degree.", "bach"),
    ("Bachelor's, Master's, or PhD degree required, and Python preferred.", "bach"),
    ("Bachelor's degree required, Master's or PhD preferred.", "bach"),
    ("PhD in math and computer science preferred.", None),
    ("Bachelor's, Master's, or PhD degree preferred.", None),
    ("Only applicants who have graduated with a PhD are eligible.", "grad"),
    ("All candidates who have graduated must hold a PhD.", "grad"),
    ("Candidates who have graduated with a PhD are required.", "grad"),
    ("Applicants who have graduated must be entering a master's program.", "grad"),
    ("Only applicants who have graduated and are entering a master's program are eligible.", "grad"),
    ("PhD preferred, Bachelor's degree required, Python preferred.", "bach"),
    ("Bachelor's preferred, PhD required, Python preferred.", "grad"),
    ("Python preferred, PhD required, C++ experience preferred.", "grad"),
    ("PhD in math and computer science or equivalent experience.", "equivalent"),
    ("Master's in electrical and computer engineering or equivalent experience.", "equivalent"),
    ("PhD in electrical and computer engineering required and cloud certification or equivalent experience.", "grad"),
)


def responses(requirement):
    """The same eligibility rule must survive each supported provider shape."""
    markup = "<h2>Requirements</h2><p>" + requirement + "</p>"
    return (
        ("html", "<h1>Research Intern</h1>" + markup, None),
        ("html", page("Research Intern", educationRequirements=markup), None),
        ("greenhouse", json.dumps({"title": "Research Intern", "content": markup}), None),
        ("lever", json.dumps({"text": "Research Intern", "lists": [
            {"text": "Requirements", "content": "<li>" + requirement + "</li>"}]}), None),
        ("ashby", json.dumps({"jobs": [{"id": "123", "title": "Research Intern",
                                      "descriptionHtml": markup}]}), "123"),
        ("smartrecruiters", json.dumps({"name": "Research Intern", "jobAd": {
            "sections": {"qualifications": {"title": "Requirements", "text": markup}}}}), None),
        ("workday", json.dumps({"jobPostingInfo": {
            "title": "Research Intern", "jobDescription": markup}}), None),
    )


class RequirementScopeTests(unittest.TestCase):
    def test_requirement_scope_across_all_providers(self):
        for requirement, expected in CASES:
            for provider, raw, token in responses(requirement):
                with self.subTest(requirement=requirement, provider=provider, structured="ld+json" in raw):
                    self.assertEqual(employer.parse_response(provider, raw, token)[1], expected)

    def test_review_counterexamples_reach_the_correct_channel(self):
        cases = (
            (CASES[0][0], ["PhD"], "grad"),
            (CASES[8][0], ["Bachelor's"], "bach"),
            (CASES[17][0], [], "grad"),
        )
        for requirement, tags, degree in cases:
            for kind in ("internship", "new_grad"):
                job = listing("Research Intern", tags, kind)
                raw = responses(requirement)[0][1]
                with self.subTest(requirement=requirement, kind=kind), \
                     patch("socket.socket", side_effect=AssertionError("Tests must stay offline")), \
                     patch.object(employer, "fetch_public_text", return_value=raw), \
                     patch.multiple(bot, EMPLOYER_LOOKUP=True, MAX_EMPLOYER_LOOKUPS=1, DEGREE_LOOKUP=False), \
                     contextlib.redirect_stdout(io.StringIO()):
                    bot.resolve_degrees([job], {})
                self.assertEqual(job["degree"], degree)
                self.assertEqual(job["degree_src"], "employer")
                self.assertEqual(job["degree_evidence"]["status"], "verified")
                expected = "grad" if degree == "grad" else "bachelors" if kind == "internship" else "newgrad"
                self.assertEqual(bot.route(job), expected)

    def test_graduated_subgroup_requires_an_explicit_undergraduate_path(self):
        conditional = (
            "Applicants who have graduated must be entering a master's program.",
            "If you have already graduated, you must be entering a master's program.",
            "Candidates who have graduated must be pursuing a PhD.",
        )
        for requirement in conditional:
            for degree in ("Bachelor's", "Bachelor's or Master's"):
                with self.subTest(requirement=requirement, degree=degree):
                    undergraduate = "<p>Currently pursuing a " + degree + " degree.</p>"
                    markup = "<h2>Requirements</h2><p>" + requirement + "</p>"
                    self.assertEqual(employer.parse_page(markup)[1], "grad")
                    self.assertEqual(employer.parse_page(markup + undergraduate)[1], "bach")
                    self.assertEqual(employer.parse_page("<h2>Requirements</h2>" + undergraduate
                                                       + "<p>" + requirement + "</p>")[1], "bach")

    def test_graduated_subgroup_cannot_erase_a_universal_graduate_requirement(self):
        for universal in ("all applicants must be pursuing a PhD", "every candidate must hold a PhD"):
            markup = ("<h2>Requirements</h2><p>Currently pursuing a Bachelor's degree.</p>"
                      "<p>Applicants who have graduated must enter a master's program and " + universal + ".</p>")
            with self.subTest(universal=universal):
                self.assertEqual(employer.parse_page(markup)[1], "grad")

    def test_exclusive_graduate_requirement_wins_over_bachelor_prerequisite(self):
        for restriction in (
            "Only applicants who have graduated with a PhD are eligible.",
            "Only applicants who have graduated must be entering a master's program.",
            "All candidates who have graduated must hold a PhD.",
        ):
            for bachelor in ("Bachelor's degree required.", "Currently pursuing a Bachelor's degree."):
                with self.subTest(restriction=restriction, bachelor=bachelor):
                    markup = "<h2>Requirements</h2><p>" + bachelor + "</p><p>" + restriction + "</p>"
                    self.assertEqual(employer.parse_page(markup)[1], "grad")

    def test_long_coordinated_requirements_do_not_exhaust_the_parser_stack(self):
        requirement = "PhD required" + " and ability to use Python" * 1100
        self.assertEqual(employer.parse_page("<h2>Requirements</h2><p>" + requirement + "</p>")[1], "grad")
