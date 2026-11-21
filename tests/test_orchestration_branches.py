"""Tests de ramas, limites y persistencia de orquestacion (Fase 04 parte B)."""

import json
import tempfile
import unittest
from pathlib import Path

from lab.orchestration.branches import (
    APPROVAL_THRESHOLD_CONFIDENCE,
    route_after_policy,
    route_after_retrieve,
    route_after_redact,
)
from lab.orchestration.limits import (
    MAX_ITERATIONS,
    MAX_TOOL_CALLS,
    check_iteration_limit,
    check_tool_call_limit,
    increment_iteration,
    increment_tool_call,
)
from lab.orchestration.persistence import (
    save_state,
    load_state,
    InMemoryCheckpointer,
    get_checkpointer,
    LANGGRAPH_AVAILABLE,
)


class TestBranches(unittest.TestCase):
    def test_route_after_policy_approve(self):
        self.assertEqual(route_after_policy({"policy_decision": "require_approval"}), "approve")

    def test_route_after_policy_escalate(self):
        self.assertEqual(route_after_policy({"policy_decision": "escalate"}), "escalate")

    def test_route_after_policy_redact(self):
        self.assertEqual(route_after_policy({"policy_decision": "auto_execute"}), "redact")

    def test_route_after_retrieve_abstain(self):
        self.assertEqual(route_after_retrieve({"rag_abstained": True}), "abstain")

    def test_route_after_retrieve_policy(self):
        self.assertEqual(route_after_retrieve({"rag_abstained": False}), "policy")

    def test_route_after_redact_end(self):
        self.assertEqual(route_after_redact({"confidence": 0.9}), "end")

    def test_route_after_redact_review_low(self):
        self.assertEqual(route_after_redact({"confidence": 0.1}), "review")

    def test_threshold_constant(self):
        self.assertEqual(APPROVAL_THRESHOLD_CONFIDENCE, 0.5)


class TestLimits(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(MAX_ITERATIONS, 5)
        self.assertEqual(MAX_TOOL_CALLS, 10)

    def test_check_iteration_under(self):
        self.assertFalse(check_iteration_limit({"iterations": 0}))

    def test_check_iteration_at_limit(self):
        self.assertTrue(check_iteration_limit({"iterations": 5}))

    def test_check_tool_under(self):
        self.assertFalse(check_tool_call_limit({"tool_calls": 3}))

    def test_check_tool_at_limit(self):
        self.assertTrue(check_tool_call_limit({"tool_calls": 10}))

    def test_increment_iteration(self):
        st = {"iterations": 0}
        out = increment_iteration(st)
        self.assertEqual(out["iterations"], 1)
        self.assertEqual(st["iterations"], 1)
        out2 = increment_iteration(st)
        self.assertEqual(out2["iterations"], 2)

    def test_increment_tool_call(self):
        st = {"tool_calls": 0}
        out = increment_tool_call(st, 2)
        self.assertEqual(out["tool_calls"], 2)
        self.assertEqual(st["tool_calls"], 2)
        out2 = increment_tool_call(st)
        self.assertEqual(out2["tool_calls"], 3)

    def test_increment_iteration_count_key(self):
        st = {"iteration_count": 1}
        out = increment_iteration(st)
        self.assertEqual(out["iteration_count"], 2)

    def test_increment_tool_call_count_key(self):
        st = {"tool_call_count": 9}
        out = increment_tool_call(st)
        self.assertEqual(out["tool_call_count"], 10)
        self.assertTrue(check_tool_call_limit(st))


class TestPersistence(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            state = {"a": 1, "b": [2, 3]}
            save_state(state, path)
            loaded = load_state(path)
            self.assertEqual(loaded, state)

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nope.json"
            self.assertIsNone(load_state(path))

    def test_default_path(self):
        # save without path -> outputs/orchestration_state.json
        state = {"hello": "world"}
        save_state(state)
        default = Path("outputs/orchestration_state.json")
        self.assertTrue(default.exists())
        loaded = load_state()
        self.assertEqual(loaded, state)
        # cleanup
        try:
            default.unlink()
        except Exception:
            pass

    def test_inmemory_checkpointer(self):
        cp = InMemoryCheckpointer()
        cp.put("t1", {"x": 5})
        self.assertEqual(cp.get("t1"), {"x": 5})
        # mutation isolation
        got = cp.get("t1")
        got["x"] = 999
        self.assertEqual(cp.get("t1")["x"], 5)
        self.assertIsNone(cp.get("missing"))

    def test_get_checkpointer(self):
        cp = get_checkpointer()
        self.assertTrue(hasattr(cp, "put") and hasattr(cp, "get"))
        cp.put("a", {"y": 1})
        self.assertEqual(cp.get("a"), {"y": 1})


if __name__ == "__main__":
    unittest.main()
