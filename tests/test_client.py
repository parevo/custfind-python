import hashlib
import hmac
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from custfind import Custfind, CustfindError, WebhookVerificationError, verify_webhook


class _Server:
    def __init__(self, respond):
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                outer.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
                status, payload, extra = respond(len(outer.seen))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class ClientTest(unittest.TestCase):
    def test_request_shape(self):
        s = _Server(lambda n: (202, {"job_id": "j1", "status": "queued"}, None))
        cf = Custfind(api_key="cf_live_test", base_url=s.url)
        res = cf.searches.create(sector="Dental clinic", location="Istanbul", limit=25)
        self.assertEqual(res["job_id"], "j1")
        first = s.seen[0]
        self.assertEqual((first["method"], first["path"]), ("POST", "/v1/searches"))
        self.assertEqual(first["headers"]["Authorization"], "Bearer cf_live_test")
        self.assertTrue(first["headers"].get("Idempotency-Key"))
        self.assertEqual(json.loads(first["body"]), {"sector": "Dental clinic", "location": "Istanbul", "limit": 25})
        cf.leads.list(min_score=70, contact="email")
        self.assertEqual(s.seen[1]["path"], "/v1/leads?min_score=70&contact=email")
        self.assertNotIn("Idempotency-Key", s.seen[1]["headers"])
        s.close()

    def test_retries_keep_the_idempotency_key(self):
        plan = {1: (503, {"error": "x"}, None), 2: (429, {"error": "rate_limited"}, {"Retry-After": "0"})}
        s = _Server(lambda n: plan.get(n, (202, {"job_id": "j2"}, None)))
        cf = Custfind(api_key="k", base_url=s.url, max_retries=3)
        self.assertEqual(cf.searches.create(sector="x")["job_id"], "j2")
        self.assertEqual(len(s.seen), 3)
        self.assertEqual(len({r["headers"]["Idempotency-Key"] for r in s.seen}), 1)
        s.close()

    def test_error_code(self):
        s = _Server(lambda n: (402, {"error": "customer_limit_reached"}, None))
        cf = Custfind(api_key="k", base_url=s.url)
        with self.assertRaises(CustfindError) as ctx:
            cf.searches.create(sector="x", external_customer_id="acme")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (402, "customer_limit_reached"))
        self.assertEqual(len(s.seen), 1)
        s.close()

    def test_path_encoding_and_204(self):
        s = _Server(lambda n: (204, None, None))
        cf = Custfind(api_key="k", base_url=s.url)
        self.assertIsNone(cf.customers.remove_limit("acme/eu 1"))
        self.assertEqual(s.seen[0]["path"], "/v1/customers/acme%2Feu%201/limit")
        s.close()

    def test_wait_for_completion(self):
        statuses = ["queued", "processing", "completed"]
        s = _Server(lambda n: (200, {"id": "j", "status": statuses[min(n - 1, 2)]}, None))
        cf = Custfind(api_key="k", base_url=s.url)
        self.assertEqual(cf.searches.wait_for_completion("j", interval=0.01)["status"], "completed")
        self.assertEqual(len(s.seen), 3)
        s.close()


class WebhookTest(unittest.TestCase):
    def test_verify(self):
        secret = "whsec_test"
        body = json.dumps({"id": "evt", "type": "search.completed", "data": {"id": "j"}}).encode()
        t = int(time.time())
        sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertEqual(verify_webhook(body, f"t={t},v1={sig}", secret)["type"], "search.completed")
        for bad_body, header, key in [
            (body + b" ", f"t={t},v1={sig}", secret),
            (body, f"t={t},v1={sig}", "whsec_other"),
            (body, f"t={t - 3600},v1={sig}", secret),
            (body, "garbage", secret),
        ]:
            with self.assertRaises(WebhookVerificationError):
                verify_webhook(bad_body, header, key)


if __name__ == "__main__":
    unittest.main()
