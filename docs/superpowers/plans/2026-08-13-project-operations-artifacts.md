# Project Operations Artifacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stale tutorial-first entry with an evidence-backed product README, detailed build/run guidance, and three linked operations artifacts that expose current capability, limitations, and the Batch 3.1–3.3 path.

**Architecture:** The root README is the product landing page; `mvp/README.md` is the authoritative build and runtime guide. `docs/operations/api-inventory.json` is the structured evidence source used by the manual and the standalone interactive graph, while all planned capabilities remain visibly separated from implemented ones.

**Tech Stack:** Markdown, JSON, vanilla HTML/CSS/JavaScript, inline SVG, Python 3.11–3.13, uv, Node.js/npm, Electron, React, FastAPI, SQLite, LangGraph.

## Global Constraints

- Never include API keys, tokens, passwords, cookies, private prompts, hidden reasoning, or raw user histories.
- Do not claim Batch 3.1, 3.2, or 3.3 is implemented.
- Keep the graph standalone: no CDN, external scripts/fonts, network calls, `localStorage`, or `sessionStorage`.
- Preserve unrelated worktree files and do not stage runtime/build output.
- Use current branch and source evidence; label static, runtime-verified, inferred, and planned facts explicitly.

---

### Task 1: Product and Build Documentation

**Files:**
- Modify: `README.md`
- Modify: `mvp/README.md`

**Interfaces:**
- Consumes: current manifests, Electron launcher, Workbench settings, gate report.
- Produces: product quick start and authoritative source-build/run instructions.

- [ ] **Step 1: Replace the root README with the product entry**

Include the three functional elements, implementation status, repository map, five-minute source run, build/runtime locations, verification evidence, gap roadmap, and upstream lineage.

- [ ] **Step 2: Expand the MVP build manual**

Document `uv sync --extra dev --locked`, `npm ci`, `npm start`, `npm run build`, Python/Node locations, environment boundaries, Provider Center, LM Studio, Engine Host, test commands, troubleshooting, and the absence of distributable installers.

- [ ] **Step 3: Verify commands and links**

Run:

```bash
test -f mvp/uv.lock && test -f mvp/canvas-spike/package-lock.json
rg -n 'uv sync --extra dev --locked|npm ci|npm start|dist-electron|GO_LANGGRAPH_RUNTIME' README.md mvp/README.md
```

Expected: both manifests exist and every required operational topic appears.

---

### Task 2: Operations Manual and Interface Inventory

**Files:**
- Create: `docs/operations/PROJECT_OPERATION_MANUAL.md`
- Create: `docs/operations/api-inventory.json`

**Interfaces:**
- Consumes: FastAPI routes, Electron IPC allowlist, frontend API client, scripts, settings, tests, and Task 1 documentation.
- Produces: human-readable operation manual and machine-readable interface evidence.

- [ ] **Step 1: Write the JSON inventory**

Inventory health, Provider/Vault, conversations, legacy run lifecycle, Engine Host diagnostics, acceptance/gate scripts, and Electron-owned startup. Record normalized path/command, source, authentication, inputs, outputs, side effects, UI path, evidence status, and verification.

- [ ] **Step 2: Validate the inventory**

Run:

```bash
python3 -m json.tool docs/operations/api-inventory.json >/dev/null
python3 -c 'import json; p=json.load(open("docs/operations/api-inventory.json")); ids=[x["id"] for x in p["interfaces"]]; assert len(ids)==len(set(ids)); assert all(x["evidence_status"] in {"verified","inferred","dynamic/unresolved","planned"} for x in p["interfaces"])'
```

Expected: exit 0.

- [ ] **Step 3: Write the operation manual**

Link every interface section to an inventory ID, separate real/fixture/planned UI, document data flows and side effects, and finish with a P0/P1/P2 gap matrix and Batch 3.1 recommendation.

- [ ] **Step 4: Verify manual links**

Run:

```bash
rg -n 'api-inventory.json|project-operation-knowledge-graph.html|Batch 3.1|Batch 3.2|Batch 3.3' docs/operations/PROJECT_OPERATION_MANUAL.md
```

Expected: all three outputs and roadmap stages are referenced.

---

### Task 3: Offline Interactive Capability and Gap Graph

**Files:**
- Create: `docs/operations/project-operation-knowledge-graph.html`

**Interfaces:**
- Consumes: the three-element capability model and P0/P1/P2 gap matrix.
- Produces: a layered/force graph with stable nodes and evidence-aware details.

- [ ] **Step 1: Copy the approved graph template**

Copy `/Users/sushi/.agents/skills/interactive-knowledge-graph/assets/knowledge-graph-template.html`, preserving the engine and replacing only title, description, groups, layers, nodes, and edges.

- [ ] **Step 2: Model current and planned capabilities**

Use five layers and stable IDs. Mark UI fixtures/localStorage boundaries, real runtime/control-plane features, external model/Data Platform dependencies, and Batch 3.1–3.3 nodes.

- [ ] **Step 3: Validate JavaScript and graph integrity**

Extract the largest inline script, run `node --check`, validate all edge endpoints and group-layer mappings, and reject external URLs/scripts/fonts or browser storage.

- [ ] **Step 4: Visually inspect the graph**

Open the local file in a browser or Playwright, verify Layered/Force switching, search, legend filtering and node detail, and capture no runtime console errors.

---

### Task 4: Cross-Artifact Verification and Commit

**Files:**
- Verify: `README.md`
- Verify: `mvp/README.md`
- Verify: `docs/operations/PROJECT_OPERATION_MANUAL.md`
- Verify: `docs/operations/api-inventory.json`
- Verify: `docs/operations/project-operation-knowledge-graph.html`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: one clean, auditable documentation commit.

- [ ] **Step 1: Run sensitive-value scan**

Run a pattern scan for GitHub tokens, API keys, Data Platform token assignments, passwords, and secret-bearing URLs across the five deliverables. Expected: no match.

- [ ] **Step 2: Run structural verification**

Run JSON parsing, graph validation, link/file existence checks, `git diff --check`, and inspect `git status --short`.

- [ ] **Step 3: Review claims against source evidence**

Confirm each “implemented” claim maps to code/tests and each Batch 3.1–3.3 capability is labelled planned. Confirm the installer limitation and build-output paths are explicit.

- [ ] **Step 4: Commit intended files**

```bash
git add README.md mvp/README.md docs/operations docs/superpowers/specs/2026-08-13-project-operations-artifacts-design.md docs/superpowers/plans/2026-08-13-project-operations-artifacts.md
git commit -m "docs: publish workbench operations guide"
```
