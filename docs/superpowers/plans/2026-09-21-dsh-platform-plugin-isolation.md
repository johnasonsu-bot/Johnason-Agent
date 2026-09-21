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
- [ ] Run source tests and review; inspect actual automatic cleanup targets and obtain the user-required cleanup confirmation.
- [x] Build the non-deleting sidecar and generate real receipt/source/gate manifests; verify source-build provenance without claiming native runtime acceptance.
- [ ] Install frozen dependencies and build official runtime after cleanup confirmation. Run native and browser integration and report any blocked test accurately.
- [ ] Commit code and verified generated artifacts, keeping source changes separate from post-build attestation when necessary for HEAD-bound gates.

Focused baseline from the preceding upgrade is 134 passing source checks. Run new contract checks first to observe missing boundaries, then retain behavior checks during the refactor. Run built integration only after the concrete cleanup authorization.
