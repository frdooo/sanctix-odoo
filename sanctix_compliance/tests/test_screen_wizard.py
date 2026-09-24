"""Tests for the ad-hoc "screen any name" wizard: screen-only shows the
verdict without saving anything; save files exactly one report and bills
exactly one screen (no double-charge via the create hook).
"""
from __future__ import annotations

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


def _screen_response(verdict="FLAG_FOR_REVIEW"):
    return {
        "verdict": verdict,
        "confidence": "MEDIUM",
        "final_score": 74,
        "top_match": None,
        "all_hits": [
            {
                "entity_id": 1,
                "source": "eu",
                "name": "Matched Entity",
                "programs": [],
                "countries": [],
                "score": 74,
                "confidence": "MEDIUM",
            }
        ],
        "total_hits": 1,
        "screened_at": "2026-08-06T04:02:31Z",
        "elapsed_ms": 100,
        "ref": "STX-ADHOC1",
    }


@tagged("post_install", "-at_install")
class TestScreenWizard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            f"sanctix_compliance.api_key.{self.env.company.id}", "stx_test_key"
        )

    def _wizard(self, name="Some Name", include_ai=False):
        return self.env["sanctix.screen.wizard"].create({"name": name, "include_ai": include_ai})

    def _patch_entity(self, **kwargs):
        return patch(
            "odoo.addons.sanctix_compliance.models.sanctix_api_client.SanctixAPIClient.screen_entity",
            **kwargs,
        )

    def test_screen_only_shows_verdict_and_saves_nothing(self):
        wizard = self._wizard()
        reports_before = self.env["sanctix.screening.report"].search_count([])
        with self._patch_entity(return_value=_screen_response()):
            result = wizard.action_screen()
        self.assertEqual(wizard.result_verdict, "FLAG_FOR_REVIEW")
        self.assertEqual(wizard.result_score, 74)
        self.assertIn("Matched Entity", wizard.result_html)
        # The wizard reopens on top so the verdict card is actually seen —
        # a bare notification would strand the user on the input form.
        self.assertEqual(result["type"], "ir.actions.act_window")
        self.assertEqual(result["res_id"], wizard.id)
        self.assertEqual(result["target"], "new")
        self.assertEqual(self.env["res.partner"].search_count([("name", "=", "Some Name")]), 0)
        self.assertEqual(self.env["sanctix.screening.report"].search_count([]), reports_before)

    def test_save_bills_once_and_files_report(self):
        wizard = self._wizard()
        with self._patch_entity(return_value=_screen_response()) as mock_screen:
            wizard.action_screen_and_save()
            self.assertEqual(mock_screen.call_count, 1)
        partner = self.env["res.partner"].search([("name", "=", "Some Name")], limit=1)
        self.assertTrue(partner)
        self.assertTrue(partner.sanctix_auto_screen)  # re-enabled after silent create
        reports = partner.sanctix_screening_report_ids
        self.assertEqual(len(reports), 1)
        self.assertEqual(partner.sanctix_risk_level, "medium")

    def _patch_full(self, **kwargs):
        return patch(
            "odoo.addons.sanctix_compliance.models.sanctix_api_client.SanctixAPIClient.screen_full",
            **kwargs,
        )

    def _full_response(self):
        return {
            "ref": "STX-FULL1",
            "verdict": "DO_NOT_TRANSACT",
            "confidence": "HIGH",
            "final_score": 100,
            "entity_screen": {
                "all_hits": [
                    {
                        "entity_id": 7,
                        "source": "ofac_sdn",
                        "name": "Full Hit",
                        "programs": [],
                        "countries": [],
                        "score": 100,
                        "confidence": "HIGH",
                    }
                ],
                "total_hits": 1,
            },
            "ai_report": "## Memo\n\n**Finding:** sanctioned.\n\n- Point one\n- Point two",
        }

    def test_ai_flow_renders_memo_and_files_it(self):
        wizard = self._wizard("AI Person", include_ai=True)
        with self._patch_full(return_value=self._full_response()):
            wizard.action_screen()
        self.assertEqual(wizard.result_verdict, "DO_NOT_TRANSACT")
        self.assertIn("Finding:", wizard.result_html)
        self.assertIn("<strong>Finding:</strong>", wizard.result_html)
        self.assertNotIn("## Memo", wizard.result_html)
        with self._patch_full() as mock_full, self._patch_entity() as mock_entity:
            wizard.action_screen_and_save()
            mock_full.assert_not_called()
            mock_entity.assert_not_called()
        partner = self.env["res.partner"].search([("name", "=", "AI Person")], limit=1)
        report = partner.sanctix_screening_report_ids[:1]
        self.assertEqual(report.match_count, 1)
        self.assertEqual(report.triggered_by, "full")
        self.assertIn("Finding:", report.ai_assessment)
        self.assertIn("Finding:", report.report_detail_html)
        self.assertEqual(report.report_reference, "STX-FULL1")

    def test_save_reuses_existing_contact(self):
        existing = self.env["res.partner"].create({"name": "Known Entity", "sanctix_auto_screen": False})
        wizard = self.env["sanctix.screen.wizard"].create({"name": "known entity", "include_ai": False})
        with self._patch_entity(return_value=_screen_response()):
            wizard.action_screen_and_save()
        self.assertEqual(self.env["res.partner"].search_count([("name", "=ilike", "Known Entity")]), 1)
        self.assertEqual(len(existing.sanctix_screening_report_ids), 1)

    def test_user_without_group_is_rejected(self):
        outsider = self.env["res.users"].create(
            {"name": "No Access", "login": "noaccess_screen_test", "groups_id": [(6, 0, [])]}
        )
        wizard = self._wizard().with_user(outsider)
        with self._patch_entity(return_value=_screen_response()) as mock_screen:
            with self.assertRaises(UserError):
                wizard.action_screen()
        mock_screen.assert_not_called()