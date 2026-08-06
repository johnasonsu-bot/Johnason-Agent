# Hermes Workbench MVP

This directory contains the recoverable local Workbench MVP described in the
Phase 0 and Phase 1 plans under `docs/superpowers/plans/`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests/unit -v
```

## Run the Workbench

The local API is an Electron-owned child process rather than a standalone
fixed-port service. On every start Electron generates a new capability, asks the
backend to bind an operating-system-selected loopback port, verifies the
service-instance identity, and exposes only the narrow preload IPC bridge to the
renderer. Closing the window, quitting the application, or losing the renderer
locks the credential vault and terminates the child process.

Provider credentials are entered only in Provider Center and encrypted in the
application-owned vault. SQLite stores an opaque credential reference, never the
credential value. The vault starts locked after every application restart and
does not use environment variables or an operating-system keychain for Provider
Center credentials.

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
