"""Tests for the platform-style report rendering: the on-screen detail card
and the printable official-report values. Key guarantee: views never show
raw JSON — everything user-facing comes from these builders.
"""
from __future__ import annotations

import json

from odoo.tests.common import TransactionCase, tagged


def _entity_response(verdict="DO_NOT_TRANSACT", hits=2):
    return {
        "schema_version": "3.0",
        "query": {"name": "Bin Laden Osama"},
        "verdict": verdict,
        "confidence": "HIGH",
        "final_score": 74,
        "top_match": None,
        "all_hits": [
            {
                "entity_id": i,
                "source": "ofac_sdn" if i == 0 else "eu",
                "source_id": f"src-{i}",
                "entity_type": "individual",
                "name": f"Hit Name {i}",
                "programs": ["SDGT"],
                "countries": ["AF"],
                "score": 74 - i,
                "confidence": "MEDIUM",
                "signal_breakdown": {
                    "token_sort": 30,
                    "token_overlap": 20,
                    "fuzzy_wratio": 33,
                    "phonetic": 8,
                    "exact_normalized": 0,
                    "name_subtotal": 74 - i,
                    "dob_delta": 0,
                    "country_delta": 0,
                    "id_delta": 0,
                    "final": 74 - i,
                    "matched_on": "alias_match" if i == 0 else "primary_name",
                },
            }
            for i in range(hits)
        ],
        "total_hits": hits,
        "screened_at": "2026-08-06T04:02:31Z",
        "elapsed_ms": 11100,
        "ref": "STX-TEST1",
    }


@tagged("post_install", "-at_install")
class TestReporting(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create(
            {"name": "Report Test", "sanctix_auto_screen": False}
        )

    def _report(self, response, risk_hint="manual"):
        return (
            self.env["sanctix.screening.report"]
            .sudo()
            .create_from_api_response(self.partner, response, risk_hint)
        )

    def test_detail_card_shows_matches_not_json(self):
        report = self._report(_entity_response())
        card = report.report_detail_html
        self.assertIn("Hit Name 0", card)
        self.assertIn("74", card)
        self.assertNotIn('"verdict"', card)
        self.assertNotIn("all_hits", card)

    def test_detail_card_clear_state(self):
        report = self._report(_entity_response(verdict="CLEAR", hits=0))
        self.assertEqual(report.risk_level, "clear")
        self.assertIn("No matches", report.report_detail_html)

    def test_detail_card_error_state(self):
        report = (
            self.env["sanctix.screening.report"]
            .sudo()
            .create_error_record(self.partner, "boom", "manual")
        )
        self.assertIn("boom", report.report_detail_html)
        self.assertIn("did not run", report.report_detail_html)

    def test_official_values_cover_pdf_sections(self):
        report = self._report(_entity_response())
        vals = report._prepare_official_report()
        self.assertEqual(vals["verdict"], "DO_NOT_TRANSACT")
        self.assertEqual(vals["score"], 74)
        self.assertEqual(vals["total"], 2)
        self.assertEqual(len(vals["matches"]), 2)
        top = vals["matches"][0]
        self.assertEqual(top["name"], "Hit Name 0")
        self.assertEqual([s["label"] for s in top["signals"]][:2], ["Token Sort Ratio", "Token Overlap"])
        self.assertIn("Report Test", vals["summary"])
        sources = {c["source"] for c in vals["coverage"]}
        self.assertEqual(sources, {"ofac_sdn", "eu"})
        self.assertAlmostEqual(sum(s["fraction"] for s in vals["segments"]), 1.0)
        self.assertIn("tone", vals)
        self.assertIn("assessment", vals)

    def test_official_values_tolerate_garbage(self):
        report = self._report({"verdict": "CLEAR", "ref": "x"})
        report.sudo().write({"raw_response": "not-json{{{", "risk_level": "clear", "match_count": 0})
        card = report.report_detail_html
        self.assertNotIn("not-json", card)
        vals = report._prepare_official_report()
        self.assertEqual(vals["total"], 0)
        self.assertEqual(vals["matches"], [])

    def test_markdown_converter_is_safe_and_structured(self):
        from odoo.addons.sanctix_compliance.models.sanctix_style import markdown_to_html

        out = markdown_to_html("## Title\n\n**Bold** move.\n\n- one\n- two\n\n<script>alert(1)</script>")
        self.assertIn("<strong>Bold</strong>", out)
        self.assertIn("<li>one</li>", out)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertEqual(markdown_to_html(""), "")

    def test_ai_assessment_renders_in_card_and_pdf_values(self):
        report = self._report(_entity_response())
        report.sudo().write({"ai_assessment": "## Memo\n\n**Finding:** check this."})
        self.assertIn("Screening assessment", report.report_detail_html)
        self.assertIn("<strong>Finding:</strong>", report.report_detail_html)
        vals = report._prepare_official_report()
        self.assertIn("<strong>Finding:</strong>", vals["ai_html"])

    def test_download_button_returns_pdf_action_and_logo_resolves(self):
        import os

        from odoo.modules import get_module_resource

        report = self._report(_entity_response())
        action = report.action_download_pdf()
        self.assertEqual(action["type"], "ir.actions.report")
        self.assertEqual(action["report_name"], "sanctix_compliance.report_sanctix_official_document")
        logo = get_module_resource("sanctix_compliance", "static/src/img", "sanctix_logo.svg")
        self.assertTrue(logo and os.path.isfile(logo))
