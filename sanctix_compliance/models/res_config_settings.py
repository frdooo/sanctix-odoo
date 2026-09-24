from __future__ import annotations

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .sanctix_api_client import SanctixAPIClient, SanctixAPIError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    sanctix_api_key = fields.Char(
        string="Sanctix API Key",
        help="Generated from your Sanctix dashboard under Settings > API Keys. "
        "Stored per-company so a multi-company Odoo instance can screen "
        "under different Sanctix orgs if needed.",
    )
    sanctix_base_url = fields.Char(
        string="Sanctix API Base URL",
        config_parameter="sanctix_compliance.base_url",
        default="https://api.sanctix.com/api/v1",
        help="Only change this if instructed by Sanctix support (e.g. a "
        "dedicated/regional endpoint on an Enterprise plan).",
    )
    sanctix_auto_screen_on_create = fields.Boolean(
        string="Auto-screen new contacts",
        config_parameter="sanctix_compliance.auto_screen_on_create",
        default=True,
    )
    sanctix_auto_screen_on_write = fields.Boolean(
        string="Re-screen on name/country change",
        config_parameter="sanctix_compliance.auto_screen_on_write",
        default=True,
    )
    sanctix_scheduled_rescreen_enabled = fields.Boolean(
        string="Enable scheduled re-screening",
        config_parameter="sanctix_compliance.scheduled_rescreen_enabled",
        default=True,
        help="Periodically re-screens existing contacts even if nothing "
        "about them changed in Odoo — catches a contact that gets added "
        "to a sanctions list after you originally screened them clean. "
        "Interval is set on the scheduled action itself (Settings > "
        "Technical > Scheduled Actions > 'Sanctix: Re-screen Contacts').",
    )
    sanctix_rescreen_after_days = fields.Integer(
        string="Re-screen contacts older than (days)",
        config_parameter="sanctix_compliance.rescreen_after_days",
        default=30,
        help="The cron re-screens contacts never screened or not screened "
        "in the last N days. Lower = more quota usage.",
    )
    sanctix_cron_batch_size = fields.Integer(
        string="Max contacts per cron run",
        config_parameter="sanctix_compliance.cron_batch_size",
        default=200,
        help="Bounded batch per company per cron run so large databases "
        "drain gradually instead of timing out.",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        icp = self.env["ir.config_parameter"].sudo()
        stored = icp.get_param(f"sanctix_compliance.api_key.{self.env.company.id}")
        # Never echo the real key back into the field's default — show a
        # masked placeholder instead so it doesn't sit in plaintext in the
        # rendered HTML/JS state or browser autofill history. Real value
        # is only ever read server-side.
        res["sanctix_api_key"] = "••••••••" + stored[-4:] if stored else ""
        return res

    def set_values(self):
        super().set_values()
        icp = self.env["ir.config_parameter"].sudo()
        if self.sanctix_api_key and not self.sanctix_api_key.startswith("••••••••"):
            # Only overwrite if the user actually typed a new key —
            # otherwise the masked placeholder from get_values would get
            # saved back as the "real" key and brick the connection.
            icp.set_param(f"sanctix_compliance.api_key.{self.env.company.id}", self.sanctix_api_key)

    def action_sanctix_test_connection(self):
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        api_key = icp.get_param(f"sanctix_compliance.api_key.{self.env.company.id}")
        if not api_key:
            raise UserError(_("Enter and save an API key first."))
        client = SanctixAPIClient(api_key=api_key, base_url=self.sanctix_base_url)
        try:
            usage = client.get_usage()
            subscription = client.get_subscription()
        except SanctixAPIError as exc:
            raise UserError(_("Connection failed: %s") % exc) from exc
        finally:
            client.close()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sanctix connection OK"),
                # GET /billing/usage has no plan name of its own (only
                # counters) — the plan label comes from
                # GET /billing/subscription instead.
                "message": _("Plan: %(plan)s — Screens used: %(used)s/%(included)s")
                % {
                    "plan": subscription.get("plan", "unknown"),
                    "used": usage.get("screens_used", "?"),
                    "included": usage.get("screens_included") or _("Unlimited"),
                },
                "type": "success",
            },
        }
