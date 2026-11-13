"""Evaluation package of the AI Help Desk triage agent.

Two entry points:

* :mod:`eval.metrics` -- pure metric helpers (no I/O);
* :mod:`eval.run_eval` -- the harness that runs the pipeline over
  ``eval/labeled_tickets.json`` and writes ``outputs/eval_report.md`` and
  ``outputs/eval_report.json``.

The harness never touches the network and never executes an action: it only
exercises triage, identity and policy (SPEC section 3.8).
"""

__all__ = ["metrics", "run_eval"]
