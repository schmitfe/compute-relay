"""Check the smoke harness's HTTP privacy/diagnostics, not broker security."""

from __future__ import annotations

import importlib.util
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("container_smoke", ROOT / "tools/container_smoke.py")
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/login/":
            self.send_response(200)
            self.send_header("Set-Cookie", "csrftoken=synthetic-csrf; Path=/")
            self.end_headers()
            self.wfile.write(b"<form>synthetic login fixture</form>")
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/steal-token")
            self.end_headers()
            return
        if self.path == "/error":
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"detail":"synthetic-secret-must-not-appear-in-errors"}')
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_POST(self):
        content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/login/":
            fields = urllib.parse.parse_qs(content.decode())
            expected = {"username": ["fixture-owner"], "password": ["synthetic-password"],
                        "csrfmiddlewaretoken": ["synthetic-csrf"]}
            valid = (self.headers.get("Content-Type") == "application/x-www-form-urlencoded"
                     and fields == expected)
            self.send_response(302 if valid else 400)
            if valid:
                self.send_header("Set-Cookie", "sessionid=synthetic-session; Path=/; HttpOnly")
                self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/form":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(urllib.parse.parse_qs(content.decode())).encode())
            return
        self.send_response(201)
        self.end_headers()
        self.wfile.write(json.dumps({
            "body": json.loads(content),
            "idempotency_key": self.headers.get("Idempotency-Key"),
            "authorization": self.headers.get("Authorization"),
        }).encode())


class SmokeClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def test_external_destination_and_url_credentials_are_rejected(self):
        for url in ("https://example.invalid", "http://token@localhost:8765", "http://localhost:8765/api"):
            with self.subTest(url=url), self.assertRaises(SMOKE.SmokeFailure):
                SMOKE.Client(url, token="synthetic-test-token")

    def test_redirect_cannot_forward_credential_off_origin(self):
        with self.assertRaisesRegex(SMOKE.SmokeFailure, "outside"):
            SMOKE.Client(self.origin, token="synthetic-test-token").request("GET", "/redirect")

    def test_error_details_are_not_in_diagnostic_message(self):
        with self.assertRaises(SMOKE.SmokeFailure) as captured:
            SMOKE.Client(self.origin).request("GET", "/error")
        self.assertIn("HTTP 403", str(captured.exception))
        self.assertNotIn("synthetic-secret", str(captured.exception))

    def test_mutations_send_idempotency_key_and_token_in_header_only(self):
        response = SMOKE.Client(self.origin, token="synthetic-test-token").request("POST", "/echo", {"ok": True})
        self.assertEqual(response["body"], {"ok": True})
        self.assertEqual(response["authorization"], "Bearer synthetic-test-token")
        self.assertEqual(len(response["idempotency_key"]), 36)

    def test_login_uses_form_encoding_and_csrf_cookie(self):
        client = SMOKE.Client(self.origin)
        client.login("fixture-owner", "synthetic-password")
        self.assertEqual(client.cookie("sessionid"), "synthetic-session")

    def test_management_forms_preserve_multiple_scopes_and_connections(self):
        response = SMOKE.Client(self.origin).request("POST", "/form", {
            "scopes": ["jobs:read", "jobs:write"], "connections": ["first", "second"],
        }, form=True)
        self.assertEqual(response["scopes"], ["jobs:read", "jobs:write"])
        self.assertEqual(response["connections"], ["first", "second"])

    def test_key_reveal_parser_reads_public_id_without_printing_secret(self):
        client = SMOKE.Client(self.origin)
        client.last_headers = {"cache-control": "no-store, private"}
        identifier = "391a5ac7-85eb-459f-b205-76fa7798655d"
        secret = "cbp_synthetic-key-memory-only"
        html = f'<pre id="api-secret">{secret}</pre><p>Identifier: {identifier}</p>'.encode()
        self.assertEqual(SMOKE.revealed_key(client, html), (secret, identifier))

    def test_cluster_setup_parser_binds_current_profile_fingerprint_and_revision(self):
        choice = "fixture-profile:" + "a" * 64
        page = f'<input name="setup_revision" value="3"><select name="credential_profile"><option value="{choice}">fixture-profile</option></select>'.encode()
        self.assertEqual(SMOKE.cluster_binding_fields(page, "fixture-profile"),
                         {"credential_profile": choice, "setup_revision": "3"})
        with self.assertRaises(SMOKE.SmokeFailure):
            SMOKE.cluster_binding_fields(page, "another-profile")
        with self.assertRaises(SMOKE.SmokeFailure):
            SMOKE.cluster_binding_fields(page.replace(b'value="3"', b'value="0"'), "fixture-profile")


if __name__ == "__main__":
    unittest.main()
