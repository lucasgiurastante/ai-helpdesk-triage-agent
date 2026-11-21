"""Tests de orquestacion (Fase 04)."""

import os
import shutil
import tempfile
import unittest

from lab.orchestration import run_pipeline, build_graph, MAX_ITERATIONS


class TestOrchestrationPipeline(unittest.TestCase):
    def test_run_pipeline_vpn(self):
        state = run_pipeline(
            {
                "ticket_id": "TCK-VPN",
                "requester": "juan.perez",
                "description": "No puedo conectar a la VPN desde casa, error de tunnel",
                "service": "VPN",
            }
        )
        self.assertIsNotNone(state.get("final"))
        self.assertEqual(state.get("category"), "vpn")
        self.assertIsNotNone(state.get("priority"))
        self.assertLessEqual(state.get("iterations", 0), MAX_ITERATIONS)

    def test_abstention(self):
        state = run_pipeline(
            {
                "ticket_id": "TCK-XYZ",
                "requester": "juan.perez",
                "description": "xyzzy frobnicator quux",
                "service": "Unknown",
            }
        )
        self.assertTrue(state.get("rag_abstained"))
        self.assertIsNotNone(state.get("final"))

    def test_policy_decision(self):
        state = run_pipeline(
            {
                "ticket_id": "TCK-POL",
                "requester": "juan.perez",
                "description": "Necesito acceso a la carpeta de finanzas",
                "service": "SAP Fiori",
            }
        )
        self.assertIn(
            state.get("policy_decision"),
            ("auto_execute", "require_approval", "escalate"),
        )

    def test_build_graph_invoke(self):
        g = build_graph()
        state = g.invoke(
            {
                "ticket_id": "TCK-G",
                "requester": "juan.perez",
                "description": "password reset",
                "service": "SAP Fiori",
            }
        )
        self.assertIsNotNone(state.get("final"))


if __name__ == "__main__":
    unittest.main()
