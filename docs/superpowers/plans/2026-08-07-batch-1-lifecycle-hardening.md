# Batch 1 Lifecycle Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three load-bearing lifecycle gaps found in the Batch 1 final review so the Electron-owned Workbench can start, stop, and recover deterministically on Windows, macOS, and Linux before Batch 2 begins.

**Architecture:** Keep the existing random-port/capability-token boundary and Provider Center APIs unchanged. Harden the Python listener at bind time, make Electron backend startup a single-flight operation with one cleanup path and a parent-liveness pipe, and make Vault recovery publish a complete new document before retiring the corrupt primary while leaving a durable recovery marker for interrupted operations.

**Tech Stack:** Python 3.11+, FastAPI/uvicorn, pytest, Electron, TypeScript, Playwright, POSIX/Windows socket and file primitives.

## Global Constraints

- Do not write API keys, tokens, passwords, or capability values to source, tests, logs, or committed fixtures.
- Preserve `127.0.0.1` binding, random port `0`, and the existing `X-Workbench-Capability` authentication contract.
- Preserve the existing UI navigation and Provider Center behavior; lifecycle changes must be transparent to renderer callers.
- A vault that is missing, corrupt, or interrupted during recovery must never be reported as `uninitialized`; it must expose `recovery_required` until an explicit recovery succeeds.
- Every production change must be preceded by a failing automated test and verified with the narrow test plus the relevant full suite.
- Do not remove any existing backup or recovery artifact automatically; explicit recovery remains user-controlled.

---

### Task 1: Make Electron-owned socket binding exclusive on Windows

**Files:**
- Modify: `mvp/src/workbench/main.py:82-91`
- Test: `mvp/tests/unit/test_main.py` (create if absent)

**Interfaces:**
- Consumes: `_serve_electron_backend(settings, capability, instance_id)` and its listener factory.
- Produces: a listener that uses `SO_EXCLUSIVEADDRUSE` on Windows before bind, and uses `SO_REUSEADDR` only on non-Windows platforms; port `0` remains supported.

- [ ] **Step 1: Write the failing platform-binding tests.**

  Add tests that monkeypatch the socket factory with a recording socket and assert:
  - when `os.name == "nt"`, `setsockopt(SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1)` occurs before `bind`, and no `SO_REUSEADDR` option is set;
  - on POSIX, `SO_REUSEADDR` remains enabled and `SO_EXCLUSIVEADDRUSE` is not requested;
  - the selected ephemeral port is read from `getsockname()` and passed to uvicorn without opening a second socket.

- [ ] **Step 2: Run the focused tests and verify they fail for the current implementation.**

  Run: `cd mvp && pytest -q tests/unit/test_main.py`

  Expected: the Windows assertion fails because the current code unconditionally sets `SO_REUSEADDR`.

- [ ] **Step 3: Implement the minimal platform-specific socket setup.**

  Add a small `_configure_listener(listener)` helper in `mvp/src/workbench/main.py` that checks `os.name` and applies the platform-specific option before `bind`; retain the existing loopback guard and random-port handshake.

- [ ] **Step 4: Run the focused and Python suites.**

  Run: `cd mvp && pytest -q tests/unit/test_main.py tests/unit/credentials/test_vault.py`

  Expected: all selected tests pass with no new warnings.

- [ ] **Step 5: Commit.**

  `git add mvp/src/workbench/main.py mvp/tests/unit/test_main.py && git commit -m "fix: make workbench listener exclusive on Windows"`

---

### Task 2: Make Electron backend startup and shutdown single-flight

**Files:**
- Modify: `mvp/canvas-spike/src/main.ts`
- Test: `mvp/canvas-spike/tests/lifecycle.spec.ts` (create if absent)

**Interfaces:**
- Consumes: `startBackend()`, `stopBackend()`, `readHandshake()`, `createWindow()`, and the existing `before-quit`/`activate` handlers.
- Produces: one shared `startingBackend: Promise<void> | null`, a tracked child process whose stdin remains open as a parent-liveness pipe, and a single cleanup path used by ready, activate, renderer-crash, handshake failure, window creation failure, and unexpected backend exit.

- [ ] **Step 1: Write failing lifecycle regression tests.**

  Extend the Playwright Electron suite with deterministic scenarios that:
  - launch the app and trigger `activate` while the initial backend start is pending, then assert only one backend process/handshake serves the window;
  - force a handshake timeout or invalid handshake and assert the child is terminated before Electron exits;
  - make `createWindow()` fail after backend startup and assert the backend is stopped;
  - terminate the backend unexpectedly and assert Electron exits only after cleanup, with no orphan child;
  - launch the app with a short-lived parent-control fixture and assert the backend exits when its stdin reaches EOF, while normal app startup keeps stdin open.

  Use only loopback test fixtures and generated runtime paths; do not add fixed ports or credentials.

- [ ] **Step 2: Run the focused Electron tests and verify the race/cleanup cases fail.**

  Run: `cd mvp/canvas-spike && npm test -- --grep "startup|handshake|backend|liveness|activate"`

  Expected: at least the duplicate-start or orphan-child assertion fails against the current `startBackend()`/fatal-exit paths.

- [ ] **Step 3: Implement single-flight startup and shared fatal cleanup.**

  Change `startBackend()` so concurrent callers await the same `startingBackend` promise and only the successful handshake assigns `backend`. Add a `stopAndExit(code)` helper that awaits `stopBackend()` before setting the quit flag and calling `app.quit()`; use it from ready rejection, activate rejection, renderer crash, window creation failure, and backend unexpected exit. Do not call `app.exit()` on these paths.

- [ ] **Step 4: Preserve parent liveness through stdin.**

  Spawn the child with `stdio: ["pipe", "pipe", "pipe"]`, write exactly one bootstrap line, and do not call `child.stdin.end()`. On the Python side, add a bounded parent-liveness watcher that requests uvicorn shutdown when stdin reaches EOF without logging the bootstrap value. Ensure normal Electron shutdown still calls `stopBackend()` and closes the pipe.

- [ ] **Step 5: Run Electron tests and the full Python/TypeScript suites.**

  Run: `cd mvp/canvas-spike && npm test`

  Then: `cd mvp && pytest -q`

  Expected: all existing and new tests pass; the app has no orphan backend after each lifecycle test.

- [ ] **Step 6: Commit.**

  `git add mvp/canvas-spike/src/main.ts mvp/canvas-spike/tests/lifecycle.spec.ts mvp/src/workbench/main.py && git commit -m "fix: make electron backend lifecycle single-flight"`

---

### Task 3: Make Vault recovery crash-consistent

**Files:**
- Modify: `mvp/src/workbench/credentials/vault.py`
- Modify: `mvp/src/workbench/credentials/service.py`
- Test: `mvp/tests/unit/credentials/test_vault.py`

**Interfaces:**
- Consumes: `CredentialVault.recover(path, password)`, `CredentialVault.open(path)`, and `VaultService.status/recover()`.
- Produces: a recovery transaction marker next to the vault, atomic publication of a complete replacement before any corrupt primary is retired, startup detection/repair of an interrupted recovery, and explicit `recovery_required` when neither a valid primary nor a complete replacement exists.

- [ ] **Step 1: Write failing crash-consistency tests.**

  Add tests that simulate interruption at each recovery boundary:
  - replacement document fully written but primary retirement interrupted; a new `VaultService` must find and publish the complete replacement, keep a backup, and report `locked` (not `uninitialized`);
  - marker written but replacement incomplete; a new `VaultService` must report `recovery_required` and leave both the original artifact and marker available for explicit recovery;
  - successful recovery must retain exactly one durable backup matching the corrupt input and must not expose plaintext secrets.

  Use monkeypatches around `os.replace`, `_write_temporary`, and directory fsync; tests must assert state and artifact presence rather than implementation call counts.

- [ ] **Step 2: Run the focused tests and verify they fail.**

  Run: `cd mvp && pytest -q tests/unit/credentials/test_vault.py -k recovery`

  Expected: the restart-after-backup-before-publish case either reports `uninitialized` or loses the primary under the current implementation.

- [ ] **Step 3: Implement a durable recovery transaction.**

  Add a versioned JSON marker containing only paths/phase metadata (no secrets), write and fsync it before recovery publication, create and fsync a complete replacement temporary file, atomically publish the replacement at the primary path, then atomically move the corrupt primary to a uniquely named backup and fsync the directory. On startup, `CredentialVault.open`/`VaultService.__init__` must inspect the marker and candidate files: finalize a complete replacement, or set `_recovery_required` without treating the path as uninitialized. Clear the marker only after the new primary is validated and durable.

- [ ] **Step 4: Preserve explicit recovery semantics and cleanup rules.**

  Keep `VaultService.recover(password)` the only operation that changes a corrupt vault into a new unlocked vault. Never delete the corrupt backup or marker on an unsuccessful attempt; wipe in-memory key material and release the writer lock on every exception.

- [ ] **Step 5: Run the focused and full Python suites.**

  Run: `cd mvp && pytest -q tests/unit/credentials/test_vault.py tests/acceptance/test_batch1_provider_center.py`

  Then: `cd mvp && pytest -q`

- [ ] **Step 6: Commit.**

  `git add mvp/src/workbench/credentials/vault.py mvp/src/workbench/credentials/service.py mvp/tests/unit/credentials/test_vault.py && git commit -m "fix: make vault recovery crash consistent"`

---

### Task 4: Batch 1 lifecycle acceptance and review handoff

**Files:**
- Modify: `mvp/tests/acceptance/test_batch1_provider_center.py` if acceptance coverage needs one lifecycle assertion
- Create: `mvp/tests/acceptance/test_batch1_lifecycle_hardening.py`
- Create: `.superpowers/sdd/2026-08-07-batch-1-lifecycle-hardening/final-checklist.md` (ignored workspace artifact)

**Interfaces:**
- Consumes: the hardened listener, Electron lifecycle, and Vault recovery behavior from Tasks 1–3.
- Produces: a reproducible acceptance report proving the Batch 1 gate is green and a clean review package for the final reviewer before Batch 2 planning starts.

- [ ] **Step 1: Write the acceptance checks.**

  Exercise the real Workbench app and backend boundary for: random-port handshake, capability-authenticated health, UI startup, graceful quit, backend crash/restart, and interrupted Vault recovery. Assert no fixed-port fixture receives traffic, no token is printed, and recovery states are explicit.

- [ ] **Step 2: Run acceptance before implementation changes.**

  Run: `cd mvp && pytest -q tests/acceptance/test_batch1_lifecycle_hardening.py`

  Expected: the lifecycle checks identify the current race or recovery failure before the hardening commits are present.

- [ ] **Step 3: Run the complete verification matrix after Tasks 1–3.**

  Run:
  - `cd mvp && pytest -q`
  - `cd mvp/canvas-spike && npm test`
  - `cd mvp && python -m workbench.validation.runner`

  Record exact pass counts and any platform-specific skips in `final-checklist.md`; do not claim Windows execution on a non-Windows host.

- [ ] **Step 4: Commit acceptance evidence.**

  `git add mvp/tests/acceptance/test_batch1_lifecycle_hardening.py && git commit -m "test: gate batch one lifecycle hardening"`

- [ ] **Step 5: Prepare final review package.**

  Generate the package with `scripts/review-package` from the lifecycle-hardening plan’s merge base through `HEAD`, then dispatch the final reviewer. Any Critical/Important finding gets one coordinated fix wave and one scoped re-review; residual load-bearing findings block Batch 2.
