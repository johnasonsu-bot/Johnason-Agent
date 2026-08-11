# Task 2 Report — Scriptable Fake Host and Supervised Lifecycle Handshake

## Status

Complete. Commit SHA: 2d3fc350311400ffbc67aa1e61111d7cdd58ba0b (amended below to include this final report value).

## Change summary

- Added the deterministic, bounded-line fake NDJSON host fixture with normal, incompatible-protocol, oversized-frame, EOF, stalled drain/shutdown, diagnostic, and correlation-mismatch modes.
- Added `EngineHostClient` with a process environment allowlist, one stdout reader task, bounded stderr drain, correlated requests, negotiated capability state, and bounded terminate/kill reaping.
- Registered closed empty schemas for `host.drain` and `host.shutdown`; the hello response protocol remains a bounded string so the client can reject an incompatible major version explicitly.
- Made start/close races supervised: concurrent starts share one handshake, failure/cancellation reaps the child, close is idempotent and cancellation-safe, and close-before-start is terminal.
- Added integration coverage for successful negotiation, incompatible major, EOF, oversized frame, drain timeout, shutdown timeout, response-name mismatch, diagnostics isolation, concurrency, and idempotent close.

## RED / GREEN evidence

- RED 1: `cd mvp && .venv/bin/python -m pytest tests/integration/test_engine_host_lifecycle.py -v` failed at collection with `ModuleNotFoundError: workbench.runtime.engine_host.client` before production client code existed.
- RED 2: lifecycle-race and response-correlation tests failed as expected before their fixes: 4 failures (unreaped incompatible host, two concurrent starts, wrong response name, cancelled close).
- RED 3: close-before-start and drain response protocol tests failed as expected before terminal-close and public failure cleanup: 2 failures.
- RED 4: drain-timeout reaping test failed as expected before timeout cleanup: `returncode is None`.
- GREEN: focused lifecycle suite passed 13/13; engine-host unit plus lifecycle suite passed 45/45.

## Commands and results

- `cd mvp && .venv/bin/python -m pytest tests/integration/test_engine_host_lifecycle.py -v` — 13 passed.
- `cd mvp && .venv/bin/python -m pytest tests/unit/runtime/engine_host tests/integration/test_engine_host_lifecycle.py -v` — 45 passed.
- `cd mvp && .venv/bin/python -m compileall -q src` — passed.
- `git diff --check` — passed.
- Secret scan with `rg` over the Task 2 client, fixture, and test files — only deliberate test sentinel references; no credentials or secret values in production code.

## Self-review

- Confirmed every new lifecycle command/response uses an explicit closed `(kind, name)` schema and empty immutable payload; no generic payload escape was added.
- Confirmed subprocess inheritance is constrained to existing `PATH`, `SYSTEMROOT`, `WINDIR`, `TMP`, `TEMP`, plus `PYTHONUTF8=1`; diagnostics retain only a safe indicator, never raw stderr.
- Independent reviewer initially found start/close/correlation races. All were fixed with regression tests; final review found no Critical or Important issues.

## Concerns

- None.
