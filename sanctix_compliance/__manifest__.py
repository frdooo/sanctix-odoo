{
    "name": "Sanctix Sanctions & Compliance Screening",
    # One branch per Odoo series (17.0 / 18.0 / 19.0) with IDENTICAL code —
    # only this version prefix changes per branch. Odoo 18+ enforces the
    # format at install time (adapt_version): a 17.0.x.y.z manifest will NOT
    # install on 18.0 ("invalid manifest"), and vice versa. This checkout
    # targets the Odoo 18 test instance on this machine.
    "version": "18.0.4.0.0",
    "category": "Compliance",
    "summary": "Automated sanctions, PEP and adverse media screening for Contacts via the Sanctix API",
    "description": """
Sanctix Compliance Screening
=============================
Screens Contacts (res.partner) against 11+ global sanctions lists, PEP
registries, and adverse media using the Sanctix API
(https://sanctix.com), directly from Odoo.

Features
--------
* Manual "Screen Now" button on any Contact
* Automatic screening on Contact creation and on key field changes
  (name, country) — configurable
* Scheduled re-screening of existing Contacts on a rolling interval, so a
  contact that was clean six months ago and has since been sanctioned
  gets caught without anyone re-checking manually
* Full screening history per Contact — every report, its risk verdict,
  and a "View Full Report" action that pulls the complete stored report
  straight from Sanctix
* Batch screening wizard to screen a whole selection at once (e.g. all
  contacts of a Freight Forwarding customer)
* Document screening: attach an invoice, bill of lading or packing list
  (PDF/JPEG/PNG/WebP, max 10 MB) and Sanctix reads the parties named on
  it and screens each one — one audit row per party, contact rolls up to
  the worst verdict. Needs a Growth plan or above.
* Screening studio: dashboard with KPIs, verdict donut and hits-by-source
  charts, "Screen a Name" ad-hoc search (no contact needed), and an
  official-style printable PDF report per screening (verdict panel, match
  analysis with signal bars) — no raw JSON anywhere in the UI.
* Org-level API key stored per company (multi-company safe) — never
  logged, never exposed to non-admin users
* Handles Sanctix's plan-based rate limits and quota errors gracefully,
  with retry/backoff and clear error surfacing instead of silent failure
* Two dedicated security groups: Sanctix User (screen, view reports) and
  Sanctix Manager (configure API key, manage scheduled screening)
""",
    "author": "Sanctix",
    "website": "https://sanctix.com",
    "license": "OPL-1",
    # NOTE: `mail` intentionally NOT in depends — this module does not use
    # chatter/followers (mail.thread). Depending on mail pulls in the 19.0
    # mail restructure (mail.thread.main.attachment) for no benefit.
    "depends": ["base", "contacts"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/sanctix_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        # menu_sanctix_root is defined in sanctix_screening_report_views.xml,
        # so it MUST load before anything adding child menus. Wizard actions
        # referenced by views/menus load before their referrers.
        # Order matters here.
        "views/sanctix_screening_report_views.xml",
        "wizards/sanctix_batch_screen_views.xml",
        "wizards/sanctix_document_screen_views.xml",
        "wizards/sanctix_screen_views.xml",
        "views/sanctix_dashboard_views.xml",
        "views/res_partner_views.xml",
        "views/res_config_settings_views.xml",
        "reports/sanctix_report_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "sanctix_compliance/static/src/scss/sanctix_backend.scss",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
