# Phase 0 Validation Report

- Commit: `731edf25890e43a167f5a343ee5a10654bf9d597`
- Decision: **BLOCKED**
- Recovery guarantee: Step-boundary; token-generation recovery is not claimed.

| Check | Status | Evidence |
|---|---|---|
| `hermes.event_compatibility` | **pass** | Required Hermes event families are present; revision=01a1037d1e6d7b6eb96a786ef282c3aea4818194 |
| `lmstudio.tool_calling` | **blocked** | LMSTUDIO_MODEL is not configured; base_url=http://127.0.0.1:1234; loaded_models=gemma-4-31b-it,text-embedding-nomic-embed-text-v1.5 |
| `workflow.step_recovery` | **pass** | Committed effect survived restart without replay; guarantee=step-boundary; external_id=job-42 |
| `agui.projection` | **pass** | Domain event projected to AG-UI without state mutation |
| `data_platform.dual_channel` | **blocked** | Data Platform API, job ID, and CDP URL are required |
| `canvas.sandbox` | **pass** | Electron Canvas sandbox and renderers passed |

## Decision Rule

Phase 1 may start only when every required check is `pass`. A `blocked` external dependency remains a decision-gate blocker rather than a mocked pass.
