# Batch 3.2 Research Graph Validation

## Decision

`GO_DEVELOPMENT_GRAPH`

The approved research blueprint passes its deterministic release gate while the
Batch 3.1 sequential fallback remains `GO_RESEARCH_GRAPH`.

## Verified scenario

- Planner and `research-blueprint@1.0.0` compile to the same semantic graph.
- The plan explicitly proposes two temporary Agents and requires user approval.
- User approval reduces the proposed concurrency from four to two.
- Research, comparison, fact checking, and gap analysis fan out independently.
- Fact checking is rejected once by its local Verifier and only that branch reruns.
- The overall Supervisor identifies a conflict and Arbitration pauses for a human
  preference before Merge.
- A simulated process crash occurs before the first Merge result is committed.
  A fresh graph/checkpointer instance resumes without repeating verified branches.
- Replanning creates immutable version 2. Research, comparison, and gap-analysis
  results remain reusable; changed fact checking and downstream Merge do not.
- The final report contains claim-to-evidence mappings and is published as a
  content-addressed `text/markdown` Artifact.

## Evidence

- Exact acceptance: `1 passed`.
- Sequential baseline runner: `GO_RESEARCH_GRAPH`.
- Research acceptance runner: `GO_DEVELOPMENT_GRAPH`.
- Runtime result: `mvp/.runtime/research-graph-results.json`.
- Credential/private-context marker result: no matches.

## Next implementation boundary

The gate validates the research graph, checkpoint recovery, replan semantics and
Artifact contract. The next task must bind plan approval to a durable runtime
admission service and publish real checkpoint-derived Domain Events. Frontend
fixture events are not evidence of production execution and must not be used as
the completion criterion for that task.
