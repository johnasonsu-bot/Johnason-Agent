# Hermes Workbench MVP

This directory contains the isolated Phase 0 validation probes described in
`docs/superpowers/plans/2026-08-05-phase-0-technical-validation.md`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests/unit -v
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
