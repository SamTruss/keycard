.PHONY: install dev test lint fix check run rootfs clean

PY ?= python3

# Room to build a Firecracker rootfs for — see rootfs/README.md.
ROOM ?= ubuntu

install:
	pip install -e . --break-system-packages

dev:
	pip install -e ".[dev]" --break-system-packages

test:
	pytest -q

lint:
	ruff check .
	ruff format --check .
	mypy src/

fix:
	ruff check --fix .
	ruff format .

# Everything CI runs, in one command.
check: fix lint test

run:
	keycard up -v

# v2 (FIRECRACKER.md, Phase 1). Not part of `check` — it needs root and a
# Docker daemon, and builds nothing the shipped server uses yet.
rootfs:
	sudo rootfs/build.sh --room $(ROOM)

clean:
	docker ps -aq --filter ancestor=ubuntu:24.04 | xargs -r docker rm -f
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
