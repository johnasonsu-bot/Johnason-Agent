# Hybrid Multi-Agent Workbench Program

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved hybrid multi-Agent Workbench through sequential, independently testable batches, including a contract-first Go Engine Host insertion before multi-Agent execution.

**Architecture:** Workbench remains the durable Python control plane. A versioned `ExecutionRunner` boundary selects the existing Python Agent Runtime or an isolated Go Engine Host sidecar for one Agent Run; multi-Agent orchestration, durable state, credentials, and Artifacts remain in the control plane. Model, Skill, tool, connector, AG-UI, and Artifact capabilities are exposed through narrow versioned interfaces.

**Tech Stack:** Python 3.11-3.13, FastAPI, Pydantic 2, SQLite, HTTPX, cryptography, Argon2id, React, TypeScript, Electron, Vite, Playwright, AG-UI-compatible SSE.

## Global Constraints

- Execute plans strictly in the listed order; a batch cannot start before the prior batch gate passes.
- Use TDD for every behavior change and commit after each independently reviewable task.
- API keys must never enter source, Git, logs, normal configuration, event payloads, Artifact metadata, or business tables.
- The app must be cross-platform and must not depend on macOS Keychain.
- Raw hidden chain-of-thought must not be exposed; only decision summaries and tool evidence may reach the UI.
- Tasks do not stop automatically because of time, token, cost, or loop thresholds.
- Every batch requires an operable UI path; backend probes alone cannot pass a gate.

## Ordered Delivery Documents

1. [Batch 1 — Provider Center](2026-08-06-batch-1-provider-center.md)
2. [Batch 2 — Real Agent Conversation](2026-08-06-batch-2-real-agent-conversation.md)
3. [Batch 2.5 — Go Engine Host Contract Design](../specs/2026-08-11-go-engine-host-contract-design.md)
4. [Batch 3 — Sequential Multi-Agent Review Loops](2026-08-10-sequential-multi-agent-review-loops.md)
5. G2 — Read-only Engine Shadow, only after Batch 3 preserves a single durable execution graph
6. G3/G4 — Single-Agent then per-node Go cutover, with Python control-plane rollback
7. [Batch 4 — Artifacts and Real Tools](2026-08-06-batch-4-artifacts-and-tools.md)
8. [Batch 5 — Supervisor, Recovery, and Release Gate](2026-08-06-batch-5-supervisor-recovery.md)
9. G6 — Control-plane re-evaluation only after independent Go durable-execution evidence

Batch 2.5 G0/G1 is the next implementation unit. Its detailed implementation plan is generated after the written design review required by the project workflow.

## Program Completion Gate

Run from `mvp/`:

```bash
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -v
npm test --prefix canvas-spike
.venv/bin/python scripts/run_hybrid_multi_agent_acceptance.py
```

Expected: all automated suites pass and the acceptance script reports `GO_TEST_RELEASE` only after the UI-driven four-Agent restart-recovery scenario completes.
