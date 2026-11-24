# Automated KPI Reporting Pipeline - local developer commands
PYTHON ?= python3.11
VENV   ?= .venv
BIN    := $(VENV)/bin

.PHONY: help setup run run-no-email backfill replay test clean

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

setup:           ## create the virtualenv and install pinned dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt

run:             ## run the weekly pipeline (emails only if SMTP_* / REPORT_TO are set)
	$(BIN)/python -m pipeline.run

run-no-email:    ## run the pipeline without the email step
	$(BIN)/python -m pipeline.run --no-email

backfill:        ## seed history: make backfill WEEKS=8
	$(BIN)/python -m pipeline.run --backfill-weeks $(or $(WEEKS),8) --no-email

replay:          ## re-process saved raw pages for a date: make replay DATE=2026-11-23
	$(BIN)/python -m pipeline.run --from-raw --run-date $(DATE) --no-email

test:            ## run the pytest suite
	$(BIN)/python -m pytest

clean:           ## remove local-only artefacts (keeps the committed snapshot and reports)
	rm -rf data/raw data/*.duckdb logs .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
