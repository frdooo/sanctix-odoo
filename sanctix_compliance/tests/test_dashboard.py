"""Tests for the dashboard KPIs and quota refresh (mocked — reading quota
is free, but tests must never touch the network).
"""
from __future__ import annotations

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDashboard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            f"sanctix_compliance.api_key.{self.env.company.id}", "stx_test_key"
        )
        self.partner = self.env["res.partner"].create({"name": "Dash Co", "sanctix_auto_screen": False})

    def _make_reports(self, tag):
        report = self.env["sanctix.screening.report"].sudo()
        for risk in ("clear", "clear", "match", "error"):
            report.create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.env.company.id,
                    "report_reference": "",
                    "screened_name": f"dash-{tag}",
                    "entity_type": "individual",
                    "risk_level": risk,
                    "match_count": 0,
                    "triggered_by": "manual",
                    "raw_response": "{}",
                }
            )

    def test_kpis_and_visuals(self):        # Delta-based: the suite may run on a DB that already has reports
        # (e.g. screened from the UI), so assert increments, not absolutes.
        report = self.env["sanctix.screening.report"].sudo()
        before_total = report.search_count([])
        before_clear = report.search_count([("risk_level", "=", "clear")])
        self._make_reports("kpi")
        dash = self.env["sanctix.dashboard"].create({})
        self.assertEqual(dash.total_reports - before_total, 4)
        self.assertEqual(dash.clear_count - before_clear, 2)
        self.assertEqual(dash.flagged_count, report.search_count([("risk_level", "in", ("low", "medium", "high", "match"))]))
        self.assertEqual(dash.error_count, report.search_count([("risk_level", "=", "error")]))
        expected_rate = round(dash.clear_count * 100 / dash.total_reports, 1) if dash.total_reports else 0.0
        self.assertIn(f"{expected_rate}%", dash.kpi_html)
        self.assertIn("Do Not Transact", dash.verdict_donut_html)

    def test_refresh_quota_reads_api_without_billing(self):
        dash = self.env["sanctix.dashboard"].create({})
        with (
            patch(
                "odoo.addons.sanctix_compliance.models.sanctix_api_client.SanctixAPIClient.get_usage",
                return_value={"screens_used": 115, "screens_included": 1500},
            ),
            patch(
                "odoo.addons.sanctix_compliance.models.sanctix_api_client.SanctixAPIClient.get_subscription",
                return_value={"plan": "growth"},
            ),
        ):
            dash.action_refresh_quota()
        self.assertEqual(dash.quota_plan, "Growth")
        self.assertEqual(dash.quota_used, "115")
        self.assertEqual(dash.quota_included, "1500")

    def test_open_dashboard_returns_singleton_form(self):
        first = self.env["sanctix.dashboard"].action_open_dashboard()
        second = self.env["sanctix.dashboard"].action_open_dashboard()
        self.assertEqual(first["res_id"], second["res_id"])
        self.assertEqual(first["view_mode"], "form")

    def test_brand_header_shows_sanctix_logo(self):
        dash = self.env["sanctix.dashboard"].create({})
        self.assertIn("SANCTIX", dash.brand_html)
        self.assertIn("data:image/svg+xml;base64,", dash.brand_html)
