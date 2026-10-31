"""Fail CI if the release breaks any clearly labeled recorded audit case."""
import unittest

from scripts.replay_routing_audit import load_corpus, replay


class RecordedAuditTests(unittest.TestCase):
    def test_frozen_corpus_is_complete_and_has_valid_response_hashes(self):
        corpus = load_corpus()
        self.assertEqual(corpus["population_size"], 4543)
        cases = corpus["cases"]
        self.assertEqual({c["job"]["audit_id"] for c in cases}, set(range(1, 121)))
        self.assertEqual(sum(c["response"] is None for c in cases), 1)
        self.assertEqual(sum(c["reference_label"] == "unclear" for c in cases), 1)

    def test_every_clear_reference_routes_correctly_through_real_resolver(self):
        report = replay()
        self.assertEqual(report["scored"], 119)
        self.assertEqual(report["graduate_required"], 30)
        self.assertEqual(report["non_graduate_required"], 89)
        for result in report["results"]:
            with self.subTest(audit_id=result["audit_id"], company=result["company"]):
                if result["reference_label"] == "unclear":
                    self.assertIsNone(result["correct"])
                else:
                    self.assertTrue(result["correct"], result)
