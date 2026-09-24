from __future__ import annotations

import json
from collections import Counter

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .sanctix_api_client import SanctixAPIClient, SanctixAPIError


class SanctixDashboard(models.TransientModel):
    """Single-screen overview: local screening KPIs plus live Sanctix quota.

    Counters are computed from the local audit table (free, always fresh).
    Quota fields are filled on demand by "Refresh Quota" (two read-only API
    calls — never billed) so opening the dashboard costs nothing.
    """

    _name = "sanctix.dashboard"
    _description = "Sanctix Compliance Dashboard"

    total_reports = fields.Integer(compute="_compute_kpis", string="Screenings Run")
    clear_count = fields.Integer(compute="_compute_kpis", string="Clear")
    flagged_count = fields.Integer(compute="_compute_kpis", string="Flagged")
    error_count = fields.Integer(compute="_compute_kpis", string="Failed Calls")
    screened_contacts = fields.Integer(compute="_compute_kpis", string="Contacts Screened")
    quota_plan = fields.Char(string="Plan", readonly=True, help="From Sanctix — press Refresh Quota.")
    quota_used = fields.Char(string="Used", readonly=True)
    quota_included = fields.Char(string="Included", readonly=True)
    kpi_html = fields.Html(compute="_compute_visuals", sanitize=False, string="Overview")
    verdict_donut_html = fields.Html(compute="_compute_visuals", sanitize=False, string="Verdict Distribution")
    sources_bar_html = fields.Html(compute="_compute_visuals", sanitize=False, string="Hits by Source")
    brand_html = fields.Html(compute="_compute_visuals", sanitize=False, string="Sanctix")

    @api.depends()
    def _compute_kpis(self):
        report = self.env["sanctix.screening.report"]
        partner = self.env["res.partner"]
        for rec in self:
            rec.total_reports = report.search_count([])
            rec.clear_count = report.search_count([("risk_level", "=", "clear")])
            rec.flagged_count = report.search_count([("risk_level", "in", ("low", "medium", "high", "match"))])
            rec.error_count = report.search_count([("risk_level", "=", "error")])
            rec.screened_contacts = partner.search_count([("sanctix_last_screened", "!=", False)])

    @api.depends("total_reports", "clear_count", "flagged_count", "error_count", "screened_contacts")
    def _compute_visuals(self):
        for rec in self:
            rec._compute_visuals_single()

    def _compute_visuals_single(self):
        """Platform-style header, tiles, verdict donut and per-source bars —
        all inline SVG/HTML (no JS asset), Sanctix palette throughout."""
        from .sanctix_style import FONT_STACK, sanctix_logo_data_uri

        self.ensure_one()
        ink, green, amber, orange, red, slate, line = (
            "#0B1220", "#186A3B", "#8A5300", "#9A3412", "#991B1B", "#5A6577", "#E8EBF0",
        )
        total = self.total_reports or 0
        clearance = round(self.clear_count * 100 / total, 1) if total else 0.0
        blocked = self.env["sanctix.screening.report"].search_count([("risk_level", "=", "match")])

        logo = sanctix_logo_data_uri()
        self.brand_html = (
            f"<div style='display:flex;align-items:center;gap:10px;font-family:{FONT_STACK};'>"
            + (f"<img src='{logo}' style='height:34px;'/>" if logo else "")
            + "<div><div style='font-size:17px;font-weight:800;letter-spacing:1px;color:#132A4A;'>SANCTIX</div>"
            "<div style='font-size:10px;letter-spacing:2px;color:#5A6577;'>COMPLIANCE OVERVIEW</div></div></div>"
        )

        def tile(label, value, sub="", accent="#0284C7"):
            return (
                "<div style='flex:1;min-width:150px;background:#FFFFFF;border:1px solid #E8EBF0;"
                "border-top:3px solid %s;border-radius:7px;padding:12px 14px;box-shadow:0 1px 2px rgba(11,18,32,0.05);'>"
                f"<div style='font-size:11px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase;color:{slate};'>{label}</div>"
                f"<div style='font-size:26px;font-weight:700;color:{ink};font-variant-numeric:tabular-nums;margin:2px 0;'>%s</div>"
                f"<div style='font-size:11px;color:{slate};'>{sub}</div></div>" % (accent, value)
            )

        self.kpi_html = (
            f"<div style='display:flex;gap:12px;flex-wrap:wrap;font-family:{FONT_STACK};'>"
            + tile("Screenings", str(total), "Compliance records", "#0284C7")
            + tile("Clearance rate", f"{clearance}%", "Verified clean", green)
            + tile("Blocked", str(blocked), "Confirmed matches", red)
            + tile("Contacts screened", str(self.screened_contacts), "In Odoo", slate)
            + "</div>"
        )

        # Verdict donut: clear / low+medium / high+match / error.
        buckets = [
            ("Cleared / Passed", self.clear_count, green),
            ("Flagged for Review", self.env["sanctix.screening.report"].search_count(
                [("risk_level", "in", ("low", "medium", "high"))]), orange),
            ("Do Not Transact", blocked, red),
            ("Failed Calls", self.error_count, slate),
        ]
        denom = sum(c for _, c, _ in buckets) or 1
        rings, offset, legend = [], 0.0, []
        for label, count, color in buckets:
            frac = count / denom
            rings.append(
                '<circle cx="60" cy="60" r="44" fill="none" stroke="%s" stroke-width="18"'
                ' stroke-dasharray="%.2f 276.46" stroke-dashoffset="%.2f"'
                ' transform="rotate(-90 60 60)"/>' % (color, frac * 276.46, -offset * 276.46)
            )
            offset += frac
            legend.append(
                f"<div style='font-size:11px;color:{slate};margin-top:3px;'>"
                f"<span style='display:inline-block;width:9px;height:9px;border-radius:5px;background:{color};margin-right:5px;'></span>"
                f"{label} — {count}</div>"
            )
        self.verdict_donut_html = (
            f"<div style='display:flex;gap:16px;align-items:center;font-family:{FONT_STACK};"
            "background:#FFFFFF;border:1px solid #E8EBF0;border-radius:7px;padding:14px 16px;'>"
            f"<svg width='130' height='130' viewBox='0 0 120 120'><circle cx='60' cy='60' r='44' fill='none' stroke='{line}' stroke-width='18'/>"
            f"{''.join(rings)}</svg><div>{''.join(legend)}</div></div>"
        )

        # Hits per source (recent 1000 reports bound the parse cost).
        sources: Counter = Counter()
        for report in self.env["sanctix.screening.report"].search([], order="id desc", limit=1000):
            try:
                stored = json.loads(report.raw_response or "{}")
            except (TypeError, ValueError):
                continue
            screening = ((stored.get("party") or {}).get("screening")) or stored
            hits = screening.get("all_hits") if isinstance(screening, dict) else None
            for hit in hits or []:
                if isinstance(hit, dict) and hit.get("source"):
                    sources[str(hit["source"])] += 1
        top_sources = sources.most_common(9)
        peak = top_sources[0][1] if top_sources else 0
        bar_palette = ["#7C3AED", "#38BDF8", "#1D4ED8", "#0E7490", "#D97706", "#B91C1C", "#15803D", "#6B7280", "#EC4899"]
        bars = "".join(
            f"<div style='margin-bottom:7px;'><div style='display:flex;justify-content:space-between;font-size:11px;color:{slate};'>"
            f"<span>{src}</span><span style='font-variant-numeric:tabular-nums;'>{count}</span></div>"
            f"<div style='background:{line};border-radius:3px;height:9px;margin-top:3px;'>"
            f"<div style='background:{bar_palette[i % len(bar_palette)]};border-radius:3px;height:9px;"
            f"width:{(count * 100 // peak) if peak else 0}%;'></div></div></div>"
            for i, (src, count) in enumerate(top_sources)
        )
        self.sources_bar_html = (
            f"<div style='font-family:{FONT_STACK};background:#FFFFFF;border:1px solid #E8EBF0;"
            "border-radius:7px;padding:14px 16px;'>"
            + (bars or f"<div style='font-size:12px;color:{slate};'>No matches recorded yet — bars appear after the first flagged screening.</div>")
            + "</div>"
        )

    @api.model
    def action_open_dashboard(self) -> dict:
        """Menu entry point: one dashboard row per user, opened directly."""
        rec = self.search([("create_uid", "=", self.env.uid)], limit=1) or self.create({})
        return {
            "type": "ir.actions.act_window",
            "res_model": "sanctix.dashboard",
            "res_id": rec.id,
            "view_mode": "form",
            "views": [(self.env.ref("sanctix_compliance.view_sanctix_dashboard_form").id, "form")],
            "target": "current",
        }

    def action_refresh_quota(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group("sanctix_compliance.group_sanctix_user")):
            raise UserError(_("You are not allowed to use Sanctix."))
        icp = self.env["ir.config_parameter"].sudo()
        api_key = icp.get_param(f"sanctix_compliance.api_key.{self.env.company.id}") or icp.get_param(
            "sanctix_compliance.api_key"
        )
        if not api_key:
            raise UserError(_("No Sanctix API key configured — open Sanctix Compliance > Configuration first."))
        client = SanctixAPIClient(api_key=api_key, base_url=icp.get_param("sanctix_compliance.base_url") or None)
        try:
            try:
                usage = client.get_usage()
                subscription = client.get_subscription()
            except SanctixAPIError as exc:
                raise UserError(_("Could not read quota from Sanctix: %s") % exc) from exc
        finally:
            client.close()
        included = usage.get("screens_included")
        self.write(
            {
                "quota_plan": str(subscription.get("plan") or "—").title(),
                "quota_used": str(usage.get("screens_used", "—")),
                "quota_included": str(included if included is not None else "Unlimited"),
            }
        )
        return self.action_open_dashboard()
