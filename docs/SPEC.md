# Project scope — AI Help Desk Triage & Remediation Agent

This document describes the repository as it exists today. It replaces the earlier
multi-agent implementation contract and its stale acceptance checklist.

## Purpose

Demonstrate a safe, explainable Help Desk automation workflow that can classify and
prioritise tickets, check requester identity, apply risk rules, request human approval or
escalate, and record its decisions for review.

## Implemented in this repository

- A local CLI pipeline that reads synthetic tickets from `data/tickets.csv`.
- Deterministic, knowledge-base-based triage and priority selection.
- Mock identity, ITSM, and application adapters for offline demonstrations.
- Adapter implementations for ServiceNow, Jira Service Management, Microsoft Entra ID,
  and Okta. They have not been exercised against live tenants.
- Risk policies, identity checks, human approval records, escalation, audit logs, and
  operational metrics.
- An evaluation harness with 28 synthetic labelled tickets and explicit thresholds.
- Unit tests using Python's `unittest` library.

## Important limits

- The demo does not call an LLM. Classification and risk decisions are deterministic.
- The evaluation set was authored alongside the knowledge base. Its reported scores
  measure consistency within this demo, not accuracy on a real organisation's tickets.
- The real-system adapters require credentials and have not been tested against live
  ServiceNow, Jira, Entra ID, or Okta tenants.
- The demo uses local files and mock systems. It has no web UI, production database,
  external approval notifications, or production deployment configuration.
- No GitHub Actions or other CI workflow is included yet.

## Run locally

From the repository root:

```bash
python3 agent.py --demo
python3 -m unittest discover -s tests -t . -v
python3 eval/run_eval.py
```

The evaluation script checks category, priority, action, and decision thresholds and
returns a non-zero exit code when a threshold fails. These commands describe the
available checks; run them on the current checkout before relying on their results.

## Next steps before presenting this as a public portfolio

1. Run the unit suite, demo, and evaluation on the release candidate.
2. Add a CI workflow that runs the unit suite and evaluation script.
3. If live integrations are needed, test them only in dedicated sandbox tenants with
   least-privilege credentials and writes disabled by default.
4. Add externally sourced, appropriately anonymised labelled tickets before making any
   claim about performance on real support data.
