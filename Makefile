# AI Help Desk Triage & Remediation Agent -- developer entry points.
#
# Every target runs with the Python standard library only: no `pip install`
# is required to clone, run, test or evaluate this repository.

PYTHON ?= python3

.DEFAULT_GOAL := help
.PHONY: help test eval demo dry-run run compile lint clean all

help: ## Show this help
	@echo "AI Help Desk Triage & Remediation Agent"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'
	@echo ""
	@echo "Nothing to install: the core runtime, the tests and the evaluation"
	@echo "harness only use the Python standard library (Python 3.9+)."

compile: ## Byte-compile every module (syntax check, zero installs)
	$(PYTHON) -m compileall -q agent.py adapters services eval tests

lint: compile ## Syntax check (no external linter is required)

test: ## Run the full unit test suite
	$(PYTHON) -m unittest discover -s tests -t . -v

eval: ## Run the evaluation harness and write outputs/eval_report.{md,json}
	$(PYTHON) eval/run_eval.py

demo: ## Run the narrated dry-run demo (5 minutes, no side effect)
	$(PYTHON) agent.py --demo

dry-run: ## Run the pipeline in dry-run mode with the per-ticket narration
	$(PYTHON) agent.py --input data/tickets.csv --dry-run --verbose

run: ## Run the pipeline with write-back to the mocked ITSM
	$(PYTHON) agent.py --input data/tickets.csv --verbose

all: compile test eval ## Compile, test and evaluate

clean: ## Remove the generated artefacts
	rm -f outputs/triaged_tickets.json outputs/triaged_tickets.csv \
	      outputs/actions.json outputs/approvals.json outputs/audit.log \
	      outputs/metrics.json outputs/metrics.md \
	      outputs/eval_report.md outputs/eval_report.json
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
