import io
import json
import os
import unittest
from unittest.mock import patch

from api.index import BadProviderResponse, analyze, evidence_for, field_record, handler, read_notices, read_route


class FakeClocked:
    def __init__(self, *fields, grounding_response=None, rerank_response=None):
        self.fields = iter(fields)
        self.compared = []
        self.extracted = []
        self.verified = []
        self.ranked = []
        self.grounding_response = grounding_response
        self.rerank_response = rerank_response

    def compare(self, *, documents):
        self.compared.append(documents)
        return {"verdict": "conflict", "conflicts": [{"document_index": 1}]}

    def extract(self, *, text, fields):
        self.extracted.append((text, set(fields)))
        return next(self.fields)

    def grounding(self, **request):
        self.verified.append(request)
        if isinstance(self.grounding_response, Exception):
            raise self.grounding_response
        return self.grounding_response

    def rerank(self, **request):
        self.ranked.append(request)
        if isinstance(self.rerank_response, Exception):
            raise self.rerank_response
        return self.rerank_response


class DeadlineDeltaTests(unittest.TestCase):
    first = "First-time applicants must submit by 20 November 2026 at 17:00 CET."
    renewal = "Renewal applicants must submit by 27 November 2026 at 17:00 CET."
    earlier = "All applicants must submit by 18 November 2026 at 17:00 CET."

    def route_client(self, actions, verdict="supported"):
        return FakeClocked(
            {"fields": {"deadline": self.earlier}},
            {"fields": {"deadline": [self.first, self.renewal]}},
            rerank_response={"results": [
                {"id": f"cutoff-{i}", "action": action, "relevance_score": 0.99}
                for i, action in enumerate(actions, start=1)
            ]},
            grounding_response={"verdict": verdict, "answers_question": verdict == "supported"},
        )

    def test_evidence_contains_only_source_quotes_and_line_numbers(self):
        source = "Documents\nA letter is no longer required.\nSubmit via the portal."
        result = evidence_for({"values": ["A letter", "A letter is no longer required.", "Letter required"]}, source)
        self.assertEqual(result, [{"text": "A letter is no longer required.", "line": 2}])

    def test_exact_span_disambiguates_repeated_text(self):
        source = "Submission: 25 November 2026\nInterview: 25 November 2026"
        value = "25 November 2026"
        start = source.rindex(value)
        self.assertEqual(evidence_for({"value": value, "start": start, "end": start + len(value)}, source),
                         [{"text": value, "line": 2}])

    def test_same_full_deadline_across_language_formats(self):
        left = [{"text": "Apply by 25 November 2026 at 17:00 CET.", "line": 1}]
        right = [{"text": "Bewerbungsschluss: 25.11.2026 um 17:00 Uhr MEZ.", "line": 1}]
        self.assertEqual(field_record("deadline", left, right)["status"], "same")

    def test_clock_dates_use_explicit_offsets_across_midnight(self):
        left = [{"text": "Apply by 25 November 2026 at 11:30 pm EST.", "line": 1}]
        right = [{"text": "Bewerbungsschluss: 26.11.2026 um 06:30 UTC+02:00.", "line": 1}]
        self.assertEqual(field_record("deadline", left, right)["status"], "same")

    def test_missing_zone_does_not_imply_same_instant(self):
        left = [{"text": "Apply by 25 November 2026 at 17:00 CET.", "line": 1}]
        right = [{"text": "Apply by 25 November 2026 at 5 pm.", "line": 1}]
        self.assertEqual(field_record("deadline", left, right)["note"], "deadline_precision")

    def test_missing_new_requirement_does_not_prove_removal(self):
        record = field_record("documents", [{"text": "A signed letter", "line": 1}], [])
        self.assertEqual((record["status"], record["note"]), ("review", "not_verified_in_revision"))

    def test_compare_and_extract_with_pending_deadline(self):
        a = "Austausch\nBewerbungsschluss: 30. November 2026.\nPer E-Mail einreichen."
        b = "Austausch\nDie neue Bewerbungsfrist wird separat bekannt gegeben.\nIm Portal einreichen."
        client = FakeClocked(
            {"fields": {"deadline": "30. November 2026", "channel": "Per E-Mail"}},
            {"fields": {"deadline": {"value": "Die neue Bewerbungsfrist wird separat bekannt gegeben."}, "channel": "Im Portal"}},
        )
        response = analyze(client, a, b)
        self.assertEqual(client.compared, [[a, b]])
        self.assertEqual([notice for notice, _ in client.extracted], [a, b])
        self.assertTrue(response["signal"]["conflicts"])
        by_key = {record["key"]: record for record in response["rows"]}
        self.assertEqual(by_key["deadline"]["note"], "pending")
        self.assertEqual(by_key["deadline"]["after"][0]["line"], 2)
        self.assertEqual(by_key["channel"]["status"], "changed")
        self.assertEqual(response["decision"]["reason"], "pending")
        self.assertIsNone(response["decision"]["after"])
        self.assertEqual(client.verified, [])

    def test_two_cutoffs_do_not_select_either_date(self):
        old = "Apply by 18 November 2026."
        first = "Applications close on 20 November 2026 at 17:00 CET."
        second = "Submit your application by 22 November 2026 at 17:00 CET."
        client = FakeClocked({"fields": {"deadline": [old]}}, {"fields": {"deadline": [first, second]}})
        result = analyze(client, old, first + "\n" + second)
        self.assertEqual(result["decision"]["reason"], "multiple_deadlines")
        self.assertIsNone(result["decision"]["after"])
        self.assertEqual([item["line"] for item in result["rows"][0]["after"]], [1, 2])
        self.assertEqual(client.verified, [])

    def test_same_notice_gives_each_group_its_own_verified_cutoff(self):
        revised = self.first + "\n" + self.renewal
        for route, actions, expected_date, quote, line in [
            ("First-time applicants", ("use", "skip"), "2026-11-20", self.first, 1),
            ("Renewal applicants", ("skip", "use"), "2026-11-27", self.renewal, 2),
        ]:
            with self.subTest(route=route):
                client = self.route_client(actions)
                result = analyze(client, self.earlier, revised, route=route)
                self.assertEqual(client.ranked[0]["context"], {"application_route": route})
                self.assertEqual(client.verified[0]["documents"], [{"id": "revised-notice", "text": revised}])
                self.assertIn(route, client.verified[0]["question"])
                self.assertIn(quote, client.verified[0]["answer"])
                self.assertEqual(result["decision"]["after"]["date"], expected_date)
                self.assertEqual(result["decision"]["proof"], [{"text": quote, "line": line}])
                self.assertEqual(result["rows"][0]["status"], "scoped")

    def test_route_match_receives_source_labels_even_if_extraction_is_just_dates(self):
        revised = self.first + "\n" + self.renewal
        client = FakeClocked(
            {"fields": {"deadline": self.earlier}},
            {"fields": {"deadline": ["20 November 2026 at 17:00 CET", "27 November 2026 at 17:00 CET"]}},
            rerank_response={"results": [{"id": "cutoff-1", "action": "skip"}, {"id": "cutoff-2", "action": "use"}]},
            grounding_response={"verdict": "supported", "answers_question": True},
        )
        result = analyze(client, self.earlier, revised, route="Renewal applicants")
        self.assertEqual(client.ranked[0]["documents"][1]["text"], self.renewal)
        self.assertEqual(result["decision"]["proof"][0]["text"], self.renewal)

    def test_no_group_does_not_run_rerank_or_pick_a_date(self):
        client = self.route_client(("use", "skip"))
        result = analyze(client, self.earlier, self.first + "\n" + self.renewal)
        self.assertEqual(result["decision"]["reason"], "multiple_deadlines")
        self.assertIsNone(result["decision"]["after"])
        self.assertEqual(client.ranked, [])

    def test_irrelevant_high_score_ambiguous_or_incomplete_ranking_abstains(self):
        revised = self.first + "\n" + self.renewal
        for actions, reason in [(("skip", "expand"), "route_not_found"),
                                (("use", "use"), "multiple_deadlines"),
                                (("use",), "route_unavailable")]:
            with self.subTest(actions=actions):
                client = self.route_client(actions)
                result = analyze(client, self.earlier, revised, route="Guest applicants")
                self.assertEqual(result["decision"]["reason"], reason)
                self.assertIsNone(result["decision"]["after"])
                self.assertEqual(result["decision"]["proof"], [])
                self.assertEqual(client.verified, [])
                self.assertEqual(len(result["rows"][0]["after"]), 2)

    def test_verify_can_reject_a_plausible_but_wrong_match(self):
        client = self.route_client(("use", "skip"), verdict="unsupported")
        result = analyze(client, self.earlier, self.first + "\n" + self.renewal, route="Renewal applicants")
        self.assertEqual(result["decision"]["reason"], "route_unverified")
        self.assertIsNone(result["decision"]["after"])
        self.assertEqual(result["decision"]["proof"], [])

    def test_verify_uses_only_the_revised_notice_for_the_new_date(self):
        old = "Apply by 18 November 2026 at 17:00 CET."
        new = "Apply by 25 November 2026 at 17:00 CET."
        client = FakeClocked({"fields": {"deadline": old}}, {"fields": {"deadline": new}},
                             grounding_response={"verdict": "supported", "answers_question": True})
        decision = analyze(client, old, new)["decision"]
        self.assertEqual(decision["reason"], "new_date")
        self.assertEqual(decision["after"], {"date": "2026-11-25", "time": "17:00", "zone": "CET"})
        self.assertEqual(client.verified[0]["documents"], [{"id": "revised-notice", "text": new}])
        self.assertIn("2026-11-25 17:00 CET", client.verified[0]["answer"])

    def test_verify_refusal_or_timeout_keeps_evidence_but_hides_actionable_date(self):
        earlier = "Apply by 18 November 2026."
        revised = "Apply by 25 November 2026 if you are applying from abroad."
        for verification, reason in [({"verdict": "unsupported", "answers_question": False}, "verify_unsupported"),
                                     (TimeoutError(), "verify_unavailable")]:
            with self.subTest(reason=reason):
                client = FakeClocked({"fields": {"deadline": earlier}}, {"fields": {"deadline": revised}},
                                     grounding_response=verification)
                result = analyze(client, earlier, revised)
                self.assertEqual(result["decision"]["reason"], reason)
                self.assertIsNone(result["decision"]["after"])
                self.assertEqual(result["rows"][0]["after"][0]["text"], revised)

    def test_incomplete_provider_response_is_not_empty_success(self):
        client = FakeClocked({}, {"fields": {}})
        with self.assertRaises(BadProviderResponse):
            analyze(client, "Earlier notice", "Revised notice")

    def test_invalid_inputs_do_not_pass_validation(self):
        self.assertIsNone(read_notices({"earlier": "too short", "revised": "Valid revised notice."}))
        self.assertIsNone(read_notices({"earlier": "a" * 4801, "revised": "Valid revised notice."}))
        self.assertIsNone(read_route({"route": ["First-time applicants"]}))
        self.assertIsNone(read_route({"route": "x" * 91}))
        self.assertEqual(read_route({"route": "  Renewal   applicants  "}), "Renewal applicants")

    def test_missing_secret_never_serves_sample_as_live(self):
        payload = json.dumps({"earlier": "Earlier application notice.", "revised": "Revised application notice."}).encode()
        request = handler.__new__(handler)
        request.headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
        request.rfile = io.BytesIO(payload)
        with patch.dict(os.environ, {"CLOCKED_API_KEY": ""}), patch.object(request, "send_json") as send:
            request.do_GET()
            send.assert_called_once_with(200, {"configured": False})
            send.reset_mock()
            request.do_POST()
            send.assert_called_once_with(503, {"error": "not_configured"})


if __name__ == "__main__":
    unittest.main()
