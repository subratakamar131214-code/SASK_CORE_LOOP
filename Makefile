# SASK_CORE_LOOP local developer commands.
# Optional if `make` is unavailable: run the underlying commands directly.

PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; \
	elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe; \
	else echo python; fi)

.PHONY: help install lint typecheck test secret-scan verify dry-run cycle status \
	heartbeat heartbeat-dry-run evolve evolve-verify replicate clean

help:
	@echo "install        install the package with dev dependencies"
	@echo "lint           ruff check src tests"
	@echo "typecheck      mypy src"
	@echo "test           pytest -q"
	@echo "secret-scan    scan the repository for secrets"
	@echo "verify         invariants + lint + types + tests + secret scan + status"
	@echo "dry-run        full cycle against a temporary runtime (zero writes)"
	@echo "cycle          real offline cycle writing state/"
	@echo "status         print current state, ledger, and heartbeat status"
	@echo "heartbeat      record a heartbeat and commit-ready state"
	@echo "evolve         generate human-gated upgrade proposals"
	@echo "replicate      APPROVAL_FILE=... [MODE=workspace] [NICHE=...] [APPLY=1]"
	@echo "clean          remove build, dist, and local caches"

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check src tests

typecheck:
	$(PYTHON) -m mypy src

test:
	$(PYTHON) -m pytest -q

secret-scan:
	$(PYTHON) -m src.compliance.secretscan --tree .

verify:
	bash scripts/verify.sh

dry-run:
	$(PYTHON) -m src.orchestrator --cycle --dry-run

cycle:
	$(PYTHON) -m src.orchestrator --cycle

status:
	$(PYTHON) -m src.orchestrator --status

heartbeat:
	$(PYTHON) -m src.orchestrator --heartbeat --workflow heartbeat-immortality.yml

heartbeat-dry-run:
	$(PYTHON) -m src.orchestrator --heartbeat --dry-run

evolve:
	$(PYTHON) -m src.orchestrator --evolve

evolve-verify:
	$(PYTHON) -m src.orchestrator --evolve --verify-commands

replicate:
	@test -n "$(APPROVAL_FILE)" || (echo "APPROVAL_FILE=approvals/<file>.yaml is required" >&2; exit 1)
	$(PYTHON) -m src.orchestrator --replicate --approval-file "$(APPROVAL_FILE)" \
		$(if $(MODE),--mode $(MODE),) $(if $(NICHE),--niche $(NICHE),) $(if $(APPLY),--apply,)

clean:
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache
