# Hermes Workbench MVP

This directory contains the recoverable local Workbench MVP described in the
Phase 0 and Phase 1 plans under `docs/superpowers/plans/`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests/unit -v
```

## Run the local API

```bash
.venv/bin/python -m workbench.main
```

Open `http://127.0.0.1:8765/api/health`. Runtime state is stored under
`.runtime/`; credentials are referenced by environment-variable name and are
never persisted in SQLite or Artifact metadata.

## Run Phase 1 acceptance

Set the Data Platform variables in your shell, including Project 7, Job 73,
Run 86 and CDP 9222, then run:

```bash
.venv/bin/python scripts/run_phase1_acceptance.py
```

Runtime databases, external Hermes checkouts, logs, and probe results remain
local and are excluded from Git.

## Open the Canvas probe

Do not open `canvas-spike/index.html` with a browser. It is Electron source and
requires its preload sandbox. Launch the runnable application instead:

```bash
cd canvas-spike
npm start
```
