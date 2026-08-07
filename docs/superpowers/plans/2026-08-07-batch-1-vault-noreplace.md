# Batch 1 Vault No-Replace Recovery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the Vault recovery copy fallback from overwriting a backup that appears between existence checking and publication.

**Architecture:** Keep hard-link backup creation as the preferred atomic no-replace path. When hard links are unavailable, create the backup destination with exclusive creation (`O_CREAT|O_EXCL`) and stream-copy into that newly owned file; a destination race fails without replacing the existing file. Recovery remains marked until the new backup is fully fsynced and validated.

**Tech Stack:** Python 3.13, pytest, `os.open`, `os.fsync`, same-directory Vault artifacts.

## Global Constraints

- Do not write API keys, passwords, tokens, or capability values to source, tests, logs, or fixtures.
- Never overwrite an existing recovery backup; preserve sentinel bytes and leave the marker for explicit recovery.
- Do not publish the replacement primary until the backup is complete, fsynced, and directory-synced.
- Preserve `recovery_required` startup semantics and all existing Provider Center/UI behavior.
- Every production change must have a failing test first and a focused/full test run afterward.

---

### Task 1: Exclusive backup fallback

**Files:**
- Modify: `mvp/src/workbench/credentials/vault.py:729-770`
- Test: `mvp/tests/unit/credentials/test_vault.py`

**Interfaces:**
- Consumes: `_copy_recovery_backup(source: Path, destination: Path)` and `_ensure_recovery_backup()`.
- Produces: no-replace fallback semantics where an existing destination causes a non-destructive recovery failure, and a successful destination is fsynced before recovery publishes the replacement.

- [ ] **Step 1: Write the failing race test.**

  Add a test that forces hard-link creation to raise `EPERM`, pre-creates a backup path containing sentinel bytes immediately before the fallback publication boundary, invokes recovery, and asserts:
  - recovery raises `VaultRecoveryRequiredError`;
  - the sentinel backup remains byte-for-byte unchanged;
  - the corrupt primary remains unchanged;
  - the recovery marker remains present;
  - a fresh `VaultService` reports `recovery_required`.

  Add a success test that forces hard-link creation to raise `EPERM`, runs recovery without a race, and asserts the copied backup equals the corrupt primary and the recovered vault unlocks after restart.

- [ ] **Step 2: Run focused tests to verify RED.**

  Run: `cd mvp && .venv/bin/pytest -q tests/unit/credentials/test_vault.py -k "no_replace or hard_link_fallback"`

  Expected: the race test fails because the current `os.replace(temporary_path, destination)` overwrites the sentinel backup.

- [ ] **Step 3: Implement exclusive fallback publication.**

  Replace the check-then-`os.replace` path in `_copy_recovery_backup` with:

  ```python
  descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  ```

  Stream-copy the source into the exclusively-created destination, flush and `os.fsync` the descriptor, close it, and let `_ensure_recovery_backup` fsync the directory. If exclusive creation raises `FileExistsError`, propagate it to the existing `VaultRecoveryRequiredError` conversion; never unlink or replace the pre-existing destination. If a copy fails after exclusive creation, leave the artifact and marker for explicit recovery rather than deleting evidence.

- [ ] **Step 4: Run focused and full suites.**

  Run:
  - `cd mvp && .venv/bin/pytest -q tests/unit/credentials/test_vault.py -k "no_replace or hard_link_fallback"`
  - `cd mvp && .venv/bin/pytest -q`

  Expected: focused tests pass and all Python tests remain green.

- [ ] **Step 5: Commit.**

  `git add mvp/src/workbench/credentials/vault.py mvp/tests/unit/credentials/test_vault.py && git commit -m "fix: publish vault backups without replacement"`

### Task 2: Scoped review and Batch 2 gate

**Files:**
- Create: `.superpowers/sdd/2026-08-07-batch-1-vault-noreplace/final-checklist.md` (ignored workspace artifact)

- [ ] **Step 1: Record focused evidence.**

  Record the race and success test output, the full Python suite result, and the fact that Windows was not executed on this macOS host.

- [ ] **Step 2: Generate a scoped review package and dispatch review.**

  The reviewer must verify no existing backup can be overwritten, no replacement is published before durable backup, and no new Critical/Important issues were introduced.

- [ ] **Step 3: Decide the Batch 2 gate.**

  Only a clean scoped review unlocks Batch 2. Any remaining Important/Critical finding keeps the project paused.
