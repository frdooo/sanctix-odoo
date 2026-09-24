"""Thin, defensive HTTP client for the Sanctix Compliance API.

Kept deliberately separate from any Odoo model so it's easy to unit test
in isolation and easy to swap out (e.g. mock in CI) without touching
ORM code. Every method returns a plain dict/None — no Odoo recordsets
cross this boundary in either direction.
"""

from __future__ import annotations

import logging
import time

import requests

from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.sanctix.com/api/v1"
DEFAULT_TIMEOUT = 20  # seconds — screening can involve upstream lookups, give it room
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2  # doubles each retry: 2s, 4s, 8s

# POST /screen/document limits, mirrored from the backend so bad files fail
# fast in Odoo with a clear message instead of burning a round-trip:
# app/core/config.py DOCUMENT_SCREENING_MAX_BYTES/_PAGES, app/core/document_pages.py.
DOCUMENT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB decoded (wire ~13.4 MB base64)
DOCUMENT_ALLOWED_CONTENT_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png", "image/webp"})


class SanctixAPIError(Exception):
    """Raised for any non-2xx response the caller should see surfaced,
    after retries are exhausted. Carries the Sanctix error `code` when the
    API returned one, so calling code (e.g. the cron job) can decide
    whether to keep retrying next run or stop (e.g. quota_exceeded should
    not be retried immediately — retrying will not help until usage
    resets)."""

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class SanctixAPIClient:
    def __init__(self, api_key: str, base_url: str | None = None, timeout: int = DEFAULT_TIMEOUT):
        if not api_key:
            raise UserError(_("Sanctix API key is not configured. Set it in Settings > Sanctix Compliance."))
        self._api_key = api_key
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                # The Sanctix API resolves credentials as EITHER a JWT
                # bearer token (dashboard end users) OR an X-API-Key header
                # (server-to-server, which is what this module is). Sending
                # the API key as `Authorization: Bearer` routes it into the
                # JWT decode path and is rejected — this header is the one
                # the backend actually recognizes for API keys.
                "X-API-Key": self._api_key,
                "Content-Type": "application/json",
                "User-Agent": "sanctix-odoo-connector/2.0",
            }
        )

    def close(self) -> None:
        """Release the underlying HTTP session. Callers should close the
        client in a finally block — otherwise a cron/batch run over hundreds
        of contacts leaks one open Session (connection pool) per client."""
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 — close must never raise
            _logger.debug("Sanctix client session close failed", exc_info=True)

    def __enter__(self) -> "SanctixAPIClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def screen_entity(
        self,
        *,
        name: str,
        country: str | None = None,
        dob: str | None = None,
        nationality: str | None = None,
        id_number: str | None = None,
        threshold: int | None = None,
        limit: int | None = None,
        sources: list[str] | None = None,
    ) -> dict:
        """Screen a single entity against POST /screen/entity. `country` is
        ISO-2 or a full country name (the API accepts either). There is no
        `entity_type`/individual-vs-organization field on this endpoint —
        the API matches on name + corroborating signals regardless of
        whether the subject is a person or a company; Odoo tracks
        is_company separately, on the local screening report, purely for
        its own display."""
        payload: dict = {"name": name}
        if country:
            payload["country"] = country
        if dob:
            payload["dob"] = dob
        if nationality:
            payload["nationality"] = nationality
        if id_number:
            payload["id_number"] = id_number
        if threshold is not None:
            payload["threshold"] = threshold
        if limit is not None:
            payload["limit"] = limit
        if sources:
            payload["sources"] = sources
        return self._request("POST", "/screen/entity", json=payload)

    def screen_batch(
        self,
        queries: list[dict],
        *,
        threshold: int | None = None,
        limit: int | None = None,
        sources: list[str] | None = None,
        label: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Screen up to 500 queries in one call via POST /screen/batch.
        Each item in `queries` is a dict with at minimum {"name": ...} and
        optionally dob/country/nationality/id_number. Each item counts
        individually against the org's screening allowance — a 50-query
        batch uses 50 screens, same as calling screen_entity() 50 times,
        but as ONE HTTP round-trip and ONE stored batch (GET
        /reports/batches/{ref}), with idempotency support so a retried
        call can't double-screen or double-bill."""
        payload: dict = {"queries": queries}
        if threshold is not None:
            payload["threshold"] = threshold
        if limit is not None:
            payload["limit"] = limit
        if sources:
            payload["sources"] = sources
        if label:
            payload["label"] = label
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/screen/batch", json=payload, headers=headers)

    def get_report(self, report_reference: str) -> dict:
        """Fetch a full stored report (GET /reports/{ref}) — org-scoped,
        requires the same authenticated API key that created it. Used by
        the "View Full Report" action on a screening report."""
        return self._request("GET", f"/reports/{report_reference}")

    def get_usage(self) -> dict:
        """Used by the config settings screen to show remaining quota
        before a user schedules a large batch screen. Does NOT include the
        plan name/label — see get_subscription() for that."""
        return self._request("GET", "/billing/usage")

    def get_subscription(self) -> dict:
        """Plan name and billing-cycle status (GET /billing/subscription) —
        GET /billing/usage does not include a plan name, only counters."""
        return self._request("GET", "/billing/subscription")

    def screen_full(
        self,
        *,
        name: str,
        country: str | None = None,
        dob: str | None = None,
        nationality: str | None = None,
        id_number: str | None = None,
        threshold: int | None = None,
        generate_ai_report: bool = True,
        idempotency_key: str | None = None,
    ) -> dict:
        """Full compliance check via POST /screen/full: entity screen plus
        an AI assessment memo. Shipment fields are omitted (entity-only).

        Costs exactly 1 screen — the same unit /screen/entity costs — and
        the result IS persisted server-side (GET /reports/{ref} resolves,
        unlike single-entity refs). Supports Idempotency-Key: same key +
        same body replayed within 24h returns the original response without
        re-billing. `ai_report` in the response is markdown text, or None
        when the plan/server has AI disabled.
        """
        payload: dict = {"name": name, "generate_ai_report": generate_ai_report}
        if country:
            payload["country"] = country
        if dob:
            payload["dob"] = dob
        if nationality:
            payload["nationality"] = nationality
        if id_number:
            payload["id_number"] = id_number
        if threshold is not None:
            payload["threshold"] = threshold
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/screen/full", json=payload, headers=headers)

    def screen_document(
        self,
        *,
        filename: str,
        content_type: str,
        content_base64: str,
        threshold: int | None = None,
        limit: int | None = None,
        sources: list[str] | None = None,
    ) -> dict:
        """Extract named parties from a trade document (invoice, bill of
        lading, packing list, …) and screen each one via POST
        /screen/document. The file travels as base64 inside JSON (not
        multipart) and is processed in memory server-side, then discarded —
        only party names/roles/countries and their verdicts are stored.

        Billing: max(1, parties found) screens per call — a document naming
        nobody still costs 1 screen. Requires a Growth plan or above
        (server returns 403 otherwise). Rate limit is 5 calls/minute, and
        unlike /screen/batch there is NO idempotency key — do not
        blind-retry the same bytes on success; retry only on transport
        errors, which _request already handles.

        Returns the DocumentScreenResponse dict: ref (batch ref, None when
        no parties found), document{filename,content_type,pages,
        document_type}, parties[{name,role,country,page,screening{ref,
        verdict,…}}], total_screened, total_flagged.
        """
        import base64

        if content_type not in DOCUMENT_ALLOWED_CONTENT_TYPES:
            raise UserError(
                _("Unsupported document type %(type)s — use PDF, JPEG, PNG or WebP.") % {"type": content_type}
            )
        try:
            approx_bytes = len(content_base64 or "") * 3 // 4
        except TypeError:
            approx_bytes = DOCUMENT_MAX_BYTES + 1
        if approx_bytes > DOCUMENT_MAX_BYTES:
            raise UserError(
                _("Document is too large (about %(size)s MB) — the Sanctix limit is 10 MB decoded.")
                % {"size": approx_bytes // (1024 * 1024)}
            )
        try:
            base64.b64decode(content_base64 or "", validate=True)
        except Exception as exc:
            raise UserError(_("Document data is not valid base64 — re-attach the file and retry.")) from exc
        payload: dict = {
            "filename": filename,
            "content_type": content_type,
            "content_base64": content_base64,
        }
        if threshold is not None:
            payload["threshold"] = threshold
        if limit is not None:
            payload["limit"] = limit
        if sources:
            payload["sources"] = sources
        try:
            return self._request("POST", "/screen/document", json=payload)
        except SanctixAPIError as exc:
            if exc.status_code == 403:
                # Plan gate: document screening is Growth+ only. The server
                # detail already says so — prepend the Odoo-side action.
                raise SanctixAPIError(
                    _("Document screening needs a Sanctix Growth plan or above. %s") % exc,
                    code="document_plan_required",
                    status_code=403,
                ) from exc
            raise

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, *, headers: dict | None = None, **kwargs) -> dict:
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._session.request(method, url, timeout=self._timeout, headers=headers, **kwargs)
            except requests.exceptions.Timeout as exc:
                last_exc = exc
                _logger.warning("Sanctix API timeout on %s %s (attempt %s/%s)", method, path, attempt, MAX_RETRIES)
                self._sleep_backoff(attempt)
                continue
            except requests.exceptions.ConnectionError as exc:
                last_exc = exc
                _logger.warning(
                    "Sanctix API connection error on %s %s (attempt %s/%s): %s", method, path, attempt, MAX_RETRIES, exc
                )
                self._sleep_backoff(attempt)
                continue

            if response.status_code == 429:
                # Rate limited — respect Retry-After if Sanctix sends one,
                # otherwise fall back to our own backoff schedule.
                retry_after = int(response.headers.get("Retry-After", RETRY_BACKOFF_SECONDS * attempt))
                _logger.info("Sanctix API rate limited, waiting %ss (attempt %s/%s)", retry_after, attempt, MAX_RETRIES)
                time.sleep(min(retry_after, 30))
                continue

            if response.status_code >= 500:
                # Transient upstream failure — worth a retry.
                last_exc = SanctixAPIError(
                    f"Sanctix API returned {response.status_code}", status_code=response.status_code
                )
                self._sleep_backoff(attempt)
                continue

            if response.status_code == 402:
                # Quota exceeded on a hard-capped plan — retrying will not
                # help, fail immediately with a clear, actionable message.
                detail = self._safe_error_detail(response)
                raise SanctixAPIError(
                    _(
                        "Sanctix screening quota exhausted for this billing period. "
                        "Upgrade your plan or wait for the next cycle. (%s)"
                    )
                    % detail,
                    code="quota_exceeded",
                    status_code=402,
                )

            if response.status_code == 403:
                detail = self._safe_error_detail(response)
                raise SanctixAPIError(
                    _("Sanctix API access denied: %s") % detail, code="forbidden", status_code=403
                )

            if response.status_code == 401:
                raise SanctixAPIError(
                    _("Sanctix API key is invalid or has been revoked. Check Settings > Sanctix Compliance."),
                    code="unauthorized",
                    status_code=401,
                )

            if response.status_code >= 400:
                detail = self._safe_error_detail(response)
                raise SanctixAPIError(
                    _("Sanctix API rejected the request: %s") % detail,
                    code="client_error",
                    status_code=response.status_code,
                )

            # 2xx
            try:
                return response.json()
            except ValueError as exc:
                raise SanctixAPIError(_("Sanctix API returned an unreadable response.")) from exc

        # Retries exhausted on timeout/connection/5xx
        raise SanctixAPIError(
            _("Could not reach the Sanctix API after %s attempts: %s") % (MAX_RETRIES, last_exc),
            code="unreachable",
        )

    @staticmethod
    def _safe_error_detail(response: requests.Response) -> str:
        try:
            body = response.json()
            return body.get("error", {}).get("detail") or body.get("detail") or response.text[:200]
        except ValueError:
            return response.text[:200]

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(RETRY_BACKOFF_SECONDS * attempt)
