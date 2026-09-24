from __future__ import annotations

import html
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .sanctix_api_client import SanctixAPIError
from .sanctix_style import markdown_to_html, risk_pill_html, score_donut_svg

# The Sanctix screening engine's verdict vocabulary (screening_engine.py
# `_verdict()`) is CLEAR / POSSIBLE_MATCH / FLAG_FOR_REVIEW /
# DO_NOT_TRANSACT, in increasing severity order driven by the same score
# thresholds as `confidence`. Odoo's `risk_level` selection predates that
# API and uses different words — this is the one place that translates
# between them. "high" has no dedicated verdict on the API side (the
# engine collapses everything above the medium threshold straight to
# FLAG_FOR_REVIEW or DO_NOT_TRANSACT), so it's intentionally unused here
# rather than guessed at.
_VERDICT_TO_RISK_LEVEL = {
    "CLEAR": "clear",
    "POSSIBLE_MATCH": "low",
    "FLAG_FOR_REVIEW": "medium",
    "DO_NOT_TRANSACT": "match",
}


class SanctixScreeningReport(models.Model):
    """One row per screening call ever made. Deliberately append-only from
    the UI's perspective (no edit/delete in the view's action buttons) so
    this doubles as a local compliance audit trail, mirroring the
    hash-chained audit trail Sanctix keeps server-side. If a report is
    later re-verified or disputed, this table is the record of what Odoo
    knew and when."""

    _name = "sanctix.screening.report"
    _description = "Sanctix Screening Report"
    _order = "create_date desc"
    _rec_name = "report_reference"

    partner_id = fields.Many2one(
        "res.partner", string="Contact", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", string="Company", required=True, default=lambda self: self.env.company, index=True
    )
    report_reference = fields.Char(
        string="Sanctix Report Reference",
        # NOT required: failed calls (risk_level=error) have no reference by
        # definition, and forcing "" into a required field breaks NOT NULL
        # semantics. Empty means "call never produced a report".
        required=False,
        copy=False,
        index=True,
        help="Opaque reference id from Sanctix. Batch, document and full "
        "(ad-hoc + AI) screenings are stored server-side — single-entity "
        "screenings return an ephemeral reference that Sanctix does not "
        "keep, so 'View Full Report' is only offered on stored rows.",
    )
    screened_name = fields.Char(
        string="Name Screened",
        required=True,
        help="The exact name string sent to Sanctix at screening time — "
        "kept even if the Contact's name is later edited, so this row "
        "always reflects what was actually checked.",
    )
    entity_type = fields.Selection(
        [("individual", "Individual"), ("organization", "Organization")],
        required=True,
        default="individual",
    )
    risk_level = fields.Selection(
        [
            ("clear", "Clear"),
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("match", "Confirmed Match"),
            ("error", "Screening Failed"),
        ],
        string="Risk Level",
        required=True,
        index=True,
    )
    match_count = fields.Integer(string="Potential Matches", default=0)
    triggered_by = fields.Selection(
        [("manual", "Manual"), ("auto_create", "Auto (Contact Created)"), ("auto_write", "Auto (Field Changed)"), ("scheduled", "Scheduled Re-screen"), ("batch", "Batch Wizard"), ("document", "Document (OCR)"), ("full", "Ad-hoc Full + AI")],
        string="Triggered By",
        required=True,
        default="manual",
    )
    raw_response = fields.Text(
        string="Raw API Response",
        help="Full JSON payload from Sanctix, kept for audit/debugging. "
        "Not shown by default in the main view — open the report to see it.",
    )
    error_message = fields.Char(
        string="Error",
        help="Populated instead of a risk_level result if the screening "
        "call itself failed (network, quota, auth) — a failed CALL is not "
        "the same as a CLEAR result, and this table distinguishes the two "
        "so 'no result' is never silently read as 'safe'.",
    )
    full_report_payload = fields.Text(
        string="Full Stored Report",
        readonly=True,
        help="The complete report as currently stored by Sanctix (GET "
        "/reports/{ref}), fetched on demand via 'View Full Report' below — "
        "not populated automatically, since it costs an extra API call for "
        "data most screenings never need to revisit.",
    )
    risk_badge_html = fields.Html(
        string="Status",
        compute="_compute_risk_badge_html",
        sanitize=False,
        help="Sanctix-style verdict pill (same colors as the dashboard) for "
        "the report header.",
    )
    report_detail_html = fields.Html(
        string="Report",
        compute="_compute_report_detail_html",
        sanitize=False,
        help="Platform-style screening report (verdict, score, matches) "
        "rendered from the stored response — no raw JSON shown.",
    )
    ai_assessment = fields.Text(
        string="Screening Assessment",
        readonly=True,
        copy=False,
        help="AI memo from Sanctix (POST /screen/full), stored as markdown; "
        "rendered as formatted text in the report, never as JSON.",
    )

    @api.depends("risk_level")
    def _compute_risk_badge_html(self):
        labels = dict(self._fields["risk_level"].selection)
        for report in self:
            report.risk_badge_html = risk_pill_html(
                report.risk_level, labels.get(report.risk_level, report.risk_level or "")
            )

    def action_download_pdf(self) -> dict:
        """Platform-style one-click download of the official PDF report
        (no print-options dialog — config=False)."""
        self.ensure_one()
        return self.env.ref("sanctix_compliance.action_report_sanctix_official").report_action(self, config=False)

    def action_view_full_report(self) -> dict:
        """Pulls the complete stored report from GET /reports/{ref} using
        this report's own company's API key, and stores it on
        full_report_payload for display. Org-scoped server-side — this can
        only ever fetch a report this Sanctix org itself created."""
        self.ensure_one()
        if not self.report_reference:
            raise UserError(_("This screening has no report reference to look up (it may have failed to run)."))
        client = self.partner_id.with_company(self.company_id)._get_sanctix_client()
        try:
            full_report = client.get_report(self.report_reference)
        except SanctixAPIError as exc:
            if exc.status_code == 404:
                raise UserError(
                    _("Sanctix no longer has this report (it keeps single screenings only briefly — "
                      "batch and document reports stay available). The summary above is your local record.")
                ) from exc
            raise UserError(_("Could not fetch the full report from Sanctix: %s") % exc) from exc
        finally:
            client.close()
        # Sanctix User only has read access to this model (see
        # ir.model.access.csv) — sudo() here is the same pattern
        # res.partner._sanctix_screen already uses to cache API results,
        # not a privilege escalation: the caller already proved they can
        # reach this specific report's data by opening its form.
        self.sudo().write({"full_report_payload": json.dumps(full_report, indent=2, default=str)})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Full report loaded"),
                "message": _("See the 'Full Stored Report' tab below."),
                "type": "success",
            },
        }

    @api.model
    def create_from_api_response(self, partner, response: dict, triggered_by: str) -> "SanctixScreeningReport":
        """Normalizes a raw Sanctix API response (POST /screen/entity shape:
        verdict/confidence/final_score/all_hits/ref — see
        screening_engine.py ScreeningResult.to_dict()) into a report row.
        Kept here (not in the client) because this is the one place that
        should know the *Odoo* shape of a report; the API client itself
        stays Odoo-agnostic."""
        verdict = response.get("verdict", "CLEAR")
        return self.create(
            {
                "partner_id": partner.id,
                "company_id": partner.company_id.id or self.env.company.id,
                "report_reference": response.get("ref") or "",
                "screened_name": partner.name or "",
                "entity_type": "organization" if partner.is_company else "individual",
                "risk_level": _VERDICT_TO_RISK_LEVEL.get(verdict, "clear"),
                "match_count": len(response.get("all_hits") or []),
                "triggered_by": triggered_by,
                "raw_response": json.dumps(response, default=str),
            }
        )

    @api.model
    def create_error_record(self, partner, error_message: str, triggered_by: str) -> "SanctixScreeningReport":
        """A failed screening CALL is recorded distinctly from a CLEAR
        result — 'error' is its own risk_level, never silently coerced to
        'clear', so a network/quota failure can never be misread later as
        'this contact was checked and is safe'."""
        return self.create(
            {
                "partner_id": partner.id,
                "company_id": partner.company_id.id or self.env.company.id,
                "report_reference": "",
                "screened_name": partner.name or "",
                "entity_type": "organization" if partner.is_company else "individual",
                "risk_level": "error",
                "match_count": 0,
                "triggered_by": triggered_by,
                "error_message": error_message,
            }
        )

    # Roles the extractor emits for business counterparties (as opposed to
    # vessels/people) — app/core/document_extraction.py. Used only to pick a
    # display entity_type for the report row; the API itself screens on name
    # regardless of type.
    _DOCUMENT_ORG_ROLES = frozenset(
        {"seller", "buyer", "consignee", "shipper", "notify_party", "carrier", "vessel", "bank", "agent", "manufacturer"}
    )

    @api.model
    def create_from_document_party(
        self, partner, party: dict, document_filename: str, triggered_by: str = "document"
    ) -> "SanctixScreeningReport":
        """Normalizes one POST /screen/document `parties[]` entry
        ({name, role, country, page, screening{ref, verdict, …}}) into a
        report row. The whole party dict is kept as raw_response so the
        role/page/country the extractor saw is always auditable, even
        though Odoo has no columns for them."""
        screening = party.get("screening") or {}
        verdict = screening.get("verdict", "CLEAR")
        match_count = (
            screening.get("total_hits")
            if isinstance(screening.get("total_hits"), int)
            else len(screening.get("all_hits") or [])
            or len((screening.get("entity_screen") or {}).get("all_hits") or [])
        )
        return self.create(
            {
                "partner_id": partner.id,
                "company_id": partner.company_id.id or self.env.company.id,
                "report_reference": screening.get("ref") or "",
                "screened_name": party.get("name") or "",
                "entity_type": "organization" if (party.get("role") in self._DOCUMENT_ORG_ROLES) else "individual",
                "risk_level": _VERDICT_TO_RISK_LEVEL.get(verdict, "clear"),
                "match_count": match_count,
                "triggered_by": triggered_by,
                "raw_response": json.dumps(
                    {"document": document_filename, "party": party}, default=str
                ),
            }
        )

    # ------------------------------------------------------------------
    # Platform-style report rendering (no raw JSON in views)
    # ------------------------------------------------------------------

    @api.depends("risk_level", "screened_name", "match_count", "triggered_by", "raw_response", "error_message",
                 "ai_assessment")
    def _compute_report_detail_html(self):
        labels = dict(self._fields["risk_level"].selection)
        trigger_labels = dict(self._fields["triggered_by"].selection)
        for report in self:
            report.report_detail_html = report._format_report_detail(
                labels.get(report.risk_level, ""), trigger_labels.get(report.triggered_by, "")
            )

    def _format_report_detail(self, risk_label: str, trigger_label: str) -> str:
        """Render the stored response as a dashboard-like report card.

        Handles all three stored shapes: single/batch entity responses
        ({verdict, final_score, all_hits[], …}), document party envelopes
        ({document, party{…, screening{…}}}), and error rows. Anything
        unparseable degrades to the header + meta rows, never raw JSON.
        """
        self.ensure_one()
        pill = risk_pill_html(self.risk_level, risk_label)
        if self.risk_level == "error":
            return (
                '<div style="border:1px solid #E8EBF0;border-radius:7px;padding:16px;'
                'font-family:-apple-system,Segoe UI,Inter,Roboto,Arial,sans-serif;">'
                f'<div style="margin-bottom:8px;">{pill}</div>'
                "<div style='font-size:14px;font-weight:600;color:#0B1220;'>Screening did not run</div>"
                f"<div style='font-size:13px;color:#5A6577;margin-top:4px;'>{html.escape(self.error_message or '')}</div>"
                "<div style='font-size:12px;color:#5A6577;margin-top:8px;'>"
                "This is a failed call, not a verdict — re-screen this contact.</div></div>"
            )

        screening, _party = self._stored_screening()
        verdict = screening.get("verdict") or "CLEAR"
        score = screening.get("final_score")
        score = score if isinstance(score, int) else None
        hits = screening.get("all_hits") or []
        if not isinstance(hits, list):
            hits = []
        top = screening.get("top_match")
        if isinstance(top, dict) and all(
            h.get("entity_id") != top.get("entity_id") for h in hits if isinstance(h, dict)
        ):
            hits = [top] + hits
        hits = sorted(
            [h for h in hits if isinstance(h, dict)], key=lambda h: h.get("score") or 0, reverse=True
        )[:5]

        created = self.create_date.strftime("%Y-%m-%d %H:%M") if self.create_date else ""
        meta_rows = [
            ("Screened name", self.screened_name or "—"),
            ("Checked on", created or "—"),
            ("Source", trigger_label or "—"),
        ]
        _stored, party = self._stored_screening()
        if party:
            if party.get("role"):
                meta_rows.append(("Document role", str(party["role"]).replace("_", " ").title()))
            if party.get("country"):
                meta_rows.append(("Document country", str(party["country"])))
            if party.get("page"):
                meta_rows.append(("Document page", str(party["page"])))
        if self.report_reference and self.triggered_by in ("batch", "document"):
            meta_rows.append(("Sanctix reference", self.report_reference))
        meta_html = "".join(
            "<tr><td style='padding:6px 12px 6px 0;font-size:12px;color:#5A6577;white-space:nowrap;'>%s</td>"
            "<td style='padding:6px 0;font-size:13px;color:#0B1220;font-weight:600;'>%s</td></tr>"
            % (html.escape(k), html.escape(v))
            for k, v in meta_rows
        )

        if self.risk_level == "clear" and not hits:
            matches_html = (
                "<div style='border:1px solid #C3E3CE;background:#EEF8F1;border-radius:7px;"
                "padding:12px 16px;font-size:13px;color:#186A3B;'>"
                "✓ No matches — this name is clear against all screened lists.</div>"
            )
        else:
            cards = []
            for hit in hits:
                name = str(hit.get("name") or "Unnamed record")
                source = str(hit.get("source") or "")
                programs = hit.get("programs") or []
                countries = hit.get("countries") or []
                hscore = hit.get("score") if isinstance(hit.get("score"), int) else None
                details = " · ".join(
                    part
                    for part in [
                        source,
                        ", ".join(programs[:3]) if isinstance(programs, list) else "",
                        ", ".join(countries[:3]) if isinstance(countries, list) else "",
                    ]
                    if part
                )
                bar = (
                    "<div style='background:#E8EBF0;border-radius:3px;height:6px;margin-top:6px;'>"
                    "<div style='background:#0284C7;border-radius:3px;height:6px;width:%d%%;'></div></div>"
                    % max(0, min(100, hscore))
                    if hscore is not None
                    else ""
                )
                cards.append(
                    "<div style='border:1px solid #E8EBF0;border-radius:7px;padding:10px 14px;margin-top:8px;'>"
                    "<div style='display:flex;justify-content:space-between;gap:8px;align-items:baseline;'>"
                    "<div style='font-size:13px;font-weight:600;color:#0B1220;'>%s</div>"
                    "<div style='font-size:12px;color:#5A6577;font-variant-numeric:tabular-nums;'>%s</div></div>"
                    "%s%s</div>"
                    % (
                        html.escape(name),
                        ("Score %d" % hscore) if hscore is not None else "",
                        ("<div style='font-size:12px;color:#5A6577;margin-top:2px;'>%s</div>" % html.escape(details))
                        if details
                        else "",
                        bar,
                    )
                )
            matches_html = (
                "<div style='font-size:13px;font-weight:600;color:#0B1220;margin:12px 0 4px;'>"
                "Potential matches (%d)</div>%s" % (self.match_count or len(hits), "".join(cards) or
                    "<div style='font-size:12px;color:#5A6577;'>Details unavailable.</div>")
            )

        return (
            '<div style="border:1px solid #E8EBF0;border-radius:7px;padding:16px;'
            'font-family:-apple-system,Segoe UI,Inter,Roboto,Arial,sans-serif;background:#FFFFFF;">'
            '<div style="display:flex;gap:16px;align-items:center;">'
            f"<div>{score_donut_svg(score, self.risk_level)}</div>"
            '<div><div style="margin-bottom:6px;">' + pill + "</div>"
            "<div style='font-size:16px;font-weight:700;color:#0B1220;letter-spacing:-0.01em;'>"
            f"{html.escape(self.screened_name or '')}</div>"
            "<div style='font-size:12px;color:#5A6577;margin-top:2px;'>"
            f"Verdict {html.escape(str(verdict))}</div></div></div>"
            f"<table style='margin-top:12px;border-collapse:collapse;'>{meta_html}</table>"
            f"{matches_html}"
            f"{self._ai_section_html()}"
            "</div>"
        )

    def _ai_section_html(self) -> str:
        ai_html = markdown_to_html(self.ai_assessment)
        if not ai_html:
            return ""
        return (
            "<div style='font-size:13px;font-weight:600;color:#0B1220;margin:12px 0 4px;'>"
            "Screening assessment</div>"
            f"<div>{ai_html}</div>"
        )

    # ------------------------------------------------------------------
    # Official-style printable report (QWeb PDF data)
    # ------------------------------------------------------------------

    # Signal labels in the platform's MATCH SIGNAL BREAKDOWN order.
    _SIGNAL_LABELS = (
        ("token_sort", "Token Sort Ratio"),
        ("token_overlap", "Token Overlap"),
        ("fuzzy_wratio", "Fuzzy WRatio"),
        ("phonetic", "Phonetic Match"),
        ("exact_normalized", "Exact Normalized"),
    )
    _CORROBORATION_LABELS = (
        ("dob_delta", "DOB Match"),
        ("country_delta", "Country Match"),
        ("id_delta", "ID Match"),
    )

    _RISK_TO_TONE = {
        "match": "blocked",
        "high": "review",
        "medium": "review",
        "error": "review",
        "low": "caution",
        "clear": "clear",
        "unscreened": "pending",
    }

    @api.model
    def _tone_colors(self, risk_level: str) -> dict:
        """Solid panel colors for the printable result block, one per tone."""
        tones = {
            "blocked": {"bg": "#991B1B", "soft_bg": "#FDF0F0", "border": "#F3C9C9", "fg": "#991B1B"},
            "review": {"bg": "#9A3412", "soft_bg": "#FDF3EC", "border": "#F3D4BD", "fg": "#9A3412"},
            "caution": {"bg": "#8A5300", "soft_bg": "#FDF7E8", "border": "#EFDCAE", "fg": "#8A5300"},
            "clear": {"bg": "#186A3B", "soft_bg": "#EEF8F1", "border": "#C3E3CE", "fg": "#186A3B"},
            "pending": {"bg": "#3D4757", "soft_bg": "#F1F3F7", "border": "#D8DDE5", "fg": "#3D4757"},
        }
        return tones[self._RISK_TO_TONE.get(risk_level or "", "caution")]

    def _stored_screening(self) -> tuple[dict, dict]:
        """Return (response_dict, party_dict) from raw_response, tolerating
        entity, batch-row, document-envelope, and garbage shapes."""
        try:
            stored = json.loads(self.raw_response or "{}")
        except (TypeError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        party = stored.get("party") if isinstance(stored.get("party"), dict) else {}
        screening = party.get("screening") if isinstance(party.get("screening"), dict) else stored
        if not isinstance(screening, dict):
            screening = {}
        return screening, party

    def _prepare_official_report(self) -> dict:
        """Flatten one report into plain values for the QWeb template (and
        unit tests). No ORM, no HTML — the template owns presentation."""
        self.ensure_one()
        screening, party = self._stored_screening()
        verdict = screening.get("verdict") or "CLEAR"
        score = screening.get("final_score")
        score = score if isinstance(score, int) else None
        confidence = screening.get("confidence") or ""
        hits = screening.get("all_hits") or []
        hits = sorted(
            [h for h in hits if isinstance(h, dict)], key=lambda h: h.get("score") or 0, reverse=True
        )
        total = screening.get("total_hits") if isinstance(screening.get("total_hits"), int) else len(hits)
        query = screening.get("query") if isinstance(screening.get("query"), dict) else {}

        # Coverage per source, from hits (plus server-known screened sources
        # on persisted rows, marked as not-returned when they gave nothing).
        by_source: dict[str, dict] = {}
        for hit in hits:
            src = str(hit.get("source") or "unknown")
            entry = by_source.setdefault(src, {"source": src, "count": 0, "top": 0})
            entry["count"] += 1
            entry["top"] = max(entry["top"], hit.get("score") or 0)
        screened_sources = screening.get("sources_screened") or []
        if isinstance(screened_sources, list):
            for item in screened_sources:
                src = item.get("source") if isinstance(item, dict) else item
                if src and str(src) not in by_source:
                    by_source[str(src)] = {"source": str(src), "count": 0, "top": 0}
        coverage = sorted(by_source.values(), key=lambda e: (-e["count"], -e["top"]))

        # Donut segments (shares of hits per source).
        palette = ["#B91C1C", "#1D4ED8", "#6B7280", "#D97706", "#0E7490", "#7C3AED", "#15803D"]
        segments = []
        offset = 0.0
        for i, entry in enumerate(coverage):
            frac = (entry["count"] / total) if total else 0
            segments.append(
                {"color": palette[i % len(palette)], "fraction": frac, "offset": offset,
                 "label": entry["source"], "count": entry["count"]}
            )
            offset += frac

        # Ranked matches (top 5) with signal bars for the top match.
        matches = []
        for rank, hit in enumerate(hits[:5], start=1):
            signals = hit.get("signal_breakdown") if isinstance(hit.get("signal_breakdown"), dict) else {}
            matches.append(
                {
                    "rank": rank,
                    "name": str(hit.get("name") or "Unnamed record"),
                    "source": str(hit.get("source") or ""),
                    "source_id": str(hit.get("source_id") or ""),
                    "programs": ", ".join((hit.get("programs") or [])[:3]),
                    "entity_type": str(hit.get("entity_type") or ""),
                    "listed_on": str(hit.get("listed_on") or "—"),
                    "score": hit.get("score") if isinstance(hit.get("score"), int) else None,
                    "matched_on": str((signals.get("matched_on") or hit.get("matched_on") or "primary_name")).replace("_", " "),
                    "signals": [
                        {"label": label, "value": signals.get(key) or 0}
                        for key, label in self._SIGNAL_LABELS
                    ],
                    "corroboration": [
                        {"label": label, "value": signals.get(key) or 0}
                        for key, label in self._CORROBORATION_LABELS
                    ],
                    "name_subtotal": signals.get("name_subtotal") or 0,
                    "final": signals.get("final") if isinstance(signals.get("final"), int) else hit.get("score"),
                }
            )

        # Executive summary, generated strictly from the data.
        given = [k for k in ("dob", "nationality", "country", "id_number") if query.get(k)]
        missing = [k for k in ("date of birth", "nationality", "country", "government identifier")
                   if k.split()[-1] not in {g.replace("government identifier", "id_number") for g in given}]
        top = matches[0] if matches else None
        if top:
            summary = (
                "The submitted name %s returned %d potential match%s across %s. "
                "The highest-scoring result is a %s record for '%s' at %s/100. "
                % (
                    self.screened_name or "—", total, "" if total == 1 else "es",
                    ", ".join(e["source"] for e in coverage) or "the screened sources",
                    top["source"], top["name"],
                    top["score"] if top["score"] is not None else "—",
                )
            )
        else:
            summary = "The submitted name %s returned no potential matches across the screened sources. " % (
                self.screened_name or "—"
            )
        if missing:
            summary += (
                "The submitted input contains no %s, so flagged results require identity review "
                "rather than a definitive match determination." % ", ".join(missing)
            )
        else:
            summary += "Corroborating identifiers were supplied with the query."

        if self.risk_level == "clear":
            assessment = (
                "No name-similarity matches met the screening threshold. A no-match result does not "
                "guarantee the absence of sanctions or other risk — only that nothing matched at "
                "screening time with the supplied input."
            )
        else:
            assessment = (
                "Name-similarity matches were identified across the screened sources. Because the "
                "query relies on name matching, these are potential matches that require additional "
                "review and verification against identifying information before any determination."
            )

        return {
            "report": self,
            "verdict": verdict,
            "verdict_label": verdict.replace("_", " "),
            "logo_data_uri": self._sanctix_logo_data_uri(),
            "risk_label": dict(self._fields["risk_level"].selection).get(self.risk_level, ""),
            "score": score,
            "confidence": confidence,
            "tone": self._tone_colors(self.risk_level),
            "screened_on": self.create_date.strftime("%d %b %Y %H:%M:%S UTC") if self.create_date else "",
            "trigger_label": dict(self._fields["triggered_by"].selection).get(self.triggered_by, ""),
            "reference": self.report_reference or "—",
            "entity_type_label": dict(self._fields["entity_type"].selection).get(self.entity_type, ""),
            "threshold_note": "Name screening only",
            "elapsed": screening.get("elapsed_ms"),
            "query": query,
            "summary": summary,
            "assessment": assessment,
            "total": total,
            "coverage": coverage,
            "segments": segments,
            "matches": matches,
            "extra_matches": max(0, total - len(matches)),
            "party": party,
            "document_file": self._stored_document_name(),
            "ai_html": markdown_to_html(self.ai_assessment),
        }

    def _stored_document_name(self) -> str | None:
        """Filename from a document envelope, tolerating garbage payloads."""
        try:
            stored = json.loads(self.raw_response or "{}")
        except (TypeError, ValueError):
            return None
        if isinstance(stored, dict):
            return stored.get("document")
        return None

    @api.model
    def _sanctix_logo_data_uri(self) -> str | None:
        """Sanctix shield mark for the PDF header (shared helper)."""
        from .sanctix_style import sanctix_logo_data_uri

        return sanctix_logo_data_uri()
