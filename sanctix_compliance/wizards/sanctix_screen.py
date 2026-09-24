from __future__ import annotations

import json
import logging
import uuid

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.sanctix_api_client import SanctixAPIClient, SanctixAPIError

_logger = logging.getLogger(__name__)


class SanctixScreenWizard(models.TransientModel):
    """Screen any name — like the platform's search box — without needing a
    Contact first. "Screen" shows the verdict right here (1 screen billed);
    nothing is saved. "Screen & Save as Contact" files the audit report on
    a Contact from the SAME already-paid response, so it never bills twice.

    "Include AI assessment" calls POST /screen/full instead of
    /screen/entity: still exactly 1 screen, but the response additionally
    carries an AI memo and a server-stored reference.
    """

    _name = "sanctix.screen.wizard"
    _description = "Sanctix Screen Any Name"

    name = fields.Char(string="Name to Screen", required=True, help="Person or organization name, as on the platform.")
    country_id = fields.Many2one("res.country", string="Country")
    dob = fields.Char(
        string="Date of Birth",
        help="Optional corroborating signal for individuals (any readable date form the API accepts).",
    )
    include_ai = fields.Boolean(
        string="Include screening assessment (AI)",
        default=True,
        help="Uses the full screening call (entity + AI memo). Same 1-screen cost, "
        "needs AI enabled for your Sanctix org — if it comes back empty, the verdict still stands.",
    )
    result_verdict = fields.Char(string="Verdict", readonly=True)
    result_score = fields.Integer(string="Score", readonly=True)
    result_ref = fields.Char(string="Sanctix Reference", readonly=True)
    result_ai = fields.Text(string="AI Assessment (raw)", readonly=True)
    result_is_full = fields.Boolean(string="Full-flow Response", readonly=True)
    result_payload = fields.Text(string="Raw Response (cached)", readonly=True)
    result_html = fields.Html(string="Result", readonly=True, sanitize=False)

    def action_screen(self):
        self.ensure_one()
        self._check_group()
        if not (self.name or "").strip():
            raise UserError(_("Enter a name to screen."))
        client = self._get_client()
        try:
            try:
                if self.include_ai:
                    response = client.screen_full(
                        name=self.name.strip(),
                        country=self.country_id.code if self.country_id else None,
                        dob=(self.dob or "").strip() or None,
                        generate_ai_report=True,
                        idempotency_key=str(uuid.uuid4()),
                    )
                else:
                    response = client.screen_entity(
                        name=self.name.strip(),
                        country=self.country_id.code if self.country_id else None,
                        dob=(self.dob or "").strip() or None,
                    )
            except SanctixAPIError as exc:
                _logger.error("Sanctix ad-hoc screening failed for %r: %s", self.name, exc)
                raise UserError(_("Screening failed: %s") % exc) from exc
        finally:
            client.close()
        self._store_result(response)
        # Reopen THIS wizard on top so the verdict card renders immediately —
        # a bare notification would leave the user staring at the input form
        # with no idea what came back (platform shows the result screen).
        return {
            "type": "ir.actions.act_window",
            "res_model": "sanctix.screen.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "views": [(self.env.ref("sanctix_compliance.view_sanctix_screen_wizard_form").id, "form")],
            "target": "new",
        }

    def action_screen_and_save(self):
        """File the result on a Contact — billed exactly once.

        Reuses the already-paid wizard response (no second API call) and, if
        a contact with the same name already exists, files on it instead of
        creating a duplicate.
        """
        self.ensure_one()
        self._check_group()
        if not self.result_verdict:
            self.action_screen()
        partner = self.env["res.partner"].search(
            [("name", "=ilike", self.name.strip())], limit=1
        )
        created = False
        if not partner:
            created = True
            partner = self.env["res.partner"].create(
                {
                    "name": self.name.strip(),
                    "country_id": self.country_id.id or False,
                    # Created silent: the report below is built from the response
                    # we already paid for, so the create-hook must not re-screen.
                    "sanctix_auto_screen": False,
                }
            )
        try:
            entity = self._entity_shape()
            report = (
                self.env["sanctix.screening.report"]
                .sudo()
                .create_from_api_response(partner, entity, "full" if self.result_is_full else "manual")
            )
            if self.result_ai:
                report.sudo().write({"ai_assessment": self.result_ai})
        finally:
            # Re-enable automation for the future regardless.
            partner.sudo().write({"sanctix_auto_screen": True})
        partner.sudo().write(
            {
                "sanctix_risk_level": report.risk_level,
                "sanctix_last_screened": fields.Datetime.now(),
                "sanctix_report_reference": report.report_reference,
            }
        )
        if created:
            message = _("Screened and saved as a new contact (1 screen billed).")
        else:
            message = _("Screened and filed on the existing contact '%s' (1 screen billed).") % partner.name
        return {
            "type": "ir.actions.act_window",
            "res_model": "res.partner",
            "res_id": partner.id,
            "view_mode": "form",
            "target": "current",
            "context": {"sanctix_notice": message},
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_group(self):
        if not (self.env.su or self.env.user.has_group("sanctix_compliance.group_sanctix_user")):
            raise UserError(_("You are not allowed to run Sanctix screenings."))

    def _get_client(self) -> SanctixAPIClient:
        # Same credential lookup res.partner uses, without needing a partner.
        icp = self.env["ir.config_parameter"].sudo()
        api_key = icp.get_param(f"sanctix_compliance.api_key.{self.env.company.id}") or icp.get_param(
            "sanctix_compliance.api_key"
        )
        base_url = icp.get_param("sanctix_compliance.base_url") or None
        if not api_key:
            raise UserError(
                _("No Sanctix API key configured for company %(company)s. Go to Settings > Sanctix Compliance.")
                % {"company": self.env.company.name}
            )
        return SanctixAPIClient(api_key=api_key, base_url=base_url)

    def _entity_shape(self) -> dict:
        """Rebuild an entity-shaped response dict from the cached payload for
        create_from_api_response (works for both /screen/entity and
        /screen/full shapes — full-shape hits live under entity_screen)."""
        try:
            cached = json.loads(self.result_payload or "{}")
        except (TypeError, ValueError):
            cached = {}
        entity = cached.get("entity_screen") if isinstance(cached.get("entity_screen"), dict) else {}
        hits = [h for h in (entity.get("all_hits") or cached.get("all_hits") or []) if isinstance(h, dict)]
        return {
            "verdict": self.result_verdict or cached.get("verdict") or "CLEAR",
            "all_hits": hits,
            "ref": self.result_ref or cached.get("ref") or "",
            "final_score": self.result_score,
        }

    def _store_result(self, response: dict) -> None:
        """Cache the verdict (+ AI memo) on the wizard and render the
        platform-style result card."""
        from ..models.sanctix_screening_report import _VERDICT_TO_RISK_LEVEL
        from ..models.sanctix_style import markdown_to_html, risk_pill_html, score_donut_svg

        import html as _html

        verdict = response.get("verdict", "CLEAR")
        risk = _VERDICT_TO_RISK_LEVEL.get(verdict, "clear")
        score = response.get("final_score") if isinstance(response.get("final_score"), int) else None
        entity = response.get("entity_screen") if isinstance(response.get("entity_screen"), dict) else None
        hits = [h for h in ((entity or {}).get("all_hits") or response.get("all_hits") or []) if isinstance(h, dict)]
        hits = sorted(hits, key=lambda h: h.get("score") or 0, reverse=True)[:3]
        rows = "".join(
            "<div style='display:flex;justify-content:space-between;gap:8px;padding:6px 0;"
            "border-top:1px solid #E8EBF0;font-size:12px;'>"
            "<span style='color:#0B1220;font-weight:600;'>%s</span>"
            "<span style='color:#5A6577;'>%s%s</span></div>"
            % (
                _html.escape(str(h.get("name") or "Unnamed record")),
                _html.escape(str(h.get("source") or "")),
                (" · Score %d" % h["score"]) if isinstance(h.get("score"), int) else "",
            )
            for h in hits
        )
        ai_markdown = response.get("ai_report") if isinstance(response.get("ai_report"), str) else ""
        ai_html = markdown_to_html(ai_markdown)
        self.write(
            {
                "result_verdict": verdict,
                "result_score": score if score is not None else 0,
                "result_ref": response.get("ref") or "",
                "result_ai": ai_markdown,
                "result_is_full": bool(response.get("ai_report")) or "entity_screen" in response,
                "result_payload": json.dumps(response, default=str),
                "result_html": (
                    '<div style="border:1px solid #E8EBF0;border-radius:7px;padding:16px;display:flex;'
                    'gap:16px;align-items:center;font-family:-apple-system,Segoe UI,Inter,Roboto,Arial,sans-serif;">'
                    f"<div>{score_donut_svg(score, risk)}</div>"
                    "<div><div style='margin-bottom:6px;'>"
                    f"{risk_pill_html(risk, verdict.replace('_', ' ').title())}</div>"
                    f"<div style='font-size:12px;color:#5A6577;'>Sanctix reference {_html.escape(response.get('ref') or '—')}</div>"
                    "</div></div>"
                    + (
                        "<div style='font-size:13px;font-weight:600;color:#0B1220;margin:12px 0 4px;'>"
                        f"Top matches ({len(hits)})</div>{rows}"
                        if rows
                        else "<div style='font-size:13px;color:#186A3B;margin-top:12px;'>"
                        "✓ No matches — clear against all screened lists.</div>"
                    )
                    + (
                        "<div style='font-size:13px;font-weight:600;color:#0B1220;margin:12px 0 4px;'>"
                        "Screening assessment</div>"
                        f"<div>{ai_html}</div>"
                        if ai_html
                        else ""
                    )
                    + "<div style='font-size:12px;color:#5A6577;margin-top:12px;'>"
                    "This result is not filed anywhere yet — use “Screen &amp; Save as Contact” "
                    "to keep it in Screening Reports.</div>"
                ),
            }
        )
