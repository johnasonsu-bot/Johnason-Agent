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
