# SDD ledger — plan: docs/superpowers/plans/2026-08-27-batch-3-4-b-python-term-runtime.md

Authority: docs/superpowers/specs/2026-08-26-runtime-federation-design.md (Chinese body).
Plan base: cd6a04c (the DeepSeek priority fix is already isolated in 14dfe78).

## Pre-flight interface/conflict scan

| Tasks | Producer → consumer / shared surface | Finding |
|---|---|---|
| 1 → 5 | `sdk_adapter.py`: pinned SDK facade/session → runtime execution and event mapping | Compatible; Task 5 must extend the Task 1 seam rather than bypass it. |
| 1 → 6 | `python_term/__init__.py`: stable exports → runtime registration | Compatible; Task 6 may export only stabilized Task 1–5 boundaries. |
| 2 → 3 | `repository.py`: durable Term/Step/Effect schema → Tool Effect lifecycle | Compatible; Task 3 must reuse Task 2 transactions and identity checks. |
| 2 → 5 | `repository.py`: cursor/checkpoint/projection → recovery runtime | Compatible; Task 5 consumes authoritative repository methods, not a second store. |
| 3 → 4 | Tool Router executor seam → supervised PTY executor | Compatible; PTY remains inside the same Permission/Workspace/Effect lifecycle. |
| 3 → 5 | Tool lifecycle/events → SDK Tool wrapper and Runtime events | Compatible; normalized/redacted results only. |
| 4 → 5 | PTY cleanup/output evidence → runtime checkpoint and recovery | Compatible; raw secret/oversize output cannot enter public events. |
| 5 → 6 | `PythonTermRuntime` capability → Host v2 registry/routing | Compatible; feature flag and durable runtime pin remain control-plane owned. |
| 5 → 7 | runtime, cursor, recovery behavior → deterministic gate | Compatible; gate must invoke real SDK/Runtime paths, not fixtures. |
| 6 → 7 | routing/diagnostics → acceptance and user environment | Compatible; diagnostics remain read-only and credential-free. |
| 1 | Fixed dependency, provenance tests, real Runner seam, frozen read-only Session | Internally consistent with spec §6.1; no conflict found. |
| 2 | Immutable contracts, three state layers, migrations, durable repository | Internally consistent with spec §§5.1, 5.6, 6.2–6.3; no conflict found. |
| 3 | Fail-closed Tool Router and Effect lifecycle | Internally consistent with spec §§5.5, 6.4; no conflict found. |
| 4 | Supervised PTY with argv-only execution and bounded output | Internally consistent with spec §6.5; no conflict found. |
| 5 | Runtime, normalized events, cursor/checkpoint/recovery | Internally consistent with spec §§5.2, 5.6, 6.6; no conflict found. |
| 6 | Feature-flag routing, v1 compatibility, durable pin, diagnostics | Internally consistent with spec §5.7 and rollback policy; no conflict found. |
| 7 | Deterministic gate, full regressions, report, Electron environment | Internally consistent with spec §6.6; external LM Studio remains separately reported. |

Ruling: The plan file is committed as the execution baseline before Task 1 — this makes task briefs and review ranges reproducible — cost if wrong: one documentation-only commit can be reverted without changing runtime behavior.

Task 1: fix round 1/5 (4 addressed, 0 open; commits 89795eb..c279695)
Task 1: complete (commits cd6a04c..c279695, review clean)

Task 2: Ruling: rename the pre-existing `tests/unit/agents` test package to a non-conflicting test-only package before review — the pinned SDK owns top-level `agents`, and split-suite evidence cannot replace the standard single-process backend gate — cost if wrong: external scripts that hard-code the old test directory need a path update; test content and production behavior remain unchanged.
Task 2: fix round 1/5 (5 addressed, 3 open; commits b37923b..b9ca9ae)
Task 2: fix round 2/5 (1 addressed, 4 open; commits b9ca9ae..5ed5d2d)
Task 2: fix round 3/5 (3 addressed, 2 open; commits 5ed5d2d..554751a)
Task 2: fix round 4/5 (2 addressed, 0 open; commits 554751a..a71e680)
Task 2: complete (commits c279695..a71e680, review clean)

Task 3: fix round 1/5 (2 addressed, 8 open; commits 83e0ded..8814388)
Task 3: fix round 2/5 (5 addressed, 3 open; commits 8814388..70dfcb9)
Task 3: fix round 3/5 (2 addressed, 1 open; commits 70dfcb9..4c398fa)
Task 3: fix round 4/5 (partial hardening addressed, 3 open; commits 4c398fa..43ae8a5)
Task 3: fix round 5/5 (1 addressed, 2 open; commits 43ae8a5..448d947)
Task 3: parked — arbitrary caller can self-compose RuntimeRegistry/replace dispatcher — Ruling: real and load-bearing; Task 4 must replace caller-supplied callable composition with a control-plane-owned fixed dispatcher and declarative opaque executor descriptors before PTY integration. Arbitrary in-process reflection is not treated as a security boundary.
Task 3: parked — callable object-graph scanner remains bypassable through descriptors/Event/Future/module policy mutation — Ruling: real and load-bearing; Task 4 must remove callable graph scanning from the authorization proof and route only immutable declarative executor descriptors through the fixed Host dispatcher.
Task 3: complete (commits a71e680..448d947, 2 parked with load-bearing rulings carried into Task 4)
Task 4: started at 448d947 with Task 3 breaker rulings as mandatory prerequisites
Task 4: fix round 1/5 (5 addressed, 1 open; 2 minors carried into round 2; commits 4cd44f5..8a874ee)
Task 4: Ruling — macOS cannot provide race-free descendant identity through libproc/kqueue (NOTE_TRACK/NOTE_CHILD unsupported; coalition launchd-only). Batch 3.4-B narrows the adapter to verified single-process PTY: kernel deny-process-fork plus per-execution EPERM probe, with non-macOS/unavailable verification fail-closed. Cross-platform containment remains a later platform adapter, not an OS-sandbox claim.
Task 4: fix round 2/5 (3 addressed, 0 open; commits 8a874ee..e6067dd)
Task 4: complete (commits 448d947..e6067dd, review clean)
Task 5: started at e6067dd
Task 5: fix round 1/5 (2 addressed, 4 open; commits 240954a..9968b3a)
Task 5: fix round 2/5 (1 addressed, 3 open; 1 minor carried into round 3; commits 9968b3a..cd73402)
Task 5: fix round 3/5 paused by user after focused implementation reached 54 Task 5 integration tests passing; Task 3/4/full regression, report, commit-quality review remain pending.
Pause checkpoint: local-only snapshot requested before network loss; no GitHub push. Resume from the Task 3 focused regression, then Task 4/full unit/compile/diff and independent round-3 review.
Task 5: fix round 3/5 (4 prior findings addressed in substance; 3 new Important recovery/lease findings open; commits cd73402..11c7c27)
Task 5: Ruling — Effect lease freshness is a separate authorization boundary from the Step claim and must be checked against SQLite trusted time at durable release and immediately before dispatch — cost if wrong: a stale Effect owner can execute concurrently with its successor.
Task 5: Ruling — read successor recovery must preserve verifiable predecessor lineage across checkpoints; physical replacement without a validated transition is rejected — cost if wrong: an additional immutable lineage row/schema transition, but crash recovery remains provable.
Task 5: Ruling — a durable pending write has not crossed the dispatch gate and is safe to resume under an exact current fence; only released/ambiguous writes require reconciliation — cost if wrong: recovery code must distinguish dispatch states instead of conservatively blocking all reserved writes.
Task 5: fix round 4/5 (3 addressed, 2 open; commits 11c7c27..1981672)
Task 5: Ruling — checkpoint lineage validation must accept a continuous multi-hop successor chain rooted in prior trusted evidence, not only one generation — cost if wrong: more explicit ordered-chain validation, but repeated crashes remain recoverable without accepting branches.
Task 5: Ruling — legacy active attempts above zero are invalid unless their missing predecessor chain can be reconstructed from fully validated latest-checkpoint evidence in the same migration transaction; otherwise migration fails closed — cost if wrong: some Round-3 development databases require explicit reconciliation instead of silent upgrade.
Task 5: fix round 5/5 (2 addressed, 0 open; commits 1981672..1dac595)
Task 5: complete (commits e6067dd..1dac595, final scoped review clean)
