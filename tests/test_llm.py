"""Tests del laboratorio LLM (Fase 02).

Se saltan automáticamente si las dependencias no están disponibles.
El mock no requiere deps, así que siempre se ejecuta.
"""

import unittest

from lab.llm import create_provider, validate_response, is_failed


class TestLLMMock(unittest.TestCase):
    def test_mock_vpn(self):
        p = create_provider("mock")
        r = p.complete("Cannot access VPN from home")
        self.assertEqual(r.category, "vpn")
        self.assertEqual(r.priority, "P2")
        self.assertEqual(r.suggested_action, "restart_service")
        self.assertGreaterEqual(r.confidence, 0.5)
        self.assertEqual(r.metadata.provider, "mock")

    def test_mock_password(self):
        p = create_provider("mock")
        r = p.complete("I forgot my password and I am locked out")
        self.assertEqual(r.category, "password_reset")
        self.assertEqual(r.suggested_action, "reset_password")

    def test_validate_clamps_confidence(self):
        p = create_provider("mock")
        r = p.complete("hello world")
        r.confidence = 1.5
        r.priority = "P9"
        vr = validate_response(r)
        self.assertEqual(vr.confidence, 1.0)
        self.assertEqual(vr.priority, "P3")

    def test_fallback_to_mock_when_unavailable(self):
        p = create_provider("openai")
        self.assertEqual(type(p).__name__, "MockLLMProvider")

    def test_json_roundtrip(self):
        p = create_provider("mock")
        r = p.complete("disk full on server")
        r2 = __import__("lab.llm.schema", fromlist=["LLMResponse"]).LLMResponse.from_json(r.to_json())
        self.assertEqual(r2.category, r.category)


if __name__ == "__main__":
    unittest.main()
