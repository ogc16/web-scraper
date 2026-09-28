.PHONY: help install dev test lint format typecheck check ci build verify-dist clean all

PYTHON ?= python

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package
	$(PYTHON) -m pip install .

dev:  ## Install the package in editable mode with dev extras
	$(PYTHON) -m pip install -e ".[dev]"

test:  ## Run the test suite
	$(PYTHON) -m pytest

lint:  ## Check lint and formatting
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:  ## Auto-fix and reformat
	$(PYTHON) -m ruff check . --fix
	$(PYTHON) -m ruff format .

typecheck:  ## Run mypy in strict mode
	$(PYTHON) -m mypy

check: lint typecheck test  ## Lint, typecheck and test

ci: check  ## Exactly what the CI workflow runs

build:  ## Build the sdist and wheel
	$(PYTHON) -m build

verify-dist: build  ## Install the built wheel into a throwaway venv and run it
	rm -rf .verify-venv
	$(PYTHON) -m venv .verify-venv
	.verify-venv/bin/python -m pip install --quiet dist/*.whl
	.verify-venv/bin/python -m awsa --version
	.verify-venv/bin/python -m awsa providers
	rm -rf .verify-venv

clean:  ## Remove build and cache artifacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .verify-venv
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

all: ci build  ## Every gate, then a distribution build
