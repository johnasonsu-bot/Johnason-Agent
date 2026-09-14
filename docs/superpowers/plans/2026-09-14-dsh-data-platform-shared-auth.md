# DSH Data Platform Shared Authorization Implementation Plan

> For agentic workers: Use superpowers:subagent-driven-development. Preserve existing user edits and never delete files.

**Goal:** DSH 原生授权接入无 HTTP 平台共享核心，平台与 CLI 配置并校验 RBAC/ABAC。
**Architecture:** 统一 createCore、共享认证上下文、ABAC 服务、平台配置 UI、DSH authorization flow。
**Tech Stack:** Node.js CommonJS/ESM, MySQL, React/Ant Design, node:test。
**Spec:** ../specs/2026-09-14-dsh-data-platform-integration-design.md

## Global Constraints
不通过 HTTP 调用平台；不得保存明文凭据；RBAC 复用；每次权威校验；保留未提交变更；不删除文件或真实数据。

## Task 1 — Platform shared core / ABAC
Ownership: data-platform-dsh-auth/backend/** only.
- [x] Add node:test behavior tests before implementation: read-only denial; RBAC denial even if ABAC allows; bound scope denial; attribute missing denial; explicit deny precedence; version conflict; non-admin config write denial.
- [x] Extract backend/src/core/session.js and index.js. createCore returns login/profile/logout/execute/close; execute accepts {token,projectId,command,input}. Reuse existing auth/system/project services and schema validation.
- [x] Add modules/access-control/{schema,engine,repository,service,routes}.js, isolated SQL migration. Config contract is specified in spec. Save atomically with version condition; query authoritative user assignment.
- [x] Wire existing auth middleware and new management routes to same access-control service. Guard logout from policy lockout, preserve project selection handling.
- [x] Run node --test for new tests and relevant existing tests. Report baseline failures separately.

## Task 2 — DSH native flow / CLI / UI
Ownership: dsh-standalone-agent/apps/dsh-agent/** only.
- [x] Add failing tests for grant lifecycle, no credential echo, unsupported commands, bound core identity, cancellation and locked vault.
- [x] Add local core adapter with explicit absolute core path, authorization plugin via registerFlow, encrypted grant using credentials, lifecycle close. Never fetch platform APIs.
- [x] Integrate dataplatform mode in launcher/profile while preserving existing profile entries. Add /dataplatform local page and structured command execution using core contract.
- [x] Provide secure terminal secret input, no password/token flags, optional nonsecret JSON input, stable errors. Expose same core execute as agent tool without secrets.
- [x] Run focused tests plus existing profile/launcher/vault/native tests.

## Task 3 — Platform permissions UI and end-to-end verification
Ownership: data-platform-dsh-auth/frontend/** and documentation.
- [x] Add shared accessControl service to /access-control/config, /catalog, /check routes. Responses use existing success envelope.
- [x] Add permission panel to role and user management. Separate elements (attributes/scopes/policies) from per-user assignments; use shared versioned config, safe error display and check results.
- [x] Typecheck/build, run backend/DSH regression and review entire integration for privilege escalation and drift.
- [x] Record verified results, database migration command and startup instructions; do not imply real deployment or live database tests if unavailable.
