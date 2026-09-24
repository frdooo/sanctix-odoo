"""Unit tests for the Odoo-agnostic SanctixAPIClient — no database needed,
so this is a plain unittest.TestCase rather than an Odoo TransactionCase.

These pin down the exact wire contract against the real backend (headers,
paths, request/response shapes) so a regression like "sends Authorization:
Bearer instead of X-API-Key" or "reads response['risk_level'] which doesn't
exist" fails a test instead of shipping silently, as both did before this
suite existed.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from odoo.addons.sanctix_compliance.models.sanctix_api_client import (
    SanctixAPIClient,
    SanctixAPIError,
)
from odoo.exceptions import UserError


def _fake_response(status_code: int, json_body: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    resp.text = text or json.dumps(json_body or {})
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


class TestSanctixAPIClient(unittest.TestCase):
    def setUp(self):
        self.client = SanctixAPIClient(api_key="stx_test_key", base_url="https://api.sanctix.test/api/v1")

    def test_uses_x_api_key_header_not_authorization_bearer(self):
        """The backend resolves `Authorization: Bearer` as a Supabase JWT
        and `X-API-Key` as an API key (app/api/deps.py) — sending the API
        key as a bearer token is rejected server-side. This is the bug
        that made every call from this module fail against the real API."""
        self.assertEqual(self.client._session.headers.get("X-API-Key"), "stx_test_key")
        self.assertNotIn("Authorization", self.client._session.headers)

    @patch("requests.Session.request")
    def test_screen_entity_posts_correct_path_and_field_names(self, mock_request):
        mock_request.return_value = _fake_response(
            200,
            {
                "verdict": "CLEAR",
                "confidence": "CLEAR",
                "final_score": 0,
                "all_hits": [],
                "ref": "abc123",
            },
        )
        self.client.screen_entity(name="Mahan Air", country="IR")

        mock_request.assert_called_once()
        _method, url = mock_request.call_args.args
        self.assertEqual(url, "https://api.sanctix.test/api/v1/screen/entity")
        payload = mock_request.call_args.kwargs["json"]
        # `country`, not `country_code` (EntityScreenRequest has no
        # `country_code` field); no `entity_type` (not a field on the
        # schema at all, so sending it was previously a silent no-op).
        self.assertEqual(payload, {"name": "Mahan Air", "country": "IR"})

    @patch("requests.Session.request")
    def test_screen_batch_sends_idempotency_key_header(self, mock_request):
        mock_request.return_value = _fake_response(
            200, {"total_screened": 1, "total_flagged": 0, "elapsed_ms": 5, "ref": "batch1", "results": []}
        )
        self.client.screen_batch([{"name": "Test Co"}], idempotency_key="wizard-key-123")

        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers, {"Idempotency-Key": "wizard-key-123"})
        payload = mock_request.call_args.kwargs["json"]
        self.assertEqual(payload["queries"], [{"name": "Test Co"}])

    @patch("requests.Session.request")
    def test_401_raises_unauthorized_error(self, mock_request):
        mock_request.return_value = _fake_response(401, {"error": {"code": "invalid_api_key", "detail": "bad key"}})
        with self.assertRaises(SanctixAPIError) as ctx:
            self.client.screen_entity(name="Someone")
        self.assertEqual(ctx.exception.code, "unauthorized")

    @patch("requests.Session.request")
    def test_402_quota_exceeded_raises_without_retry(self, mock_request):
        mock_request.return_value = _fake_response(
            402, {"error": {"code": "quota_exceeded", "detail": "no screens left"}}
        )
        with self.assertRaises(SanctixAPIError) as ctx:
            self.client.screen_entity(name="Someone")
        self.assertEqual(ctx.exception.code, "quota_exceeded")
        # 402 is a hard stop — retrying won't help until the billing period
        # resets, so it must NOT go through the retry loop.
        self.assertEqual(mock_request.call_count, 1)

    @patch("time.sleep", return_value=None)
    @patch("requests.Session.request")
    def test_5xx_retries_then_raises(self, mock_request, _mock_sleep):
        mock_request.return_value = _fake_response(503)
        with self.assertRaises(SanctixAPIError):
            self.client.screen_entity(name="Someone")
        from odoo.addons.sanctix_compliance.models.sanctix_api_client import MAX_RETRIES

        self.assertEqual(mock_request.call_count, MAX_RETRIES)

    @patch("requests.Session.request")
    def test_screen_document_rejects_bad_type_without_http(self, mock_request):
        with self.assertRaises(UserError):
            self.client.screen_document(
                filename="invoice.txt", content_type="text/plain", content_base64="aGk="
            )
        mock_request.assert_not_called()

    @patch("requests.Session.request")
    def test_screen_document_posts_correct_shape(self, mock_request):
        import base64

        mock_request.return_value = _fake_response(200, {"ref": None, "parties": []})
        self.client.screen_document(
            filename="invoice.pdf",
            content_type="application/pdf",
            content_base64=base64.b64encode(b"%PDF-1.4").decode(),
        )
        _method, url = mock_request.call_args.args
        self.assertEqual(url, "https://api.sanctix.test/api/v1/screen/document")
        payload = mock_request.call_args.kwargs["json"]
        self.assertEqual(payload["filename"], "invoice.pdf")
        self.assertEqual(payload["content_type"], "application/pdf")


if __name__ == "__main__":
    unittest.main()
