from __future__ import annotations

import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .sanctix_api_client import SanctixAPIClient, SanctixAPIError
from .sanctix_style import risk_pill_html

_logger = logging.getLogger(__name__)

# Fields that, if changed, should trigger an automatic re-screen — a name
# or country change can meaningfully change a match outcome; most other
# field edits (phone, notes, tags) should not burn a screening credit.
AUTO_RESCREEN_TRIGGER_FIELDS = {"name", "country_id"}


class ResPartner(models.Model):
    _inherit = "res.partner"

    sanctix_risk_level = fields.Selection(
        [
            ("unscreened", "Not Screened"),
            ("clear", "Clear"),
            ("low", "Low Risk"),
            ("medium", "Medium Risk"),
            ("high", "High Risk"),
            ("match", "Confirmed Match"),
            ("error", "Screening Failed"),
        ],
        string="Sanctions Screening Status",
        default="unscreened",
        readonly=True,
        copy=False,
        index=True,
        help="Result of the most recent Sanctix screening. This reflects "
        "ONLY the last screening call — see the Screening History tab for "
        "the full audit trail, since risk status can change between "
        "screenings without anyone in Odoo touching this record.",
    )
    sanctix_last_screened = fields.Datetime(
        string="Last Screened", readonly=True, copy=False
    )
    sanctix_report_reference = fields.Char(
        string="Latest Sanctix Report Ref", readonly=True, copy=False
    )
    sanctix_screening_report_ids = fields.One2many(
        "sanctix.screening.report", "partner_id", string="Screening History"
    )
    sanctix_screening_count = fields.Integer(
        compute="_compute_sanctix_screening_count", string="Screenings"
    )
    sanctix_auto_screen = fields.Boolean(
        string="Auto-screen this contact",
        default=True,
        help="If disabled, this specific contact is excluded from "
        "create/write auto-screening AND the scheduled re-screen cron — "
        "useful for internal contacts (your own company's employees, "
        "intra-group entities) that never need sanctions screening.",
    )
    sanctix_risk_badge_html = fields.Html(
        string="Screening Status",
        compute="_compute_sanctix_risk_badge_html",
        sanitize=False,
        help="Sanctix-style status pill (same colors as the dashboard).",
    )

    @api.depends("sanctix_risk_level")
    def _compute_sanctix_risk_badge_html(self):
        labels = dict(self._fields["sanctix_risk_level"].selection)
        for partner in self:
            partner.sanctix_risk_badge_html = risk_pill_html(
                partner.sanctix_risk_level, labels.get(partner.sanctix_risk_level, "")
            )

    @api.depends("sanctix_screening_report_ids")
    def _compute_sanctix_screening_count(self):
        # search_count instead of len(one2many): avoids loading every
        # report row just to render the stat button, and stays fast on
        # contacts with long screening histories.
        report_model = self.env["sanctix.screening.report"]
        for partner in self:
            partner.sanctix_screening_count = (
                report_model.search_count([("partner_id", "=", partner.id)]) if partner.id else 0
            )

    # ------------------------------------------------------------------
    # Screening actions
    # ------------------------------------------------------------------

    def action_sanctix_screen_now(self):
        """Button on the Contact form. Single-record by design — batch
        screening goes through the dedicated wizard so users don't
        accidentally burn a large quota by multi-selecting in a list view
        and hitting a button meant for one contact at a time."""
        self.ensure_one()
        # View-level groups= can be bypassed via direct RPC, so enforce
        # the group server-side too. Superuser (tests, migrations) bypasses
        # all access control in Odoo by design, so it is allowed through —
        # automated paths (cron) don't call this button method anyway.
        # Works on 17/18/19.
        if not (self.env.su or self.env.user.has_group("sanctix_compliance.group_sanctix_user")):
            raise UserError(_("You are not allowed to run Sanctix screenings."))
        self._sanctix_screen(triggered_by="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Screening complete"),
                "message": self._sanctix_status_message(),
                "type": "warning" if self.sanctix_risk_level in ("high", "match", "error") else "success",
                "sticky": self.sanctix_risk_level in ("high", "match", "error"),
            },
        }

    def _sanctix_status_message(self) -> str:
        labels = dict(self._fields["sanctix_risk_level"].selection)
        return _("%(name)s: %(status)s") % {"name": self.name, "status": labels.get(self.sanctix_risk_level, "")}

    def _sanctix_screen(self, *, triggered_by: str) -> None:
        """Core screening call, shared by the manual button, auto-screen
        hooks, the cron job, and the batch wizard. Never raises out to the
        caller for API-level failures (network, quota, auth) — those are
        recorded as an 'error' screening report instead, so a Sanctix
        outage never blocks someone from saving a Contact in Odoo.
        Raises only for genuine configuration errors (e.g. no API key
        set), since that's actionable by the person clicking the button,
        not something to silently swallow."""
        self.ensure_one()
        if not self.name:
            return

        client = self._get_sanctix_client()  # raises UserError if unconfigured — intentional, see docstring

        try:
            response = client.screen_entity(
                name=self.name,
                country=self.country_id.code if self.country_id else None,
            )
        except SanctixAPIError as exc:
            _logger.error("Sanctix screening failed for partner %s (id=%s): %s", self.name, self.id, exc)
            self._sanctix_record_error(str(exc), triggered_by)
            return
        finally:
            client.close()

        self._sanctix_apply_screening_result(response, triggered_by)

    def _sanctix_apply_screening_result(self, response: dict, triggered_by: str) -> "SanctixScreeningReport":
        """Turns one /screen/entity-shaped response dict into a stored
        report row and updates this Contact's status fields. Shared by the
        single-screen path above and the batch wizard, which gets its
        responses from POST /screen/batch's `results` list — each entry of
        which is the same per-query shape as a single /screen/entity call."""
        self.ensure_one()
        report = self.env["sanctix.screening.report"].sudo().create_from_api_response(self, response, triggered_by)
        self.sudo().write(
            {
                "sanctix_risk_level": report.risk_level,
                "sanctix_last_screened": fields.Datetime.now(),
                "sanctix_report_reference": report.report_reference,
            }
        )
        return report

    def _sanctix_record_error(self, error_message: str, triggered_by: str) -> "SanctixScreeningReport":
        """Records a failed screening CALL (network/quota/auth) — shared by
        the single-screen path and the batch wizard's per-item error
        handling."""
        self.ensure_one()
        report = self.env["sanctix.screening.report"].sudo().create_error_record(self, error_message, triggered_by)
        self.sudo().write({"sanctix_risk_level": "error", "sanctix_last_screened": fields.Datetime.now()})
        return report

    def _get_sanctix_client(self) -> SanctixAPIClient:
        icp = self.env["ir.config_parameter"].sudo()
        api_key = icp.get_param(f"sanctix_compliance.api_key.{self.env.company.id}") or icp.get_param(
            "sanctix_compliance.api_key"
        )
        base_url = icp.get_param("sanctix_compliance.base_url") or None
        if not api_key:
            raise UserError(
                _(
                    "No Sanctix API key configured for company %(company)s. "
                    "Go to Settings > Sanctix Compliance to add one."
                )
                % {"company": self.env.company.name}
            )
        return SanctixAPIClient(api_key=api_key, base_url=base_url)

    # ------------------------------------------------------------------
    # Automatic screening hooks
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param("sanctix_compliance.auto_screen_on_create", "True") == "True":
            for partner in partners:
                if partner.sanctix_auto_screen and partner.name and not partner.parent_id:
                    # Skip child contacts (e.g. "Invoicing" sub-contact of
                    # a company) by default — screening the parent company
                    # is what matters; screening every delivery-address
                    # child record would multiply API usage for no
                    # compliance benefit. Toggle this behavior here if you
                    # want child contacts screened too.
                    partner._sanctix_screen_safely(triggered_by="auto_create")
        return partners

    def write(self, vals):
        # Snapshot trigger fields BEFORE super().write() so we only burn a
        # screening credit when the value actually changed — writing the same
        # name back (e.g. mass-edit, import sync) must not re-screen.
        tracked_old: dict = {}
        if AUTO_RESCREEN_TRIGGER_FIELDS.intersection(vals.keys()):
            for partner in self:
                tracked_old[partner.id] = (partner.name, partner.country_id.id)
        result = super().write(vals)
        if tracked_old:
            icp = self.env["ir.config_parameter"].sudo()
            if icp.get_param("sanctix_compliance.auto_screen_on_write", "True") == "True":
                for partner in self:
                    old = tracked_old.get(partner.id)
                    if old is None:
                        continue
                    old_name, old_country_id = old
                    name_changed = "name" in vals and partner.name != old_name
                    country_changed = "country_id" in vals and partner.country_id.id != old_country_id
                    if not (name_changed or country_changed):
                        continue
                    if partner.sanctix_auto_screen and partner.name and not partner.parent_id:
                        partner._sanctix_screen_safely(triggered_by="auto_write")
        return result

    def _sanctix_screen_safely(self, *, triggered_by: str) -> None:
        """Wraps _sanctix_screen for hooks that must never block the
        underlying create/write transaction — a misconfigured API key or
        a Sanctix outage should never prevent someone from saving a
        Contact. Logs and moves on instead of raising."""
        try:
            self._sanctix_screen(triggered_by=triggered_by)
        except UserError as exc:
            _logger.warning("Sanctix auto-screen skipped for partner %s (id=%s): %s", self.name, self.id, exc)

    # ------------------------------------------------------------------
    # Scheduled re-screening (cron)
    # ------------------------------------------------------------------

    @api.model
    def _cron_sanctix_rescreen(self, batch_size: int | None = None) -> None:
        """Re-screens contacts that haven't been checked in a while, so a
        contact that was clean at screening time and has since been added
        to a sanctions list gets caught without anyone in Odoo doing
        anything. Runs per-company (each company only re-screens its own
        contacts plus shared company-agnostic contacts, using its own API
        key) and processes a bounded batch per run rather than the whole
        database at once — with a weekly interval and this cap, a large
        contact base drains gradually across multiple runs instead of one
        run trying to do everything and risking a timeout or a quota
        blowout in a single call.

        A contact is eligible if: auto-screen isn't disabled on it,
        it isn't a child contact (see create() for why), and it either
        has never been screened or wasn't screened in the last
        `rescreen_after_days` days (default 30, override via
        ir.config_parameter `sanctix_compliance.rescreen_after_days`).
        Batch cap defaults to 200, override via
        `sanctix_compliance.cron_batch_size`. Both overrides keep this
        working on 17/18/19 without code changes.
        """
        icp = self.env["ir.config_parameter"].sudo()
        try:
            rescreen_after_days = max(
                1, int(icp.get_param("sanctix_compliance.rescreen_after_days", "30"))
            )
        except (TypeError, ValueError):
            rescreen_after_days = 30
        if batch_size is None:
            try:
                batch_size = max(
                    1, int(icp.get_param("sanctix_compliance.cron_batch_size", "200"))
                )
            except (TypeError, ValueError):
                batch_size = 200
        for company in self.env["res.company"].sudo().search([]):
            if icp.get_param("sanctix_compliance.scheduled_rescreen_enabled", "True") != "True":
                continue
            if not icp.get_param(f"sanctix_compliance.api_key.{company.id}"):
                continue  # no key configured for this company — nothing to do, don't spam the log

            cutoff = fields.Datetime.now() - timedelta(days=rescreen_after_days)

            domain = [
                # Shared contacts (company_id=False) are visible to every
                # company and must be re-screened too — the old
                # ("company_id", "=", company.id) domain silently skipped
                # them forever.
                ("company_id", "in", [False, company.id]),
                ("sanctix_auto_screen", "=", True),
                ("parent_id", "=", False),
                ("name", "!=", False),
                "|",
                ("sanctix_last_screened", "=", False),
                ("sanctix_last_screened", "<", cutoff),
            ]
            partners = self.with_company(company).sudo().search(domain, limit=batch_size)
            _logger.info("Sanctix scheduled re-screen: %s contact(s) due for company %s", len(partners), company.name)
            for partner in partners:
                partner._sanctix_screen_safely(triggered_by="scheduled")
