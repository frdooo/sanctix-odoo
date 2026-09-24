"""Tests that the batch wizard calls POST /screen/batch exactly once for
the whole selection, instead of the old N-calls-in-a-loop approach, and
distributes the batch response back onto the right contacts in order.
"""
from __future__ import annotations

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


def _batch_response(verdicts: list[str]) -> dict:
    results = [
        {
            "verdict": v,
            "confidence": "CLEAR" if v == "CLEAR" else "HIGH",
            "final_score": 0 if v == "CLEAR" else 90,
            "all_hits": [],
            "ref": f"ref-{i}",
        }
        for i, v in enumerate(verdicts)
    ]
    return {
        "total_screened": len(verdicts),
        "total_flagged": sum(1 for v in verdicts if v != "CLEAR"),
        "elapsed_ms": 42,
        "ref": "batch-ref-1",
        "results": results,
    }


@tagged("post_install", "-at_install")
class TestBatchScreenWizard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            f"sanctix_compliance.api_key.{self.env.company.id}", "stx_test_key"
        )
        self.partners = self.env["res.partner"].create(
            [
                {"name": "Contact A", "sanctix_auto_screen": False},
                {"name": "Contact B", "sanctix_auto_screen": False},
            ]
        )

    def _patch_client(self, *, return_value=None, side_effect=None):
        return patch(
            "odoo.addons.sanctix_compliance.models.sanctix_api_client.SanctixAPIClient.screen_batch",
            return_value=return_value,
            side_effect=side_effect,
        )

    def test_calls_batch_endpoint_once_not_per_contact(self):
        wizard = self.env["sanctix.batch.screen.wizard"].create({"partner_ids": [(6, 0, self.partners.ids)]})
        with self._patch_client(return_value=_batch_response(["CLEAR", "DO_NOT_TRANSACT"])) as mock_batch:
            wizard.action_confirm()

        mock_batch.assert_called_once()
        queries = mock_batch.call_args.args[0]
        self.assertEqual([q["name"] for q in queries], ["Contact A", "Contact B"])

    def test_results_map_back_to_the_right_contact_in_order(self):
        wizard = self.env["sanctix.batch.screen.wizard"].create({"partner_ids": [(6, 0, self.partners.ids)]})
        with self._patch_client(return_value=_batch_response(["CLEAR", "DO_NOT_TRANSACT"])):
            wizard.action_confirm()

        contact_a, contact_b = self.partners
        self.assertEqual(contact_a.sanctix_risk_level, "clear")
        self.assertEqual(contact_b.sanctix_risk_level, "match")

    def test_mismatched_result_count_raises_instead_of_guessing(self):
        wizard = self.env["sanctix.batch.screen.wizard"].create({"partner_ids": [(6, 0, self.partners.ids)]})
        with self._patch_client(return_value=_batch_response(["CLEAR"])):  # only 1 result for 2 contacts
            with self.assertRaises(UserError):
                wizard.action_confirm()

    def test_stale_selection_dropped_on_open(self):
        dead_id = max(self.partners.ids) + 9999
        wizard = self.env["sanctix.batch.screen.wizard"].create(
            {"partner_ids": [(6, 0, self.partners.ids + [dead_id])]}
        )
        self.assertEqual(set(wizard.partner_ids.ids), set(self.partners.ids))

    def test_raw_id_list_from_action_keeps_live_only(self):
        dead_id = max(self.partners.ids) + 9999
        wizard = self.env["sanctix.batch.screen.wizard"].create(
            {"partner_ids": self.partners.ids + [dead_id]}
        )
        self.assertEqual(set(wizard.partner_ids.ids), set(self.partners.ids))

    def test_deleted_contacts_give_clear_error_not_missing_record(self):
        wizard = self.env["sanctix.batch.screen.wizard"].create({"partner_ids": [(6, 0, self.partners.ids)]})
        self.partners.unlink()
        with self._patch_client(return_value=_batch_response(["CLEAR"])) as mock_batch:
            with self.assertRaises(UserError):
                wizard.action_confirm()
        mock_batch.assert_not_called()
