"""Tests del laboratorio API (Fase 01).

Se saltan automáticamente si FastAPI/Pydantic no están instalados, de modo
que el suite base (stdlib only) sigue pasando.
"""

import unittest

try:
    from fastapi.testclient import TestClient  # noqa: F401
    import api.main  # noqa: F401
    HAS_FASTAPI = True
except Exception:  # pragma: no cover - entorno sin deps opcionales
    HAS_FASTAPI = False


@unittest.skipUnless(HAS_FASTAPI, "FastAPI no instalado (requirements-lab.txt)")
class TestLabApi(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import api.main

        self.client = TestClient(api.main.create_app())

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        self.assertEqual(r.json()["mode"], "mock")

    def test_triage_vpn(self):
        r = self.client.post(
            "/triage",
            json={"ticket_id": "T1", "requester": "alice@example.com",
                  "description": "Cannot access VPN from home", "service": "VPN"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["category"], "vpn")
        self.assertIn("request_id", body)

    def test_triage_empty_input(self):
        r = self.client.post(
            "/triage",
            json={"ticket_id": "T2", "requester": "bob@example.com"},
        )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
