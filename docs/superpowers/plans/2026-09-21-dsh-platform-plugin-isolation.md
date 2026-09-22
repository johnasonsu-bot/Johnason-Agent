# DSH Platform and Plugin Isolation Implementation Plan

**Goal:** Centralize upstream upgrade compatibility, isolate local plugins, record unresolved extension seams, and build a verified candidate.

**Architecture:** Keep upstream clean. Put platform loading/profile/version compatibility behind explicit modules; let a local plugin registry supply entries, resources and lifecycle hooks. Preserve old import paths as forwarding modules.

**Tech Stack:** Node ESM, Cordis, TypeScript sidecar, Python source gate.

**Spec:** `docs/superpowers/specs/2026-09-21-dsh-platform-plugin-isolation-design.md`

## Constraints

- Use the existing isolated worktree; preserve original worktrees and user data.
- Never delete files without explicit confirmation. Retain forwarding files instead of deleting old implementations.
- Preserve encryption, required memory events, source hashes, and fail-closed build gates.
- No production switch or real model/business API operation during build verification.

## Review focus

- Plugin relocation must preserve resource paths and exported module identity.
- Profile composition must reject duplicate/unprotected credential providers.
- Systems preparation must precede environment capture, including failures and dump-config paths.
- A new adapter module must be tracked in both build receipt and Python source gate.
- Upstream/private APIs remaining behind adapters must be named as coupling, not presented as stable independent ABI.

## Tasks

- [x] Add failing plugin boundary/compatibility tests. Relocate credentials, systems and memory implementations with original-path forwarding; introduce narrow SDK bridges and retain real-source contract tests.
- [x] Centralize platform paths and launcher/profile APIs; add a declarative plugin registry with profile entries, browser resources, validation, preparation and disposal hooks. Update source-runner/profile tests to exercise the real composition root.
- [x] Isolate Workbench official imports, add an adapter contract boundary, update sidecar source/artifact inventories and regression tests. Preserve declared capabilities.
- [x] Add platform/plugin boundary audit and upgrade contract command, and an evidence-based unresolved-isolation/evolution list.
- [x] Run source tests and review; inspect actual automatic cleanup targets and obtain the user-required cleanup confirmation.
- [x] Build the non-deleting sidecar and generate real receipt/source/gate manifests; verify source-build provenance without claiming native runtime acceptance.
- [x] Install frozen dependencies and build official runtime after cleanup confirmation. Run native and browser integration and report any blocked test accurately.
- [x] Commit code and verified generated artifacts, keeping source changes separate from post-build attestation when necessary for HEAD-bound gates.

Focused baseline from the preceding upgrade is 134 passing source checks. Run new contract checks first to observe missing boundaries, then retain behavior checks during the refactor. Run built integration only after the concrete cleanup authorization.

## Build continuation

The user accepted the design and asked to start the changes after receiving the concrete cleanup scope. Continue the official build only inside this candidate. Generated build outputs and this run's installation temporary files are within scope; original worktrees, source files, Vault, business databases and user sessions are excluded. Corepack/npm caches, Python environments and temporary paths use `.vendor/dsh-build/`. pnpm 11 ignored the supplied npm-prefixed store variable and used its existing shared package store; no shared-store cleanup is run. Use `pnpm_config_store_dir` for any future isolated installation. No deployment or historical data migration is included.

Native/macOS ARM, Host/Client and Web builds completed with exit 0. Built integration exposed and fixed file-URL provider identity, Session snapshots, async confinement, the DeepSeek request-extension callback, client package resolution scope/declarations and native Workspace membership. Final App: 196 passed / 1 explicit business-environment skip; Sidecar: 60 passed; selected Python: 1,145 passed / 5 Goose-only exclusions; source/native-process acceptance: 4 passed. The App checks include editable native chat and no external HTTP requests. Original worktrees and all 890 baseline tracked paths remain intact. Full evidence and migration limits are in the verification report and architecture inventory.

Independent reviews found no blocking issue in the native compatibility fixes or the Workspace configuration lifecycle. Source isolation is committed as `90ee3ad`, and build-discovered compatibility fixes as `f5c682c`; generated receipts/manifests and final evidence are committed separately. Keep the verified candidate on `codex/dsh-platform-plugins`, preserving the original runtime and worktrees.
