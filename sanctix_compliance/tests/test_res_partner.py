"""Tests that a fixture /screen/entity-shaped response maps to the correct
Contact status. Before this suite, the mapping read fields (`risk_level`,
`report_reference`, `matches`) that don't exist on the real response, so
every screening silently recorded as risk_level="clear" regardless of the
actual verdict — this pins the fix down so that regression can't return
unnoticed.
"""
from __future__ import annotations

from unittest.mock import patch

from odoo.addons.sanctix_compliance.models.sanctix_api_client import SanctixAPIError
from odoo.tests.common import TransactionCase, tagged


def _screen_response(verdict: str, ref: str = "ref123", hits: int = 0) -> dict:
    """Shaped like screening_engine.py ScreeningResult.to_dict() — the
    actual POST /screen/entity response, not the schema this module used
    to (incorrectly) assume."""
    return {
        "schema_version": "1.0",
        "query": {},
        "verdict": verdict,
        "confidence": "HIGH" if verdict != "CLEAR" else "CLEAR",
        "final_score": 90 if verdict != "CLEAR" else 0,
        "top_match": None,
        "all_hits": [{"entity_id": i} for i in range(hits)],
        "total_hits": hits,
        "screened_at": "2026-01-01T00:00:00Z",
        "elapsed_ms": 12,
        "ref": ref,
    }


@tagged("post_install", "-at_install")
class TestResPartnerScreening(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            f"sanctix_compliance.api_key.{self.env.company.id}", "stx_test_key"
        )
        self.partner = self.env["res.partner"].create(
            {"name": "Test Screening Target", "sanctix_auto_screen": False}
        )

    def _patch_client(self, *, return_value=None, side_effect=None):
        return patch(
            "odoo.addons.sanctix_compliance.models.sanctix_api_client.SanctixAPIClient.screen_entity",
            return_value=return_value,
            side_effect=side_effect,
        )

    def test_clear_verdict_maps_to_clear_risk_level(self):
        with self._patch_client(return_value=_screen_response("CLEAR")):
            self.partner._sanctix_screen(triggered_by="manual")
        self.assertEqual(self.partner.sanctix_risk_level, "clear")
        self.assertEqual(self.partner.sanctix_report_reference, "ref123")

    def test_do_not_transact_verdict_maps_to_match_not_clear(self):
        """This is the case that was silently swallowed before the fix —
        a confirmed/high-severity hit was recorded as 'clear' because the
        old code read a `risk_level` key the API never sends."""
        with self._patch_client(return_value=_screen_response("DO_NOT_TRANSACT", hits=3)):
            self.partner._sanctix_screen(triggered_by="manual")
        self.assertEqual(self.partner.sanctix_risk_level, "match")
        report = self.partner.sanctix_screening_report_ids[:1]
        self.assertEqual(report.match_count, 3)

    def test_flag_for_review_maps_to_medium(self):
        with self._patch_client(return_value=_screen_response("FLAG_FOR_REVIEW", hits=1)):
            self.partner._sanctix_screen(triggered_by="manual")
        self.assertEqual(self.partner.sanctix_risk_level, "medium")

    def test_api_error_records_error_not_clear(self):
        """A failed CALL must never be recorded/read as a safe result."""
        with self._patch_client(side_effect=SanctixAPIError("boom", code="unreachable")):
            self.partner._sanctix_screen(triggered_by="manual")
        self.assertEqual(self.partner.sanctix_risk_level, "error")
        report = self.partner.sanctix_screening_report_ids[:1]
        self.assertEqual(report.error_message, "boom")
