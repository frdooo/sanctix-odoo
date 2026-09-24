# Sanctix Sanctions & Compliance Screening — Odoo Module

Screens Contacts (`res.partner`) in Odoo against sanctions lists, PEP
registries, and adverse media using the [Sanctix API](https://sanctix.com),
without leaving Odoo.

## What it does

- **Dashboard** — Sanctix Compliance → Dashboard: KPI tiles (screenings,
  clearance rate, blocked, contacts screened), verdict donut, hits-by-source
  bars, live quota (Refresh Quota is free), and launchers for every flow.
  Charts menu adds native bar/pivot breakdowns over the audit table.
- **Screen a Name** — Sanctix Compliance → Screen a Name: type any person or
  organization (like the platform search box), see the verdict with score
  ring and top matches. Nothing is saved unless you choose Screen & Save —
  which files the audit row from the already-paid response (never billed
  twice), reusing the contact if the name already exists. Tick **Include
  screening assessment (AI)** (default on) to call the full screening: still
  1 screen, but the response adds an AI memo and a server-stored reference
  (Growth plan with AI enabled). The memo renders in the result card, the
  filed report, and the PDF.
- **Manual screening** — a "Screen Now" button on every Contact form.
- **Automatic screening** — new contacts are screened on creation; existing
  contacts are re-screened automatically if their name or country changes.
- **Scheduled re-screening** — a weekly cron job re-checks existing contacts
  on a rolling basis (default: anything not screened in the last 30 days),
  so a contact that gets added to a sanctions list *after* you originally
  cleared them doesn't fall through the cracks.
- **Batch screening wizard** — select any number of Contacts in the list
  view and screen them all in a single `POST /screen/batch` call (capped at
  200 per batch to avoid request timeouts), retried safely on network
  failure via an idempotency key so a re-click can't double-screen.
- **Document (OCR) screening** — on any Contact, *Screen Document* accepts
  an invoice, bill of lading, packing list or similar (`POST
  /screen/document`). Sanctix extracts the parties named on the file and
  screens each one: one `sanctix.screening.report` row per party
  (`triggered_by = Document (OCR)`), and the Contact rolls up to the
  **worst** party verdict. Notes:
  - Needs a **Growth plan or above** (server returns 403 otherwise).
  - Costs **max(1, parties found)** screens — a file with no identifiable
    parties still bills 1 screen for the extraction.
  - Limits: PDF/JPEG/PNG/WebP only, max **10 MB** decoded, max **10 pages**,
    max **5 calls/minute**. There is no idempotency key on this endpoint,
    so the wizard never re-sends a call that already answered.
- **Full audit trail** — every screening call, successful or failed, is
  logged as a `sanctix.screening.report` record linked to the Contact.
  Reports render platform-style (verdict pill, score ring, ranked matches
  with score bars) — raw JSON is kept in the database for audit but never
  shown in the UI. Each report also prints as an **official-style PDF**
  (subject block, verdict panel, executive summary, source coverage, match
  analysis with signal bars, assessment, review actions) via the **Download
  PDF** button or the Print menu — headered with the Sanctix shield from the
  platform frontend.
- **Multi-company safe** — the API key is stored per company; screening
  reports are scoped to the company that ran them.

## Notes on references

Single screenings (manual, auto, scheduled, plain ad-hoc) return an
**ephemeral** Sanctix reference that Sanctix does not store — "View Full
Report" is therefore only offered on **batch, document and full (ad-hoc +
AI)** rows, whose references resolve via `GET /reports/{ref}`. The
on-screen summary and the PDF always work from the local record regardless.

## Requirements

- Odoo 17.0, 18.0, or 19.0 — identical code on one branch per series
  (only the `__manifest__.py` version prefix differs: `17.0.x`, `18.0.x`,
  `19.0.x`). This is required, not just convention: Odoo 18+ rejects a
  mismatched 4-part version at install time (`adapt_version` → "invalid
  manifest"). Views use `<list>` (not `<tree>`), attrs-free
  `invisible="..."`, and `list,form` view modes, which is the correct
  syntax for all three. Do NOT backport `<tree>`/`attrs` for 16.0 and
  below without a separate branch.
- The `ir.cron` record deliberately omits `numbercall` (removed in 19.0;
  the default repeats forever), and the module does NOT depend on `mail`
  (avoids the 19.0 `mail.thread` restructure — no chatter is used).
- A Sanctix account with **API access enabled on your plan**. Free/Trial
  plans cannot issue API keys — this is enforced server-side, not just a
  pricing-page note (see `plans.py` / `api_access_enabled` if you're
  Sanctix engineering reading this from the backend repo).
- The `requests` Python package (bundled with every standard Odoo install).

## Installation

1. Copy the `sanctix_compliance/` folder into your Odoo `addons` path.
2. Restart the Odoo server.
3. Go to **Apps**, remove the "Apps" filter, search "Sanctix", click **Install**.

## Configuration

1. In Sanctix, go to **Settings → API Keys** and create a key (requires
   admin role and a plan with API access — Starter tier and above).
2. In Odoo, go to **Settings → Sanctix Compliance** (only visible to users
   in the **Sanctix Manager** group).
3. Paste the API key, click **Save**, then **Test Connection** to confirm
   it works and see your remaining quota.
4. Adjust the automatic-screening toggles as needed. All three
   (auto-screen on create, auto-screen on name/country change, scheduled
   re-screening) default to **on**.

## Security groups

- **Sanctix User** — can screen contacts manually or via the batch wizard,
  and view screening history. Cannot see or change the API key.
- **Sanctix Manager** — everything a User can do, plus configuring the API
  key and automation settings. Granted to the Odoo admin user by default.

Assign these under **Settings → Users → [user] → Sanctix Compliance**.

## Notes on API usage

- Only **top-level contacts are auto-screened** — child contacts (e.g. an
  "Invoicing" or "Shipping" sub-address under a company) are skipped by
  default, since screening the parent company is what matters for
  compliance and screening every sub-address would multiply API usage for
  no benefit. If you need child contacts screened too, this is a one-line
  change in `models/res_partner.py` (`create()` / `write()` — remove the
  `not partner.parent_id` condition).
- A failed screening call (network issue, expired key, quota exceeded) is
  recorded as its own `error` status — it is never silently treated as
  "Clear". Check **Screening History** on the Contact if the status shows
  "Screening Failed".
- The scheduled re-screen job processes contacts in bounded batches per
  company per run rather than the whole database at once, so a large
  contact list drains gradually across multiple weekly runs instead of
  risking one run timing out or exhausting your quota in a single go.
- The API's screening verdicts (`CLEAR` / `POSSIBLE_MATCH` /
  `FLAG_FOR_REVIEW` / `DO_NOT_TRANSACT`) are translated into Odoo's
  `sanctix_risk_level` selection (`clear` / `low` / `medium` / `match`) by
  the `_VERDICT_TO_RISK_LEVEL` mapping in
  `models/sanctix_screening_report.py` — update that mapping, not the
  selection field, if you need finer-grained status buckets.
- **Report verification**: a stored report's "View Full Report" button
  fetches the complete report via the authenticated `GET /reports/{ref}`
  endpoint. The API also exposes a *public*, unauthenticated
  `GET /verify/{ref}` for a counterparty to confirm a report is genuine —
  but that additionally requires the SHA-256 hash printed on a
  Sanctix-hosted report export, which this integration never receives, so
  it isn't offered as an in-Odoo action.

## Extending to HubSpot / other CRMs

This module talks to Sanctix purely over HTTP
(`models/sanctix_api_client.py`), with no Odoo-specific logic in the
client itself. The same client class (or its request/response shape) can
be reused as the basis for a HubSpot custom workflow action or any other
integration — see `SanctixAPIClient.screen_entity()` / `screen_batch()`
for the exact request shape, and `tests/test_sanctix_api_client.py` for
worked examples of the actual response shape (`verdict`/`confidence`/
`final_score`/`all_hits`/`ref`) to expect back.
