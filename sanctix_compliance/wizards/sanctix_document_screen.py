from __future__ import annotations

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.sanctix_api_client import SanctixAPIError
from ..models.sanctix_style import worse_risk

_logger = logging.getLogger(__name__)

# Extension -> content_type claim for POST /screen/document. The server
# re-sniffs magic bytes and 422s on mismatch, so a wrong guess here fails
# loudly server-side rather than screening the wrong bytes.
_EXTENSION_TO_CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class SanctixDocumentScreenWizard(models.TransientModel):
    """OCR-style screening: attach a trade document (invoice, bill of
    lading, packing list, …), Sanctix extracts the named parties and screens
    each one. One report row per party, partner status rolled up to the worst
    verdict — same audit-trail shape as batch screening.

    Cost: max(1, parties found) screens per document (Growth plan or above;
    5 calls/minute). There is no idempotency key on this endpoint, so this
    wizard never auto-retries a call that already returned — only transport
    errors (handled inside the client) are retried.
    """

    _name = "sanctix.document.screen.wizard"
    _description = "Sanctix Document (OCR) Screening"

    partner_id = fields.Many2one(
        "res.partner",
        string="Contact",
        # NOT required at model level: a stale default (contact deleted
        # after the opener was rendered) is dropped in create() below, and
        # action_confirm asks the user to pick a live one instead.
        required=False,
        help="Reports are filed on this contact's Screening History and its "
        "status rolls up to the worst party verdict.",
    )
    document = fields.Binary(string="Document", required=True)
    document_filename = fields.Char(string="Filename")
    threshold = fields.Integer(
        string="Match Threshold",
        default=45,
        help="Minimum score to count as a hit (30–100). Leave at 45 unless "
        "your compliance policy says otherwise.",
    )
    limit = fields.Integer(
        string="Max Hits per Party",
        default=5,
        help="How many candidate matches to keep per extracted party (1–20).",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        # The opener action carries {'default_partner_id': active_id} — from a
        # contact form that is the contact, but from anywhere else (dashboard,
        # a stale list) it is some other record's id or a ghost. Only honor
        # it for live contacts opened from a contact; otherwise drop it so
        # the wizard opens clean instead of Missing-Record on open.
        if "partner_id" in fields_list:
            if self.env.context.get("active_model") != "res.partner":
                res.pop("partner_id", None)
            elif res.get("partner_id") and not self.env["res.partner"].browse(res["partner_id"]).exists():
                res.pop("partner_id", None)
        if "partner_id" in fields_list and not res.get("partner_id"):
            active_model = self.env.context.get("active_model")
            active_id = self.env.context.get("active_id")
            if active_model == "res.partner" and active_id:
                if self.env["res.partner"].browse(active_id).exists():
                    res["partner_id"] = active_id
        return res

    @api.model_create_multi
    def create(self, vals_list):
        """Drop dead contact references (stale list/form opened before the
        contact was deleted) instead of crashing with Missing Record on
        open — the user then simply picks a live contact."""
        partner_model = self.env["res.partner"]
        for vals in vals_list:
            pid = vals.get("partner_id")
            if pid and not partner_model.browse(pid).exists():
                vals.pop("partner_id", None)
        return super().create(vals_list)

    def action_confirm(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group("sanctix_compliance.group_sanctix_user")):
            raise UserError(_("You are not allowed to run Sanctix screenings."))
        # The contact this wizard was opened for may have been deleted since
        # (stale form/binding) — .exists() never raises, unlike touching a
        # dead record, so check first and say so plainly.
        if not self.partner_id:
            raise UserError(_("Pick the contact to file this screening on first."))
        partner = self.partner_id.exists()
        if not partner:
            raise UserError(
                _("The contact linked to this screening no longer exists (it was deleted). "
                  "Reopen Screen Document from a contact, or pick another contact above.")
            )
        if not self.document or not self.document_filename:
            raise UserError(_("Attach a document first (PDF, JPEG, PNG or WebP, max 10 MB)."))
        content_type = self._content_type_for(self.document_filename)

        client = partner.with_company(partner.company_id or self.env.company)._get_sanctix_client()
        try:
            try:
                response = client.screen_document(
                    filename=self.document_filename,
                    content_type=content_type,
                    content_base64=self.document.decode() if isinstance(self.document, bytes) else self.document,
                    threshold=self.threshold or None,
                    limit=self.limit or None,
                )
            except SanctixAPIError as exc:
                _logger.error("Sanctix document screening failed (%s): %s", self.document_filename, exc)
                raise UserError(_("Document screening failed: %s") % exc) from exc
        finally:
            client.close()

        parties = response.get("parties") or []
        if not parties:
            # Valid outcome (nothing identifiable on the page) — still bills
            # 1 screen server-side, so say so instead of looking free.
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("No parties found"),
                    "message": _(
                        "Sanctix could read '%(file)s' (%(doc)s) but found no "
                        "screenable parties. Note: Sanctix still bills 1 screen "
                        "for the extraction."
                    )
                    % {
                        "file": self.document_filename,
                        "doc": (response.get("document") or {}).get("document_type", "?"),
                    },
                    "type": "warning",
                    "sticky": True,
                },
            }

        report_model = self.env["sanctix.screening.report"].sudo()
        worst = "clear"
        flagged = 0
        for party in parties:
            report = report_model.create_from_document_party(partner, party, self.document_filename)
            worst = worse_risk(worst, report.risk_level)
            if report.risk_level != "clear":
                flagged += 1
        partner.sudo().write(
            {
                "sanctix_risk_level": worst,
                "sanctix_last_screened": fields.Datetime.now(),
                "sanctix_report_reference": response.get("ref") or "",
            }
        )

        message = _("%(count)s partie(s) screened from '%(file)s'.") % {
            "count": len(parties),
            "file": self.document_filename,
        }
        if flagged:
            message += " " + _("%(flagged)s flagged for review — check Screening History.") % {"flagged": flagged}
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Document screening complete"),
                "message": message,
                "type": "warning" if flagged else "success",
                "sticky": bool(flagged),
            },
        }

    @staticmethod
    def _content_type_for(filename: str) -> str:
        ext = "." + (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
        content_type = _EXTENSION_TO_CONTENT_TYPE.get(ext)
        if not content_type:
            raise UserError(
                _("Cannot tell the file type of '%(file)s' — use .pdf, .jpg, .png or .webp.") % {"file": filename}
            )
        return content_type
