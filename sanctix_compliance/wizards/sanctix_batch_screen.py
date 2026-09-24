from __future__ import annotations

import logging
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.sanctix_api_client import SanctixAPIError

_logger = logging.getLogger(__name__)

# Hard ceiling regardless of what the user selects — POST /screen/batch
# scores every query against every sanctions list inside ONE request, so a
# huge selection risks timing out that single call before it returns
# anything at all (unlike a per-contact loop, a batch call is all-or-
# nothing: it can't leave screening "half-done" with partial results, so
# a timeout here loses the whole run). 200 is also comfortably under the
# API's own hard ceiling of 500 queries/call, leaving headroom for plans
# with a lower per-request cap (GET /billing/plans `max_batch_size`).
MAX_BATCH_SIZE = 200


class SanctixBatchScreenWizard(models.TransientModel):
    _name = "sanctix.batch.screen.wizard"
    _description = "Sanctix Batch Screening"

    partner_ids = fields.Many2many("res.partner", string="Contacts to Screen")
    skip_already_clear = fields.Boolean(
        string="Skip contacts already marked Clear",
        default=True,
        help="Avoids re-spending quota on contacts that were already "
        "screened Clear recently. Turn this off to force a full re-screen "
        "of every selected contact regardless of current status.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Drop dead contact ids from stale selections (list opened before
        the contacts were deleted) instead of crashing with Missing Record
        on open — the user then simply picks live contacts. Accepts the raw
        id lists actions pass (default_partner_ids=active_ids) as well as
        (4, id) / (6, 0, ids) commands."""
        partner_model = self.env["res.partner"]
        for vals in vals_list:
            commands = vals.get("partner_ids")
            if not commands:
                continue
            live: list[int] = []
            for command in commands:
                if isinstance(command, int):
                    if partner_model.browse(command).exists():
                        live.append(command)
                    continue
                if not isinstance(command, (list, tuple)) or len(command) < 2:
                    continue
                if command[0] == 6:
                    ids = command[2] or []
                    live.extend(i for i in ids if partner_model.browse(i).exists())
                elif command[0] == 4:
                    if partner_model.browse(command[1]).exists():
                        live.append(command[1])
            vals["partner_ids"] = [(6, 0, live)]
        return super().create(vals_list)

    def action_confirm(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group("sanctix_compliance.group_sanctix_user")):
            raise UserError(_("You are not allowed to run Sanctix screenings."))
        had_selection = bool(self.partner_ids)
        partners = self.partner_ids.exists()
        if had_selection and not partners:
            raise UserError(
                _("The selected contacts no longer exist (they were deleted). Pick contacts again.")
            )
        if self.skip_already_clear:
            partners = partners.filtered(lambda p: p.sanctix_risk_level != "clear")
        # A contact with no name can't be screened — mirrors the filter the
        # backend itself applies (blank names are dropped, not billed), kept
        # here too so `partners` and the queries we send stay index-aligned.
        partners = partners.filtered(lambda p: p.name)

        if not partners:
            raise UserError(
                _("Nothing to screen — pick contacts above (contacts already marked Clear are skipped).")
            )

        if len(partners) > MAX_BATCH_SIZE:
            raise UserError(
                _(
                    "You selected %(count)s contacts, but batch screening is capped at "
                    "%(max)s at a time to avoid timing out. Please screen in smaller batches."
                )
                % {"count": len(partners), "max": MAX_BATCH_SIZE}
            )

        client = None
        try:
            # Multi-company: one client + one batch call PER company, each
            # with its own API key and idempotency key. Using a single key
            # for a cross-company selection would bill/screen under the
            # wrong Sanctix org.
            by_company: dict = {}
            fallback_company = self.env.company
            for partner in partners:
                cid = partner.company_id.id or fallback_company.id
                by_company.setdefault(cid, self.env["res.partner"])
                by_company[cid] |= partner

            flagged = 0
            screened_total = 0
            for cid, company_partners in by_company.items():
                company = self.env["res.company"].browse(cid)
                client = company_partners.with_company(company)._get_sanctix_client()
                try:
                    queries = [
                        {
                            "name": partner.name,
                            "country": partner.country_id.code if partner.country_id else None,
                        }
                        for partner in company_partners
                    ]

                    # One HTTP call for this company's slice
                    # (POST /screen/batch) instead of one call per contact —
                    # cheaper, and idempotent on retry so re-clicking Confirm
                    # after a timeout can't double-screen or double-bill.
                    try:
                        batch_response = client.screen_batch(queries, idempotency_key=str(uuid.uuid4()))
                    except SanctixAPIError as exc:
                        _logger.error(
                            "Sanctix batch screening failed for %s contact(s): %s",
                            len(company_partners),
                            exc,
                        )
                        raise UserError(_("Batch screening failed: %s") % exc) from exc
                finally:
                    client.close()
                    client = None

                results = batch_response.get("results") or []
                if len(results) != len(company_partners):
                    # Should not happen — every query we sent has a non-blank
                    # name, so the backend shouldn't drop any of them — but
                    # if the shapes ever drift, fail loudly instead of
                    # silently mis-attributing contact B's result to A.
                    raise UserError(
                        _(
                            "Sanctix returned %(got)s result(s) for %(sent)s contact(s) submitted — "
                            "refusing to guess which result belongs to which contact. Please retry, "
                            "or contact Sanctix support if this persists."
                        )
                        % {"got": len(results), "sent": len(company_partners)}
                    )

                for partner, result in zip(company_partners, results):
                    report = partner._sanctix_apply_screening_result(result, triggered_by="batch")
                    if report.risk_level != "clear":
                        flagged += 1
                screened_total += len(company_partners)
        finally:
            if client is not None:
                client.close()

        message = _("%(screened)s contact(s) screened.") % {"screened": screened_total}
        if flagged:
            message += " " + _("%(flagged)s flagged for review — check each contact's Screening History.") % {
                "flagged": flagged
            }

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Batch screening complete"),
                "message": message,
                "type": "warning" if flagged else "success",
                "sticky": bool(flagged),
            },
        }
