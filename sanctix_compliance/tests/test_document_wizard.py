"""Tests for the document (OCR) screening wizard: one uploaded file fans out
to one report row per extracted party, and the contact rolls up to the worst
verdict — never to an average or to the last row's luck of the draw.
"""
from __future__ import annotations

import base64
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


def _doc_response(verdicts: list[str]) -> dict:
    parties = [
        {
            "name": f"Party {i}",
            "role": "consignee" if i % 2 == 0 else "notify_party",
            "country": "IR" if v != "CLEAR" else None,
            "page": 1,
            "screening": {
                "verdict": v,
                "confidence": "CLEAR" if v == "CLEAR" else "HIGH",
                "final_score": 0 if v == "CLEAR" else 90,
                "total_hits": 0 if v == "CLEAR" else 2,
                "ref": f"doc-ref-{i}",
            },
        }
        for i, v in enumerate(verdicts)
    ]
    return {
        "ref": "batch-doc-1",
        "document": {"filename": "invoice.pdf", "content_type": "application/pdf", "pages": 1, "document_type": "commercial_invoice"},
        "parties": parties,
        "total_screened": len(verdicts),
        "total_flagged": sum(1 for v in verdicts if v != "CLEAR"),
    }


@tagged("post_install", "-at_install")
class TestDocumentScreenWizard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            f"sanctix_compliance.api_key.{self.env.company.id}", "stx_test_key"
        )
        self.partner = self.env["res.partner"].create(
            {"name": "Doc Test Co", "sanctix_auto_screen": False}
        )

    def _wizard(self):
        return self.env["sanctix.document.screen.wizard"].create(
            {
                "partner_id": self.partner.id,
                "document": base64.b64encode(b"%PDF-fake").decode(),
                "document_filename": "invoice.pdf",
            }
        )

    def _patch_doc(self, **kwargs):
        return patch(
            "odoo.addons.sanctix_compliance.models.sanctix_api_client.SanctixAPIClient.screen_document",
            **kwargs,
        )

    def test_parties_fan_out_to_one_report_each(self):
        with self._patch_doc(return_value=_doc_response(["CLEAR", "DO_NOT_TRANSACT"])):
            self._wizard().action_confirm()
        reports = self.partner.sanctix_screening_report_ids
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(r.triggered_by == "document" for r in reports))
        self.assertEqual({r.screened_name for r in reports}, {"Party 0", "Party 1"})

    def test_partner_rolls_up_to_worst_verdict(self):
        with self._patch_doc(return_value=_doc_response(["CLEAR", "FLAG_FOR_REVIEW", "DO_NOT_TRANSACT"])):
            self._wizard().action_confirm()
        self.assertEqual(self.partner.sanctix_risk_level, "match")

    def test_no_parties_found_creates_no_reports(self):
        resp = _doc_response([])
        resp["ref"] = None
        with self._patch_doc(return_value=resp):
            result = self._wizard().action_confirm()
        self.assertEqual(len(self.partner.sanctix_screening_report_ids), 0)
        self.assertEqual(result["params"]["title"], "No parties found")

    def test_api_error_becomes_user_error(self):
        from odoo.addons.sanctix_compliance.models.sanctix_api_client import SanctixAPIError

        with self._patch_doc(side_effect=SanctixAPIError("boom", code="unreachable")):
            with self.assertRaises(UserError):
                self._wizard().action_confirm()

    def test_user_without_group_is_rejected(self):
        outsider = self.env["res.users"].create(
            {"name": "No Access", "login": "noaccess_test", "groups_id": [(6, 0, [])]}
        )
        wizard = self._wizard().with_user(outsider)
        with self._patch_doc(return_value=_doc_response(["CLEAR"])) as mock_doc:
            with self.assertRaises(UserError):
                wizard.action_confirm()
        mock_doc.assert_not_called()

    def test_stale_partner_id_dropped_on_open(self):
        wizard = self.env["sanctix.document.screen.wizard"].create(
            {
                "partner_id": 999999999,
                "document": base64.b64encode(b"%PDF-fake").decode(),
                "document_filename": "invoice.pdf",
            }
        )
        self.assertFalse(wizard.partner_id)

    def test_dashboard_context_does_not_leak_ghost_partner(self):
        dash = self.env["sanctix.dashboard"].create({})
        wizard = (
            self.env["sanctix.document.screen.wizard"]
            .with_context(active_model="sanctix.dashboard", active_id=dash.id, default_partner_id=dash.id)
            .create(
                {
                    "document": base64.b64encode(b"%PDF-fake").decode(),
                    "document_filename": "invoice.pdf",
                }
            )
        )
        self.assertFalse(wizard.partner_id)

    def test_deleted_contact_gives_clear_error_not_missing_record(self):
        partner = self.env["res.partner"].create({"name": "Doomed Co", "sanctix_auto_screen": False})
        wizard = self.env["sanctix.document.screen.wizard"].create(
            {
                "partner_id": partner.id,
                "document": base64.b64encode(b"%PDF-fake").decode(),
                "document_filename": "invoice.pdf",
            }
        )
        partner.unlink()
        with self._patch_doc(return_value=_doc_response(["CLEAR"])) as mock_doc:
            with self.assertRaises(UserError):
                wizard.action_confirm()
        mock_doc.assert_not_called()

    def test_unknown_extension_rejected_without_http(self):
        wizard = self.env["sanctix.document.screen.wizard"].create(
            {
                "partner_id": self.partner.id,
                "document": base64.b64encode(b"data").decode(),
                "document_filename": "invoice.txt",
            }
        )
        with self._patch_doc(return_value=_doc_response(["CLEAR"])) as mock_doc:
            with self.assertRaises(UserError):
                wizard.action_confirm()
        mock_doc.assert_not_called()
