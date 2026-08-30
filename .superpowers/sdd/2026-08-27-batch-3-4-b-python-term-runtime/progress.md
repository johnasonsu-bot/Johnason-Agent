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
Task 6: started at 810af79; feature flag defaults closed, durable pin and Host v1 compatibility remain mandatory, and Task 7 gate eligibility must not be predeclared or faked.
Task 6: fix round 1/5 started from review of 2833dbb (1 Critical, 4 Important, 1 Minor open).
Task 6: Ruling — capability metadata is not gate proof; Task 6 may register and diagnose a runtime but production admission remains unavailable until Task 7 provides a fixed control-plane proof bound to source/build/capabilities/gate result — cost if wrong: Python Term cannot be selected by users until the deterministic gate exists, avoiding a false GO.
Task 6: Ruling — explicit runtime selection must enter through the real Conversation message API and a narrow router dependency, not `app.state`; omission preserves the v1 path — cost if wrong: a minimal additive request field and turn-state identity are introduced before UI selection is expanded.
Task 6: fix round 1/5 (6 addressed in part, 4 Important open; commits 2833dbb..e867c3c)
Task 6: Ruling — a routable envelope must be compiled from authoritative Provider, Agent, Project Context and Conversation snapshots; request strings are selectors, not frozen evidence — cost if wrong: admission performs additional read-only repository lookups before pinning.
Task 6: Ruling — the v2 internal command ID is derived from `(session_id, external command_id)` because the v2 pin table is global while Conversation idempotency is session-scoped — cost if wrong: diagnostics must distinguish internal pin identity from the external API key.
Task 6: Ruling — the supported Electron backend serializes same-session admission with the existing asyncio session lock across reservation check, route/pin and reservation; this is not claimed as a distributed lock — cost if wrong: same-session requests are serialized, while different sessions remain concurrent.
Task 6: fix round 2/5 (4 prior findings addressed; 4 Important identity/recovery/test findings open; commits e867c3c..4a9b197)
Task 6: Ruling — the Provider snapshot must include every non-secret execution-affecting field, including safe headers, while excluding credentials and secret references/values — cost if wrong: a retry could silently change adapter behavior without an identity conflict.
Task 6: Ruling — model aliases are selectors only; a routable turn must persist and execute the resolved concrete model everywhere, and `default` fails closed when no saved default exists — cost if wrong: the pinned envelope and durable turn can describe different model identities.
Task 6: Ruling — an accepted v1 retry may use the read-only fast path only when its queued event exists; a crash between reservation and queued-event append must re-enter the locked idempotent repair path — cost if wrong: a durable turn can remain permanently invisible to the worker.
Task 6: fix round 3/5 started from review of 4a9b197.
Task 6: fix round 3/5 (4 prior findings addressed; 2 Important recovery/test findings open; commits 4a9b197..6c3d77c)
Task 6: Ruling — recovery of an accepted v1 turn with a missing queued event is based on the durable turn provider/model identity and must repair that event before returning terminal or paused state; current provider selection is irrelevant after acceptance — cost if wrong: queued history may be permanently incomplete or recovery may fail after configuration changes.
Task 6: Ruling — same-session v1/Python-Term admission races require deterministic barriers proving the loser reached the lock/admission boundary, with both winner orders covered — cost if wrong: the regression can pass even if lock serialization is removed.
Task 6: fix round 4/5 started from review of 6c3d77c with a fresh implementer.
Task 6: fix round 4/5 (2 prior findings addressed; 2 Important projection/test findings open; commits 6c3d77c..8e303b1)
Task 6: Ruling — a late history-repair queued event may preserve audit completeness but cannot regress a terminal or paused public projection; terminal responses and UI reducers must remain monotonic — cost if wrong: recovery history remains append-only while public state explicitly ignores stale lifecycle transitions.
Task 6: Ruling — deterministic concurrency tests must instrument a real `asyncio.Lock`, prove the loser is a true waiter, and assert it cannot enter admission before winner release — cost if wrong: the test gains a small observable-lock wrapper but validates actual mutual exclusion rather than its own scheduler.
Task 6: fix round 5/5 started from review of 8e303b1 with a fresh implementer.
Task 6: fix round 5/5 (1 prior production finding addressed, 1 test-proof finding open; commits 8e303b1..6d6b15b)
Task 6: parked — bypassing the complete `async with` can still pass durable-state assertions because the protected admission helper is synchronous on the single Electron event loop; the current observable real-lock test proves a waiter and mutual exclusion but not lock ownership at the exact admission call — Ruling: production behavior is correct and Task 6 routing is complete; Task 7 gate must add an observable real-lock owner assertion at admission and a bypass mutation that fails that ownership assertion before issuing proof. Cost if wrong: one test-only lock-observation seam is carried into the final Runtime gate, not into HTTP or caller metadata.
Task 6: complete (commits 810af79..6d6b15b, production review clean; 1 load-bearing gate-proof item carried into Task 7)
Task 7: started at 6d6b15b; no GO may be issued until the carried lock-ownership proof, real fixed executor composition, deterministic gate matrix, full regressions and credential scan all pass on one fixed revision.
Task 7: fix round 1/5 (initial gate self-signing, retry, projection, continuation and bounded-read findings addressed; 3 trust/reconciliation findings open; commits c46dd68..b9f7cd1)
Task 7: fix round 2/5 (external signer/dev trust, continuation isolation and reconciliation state addressed; 2 production trust/control-plane findings open; commits b9f7cd1..e5c4fb4)
Task 7: fix round 3/5 (fixed production root, public reconciliation control plane and build manifest addressed; 3 release/idempotency findings open; commits e5c4fb4..b1f0b2c)
Task 7: fix round 4/5 (proof/manifest cycle, exact installed-set verification and durable idempotency ledger addressed; 1 terminal replay finding open; commits b1f0b2c..d7bd31f)
Task 7: fix round 5/5 (cached-response terminal replay addressed; 1 reserved/no-response crash-window finding remains; commits d7bd31f..885eb93)
Task 7: blocked — reconciliation still spans separate Effect commit, Conversation transition and idempotency-response transactions. A crash after the last pending Effect requeues the turn but before the ledger response is stored can let the worker complete and compact the turn, leaving a reserved/no-response command that cannot be replayed after restart. The five SDD fix rounds are exhausted; a separately approved follow-on task must atomically commit the Conversation transition and ledger response, or persist an immutable response intent sufficient for recovery, before Task 7 can be marked complete or production GO can be claimed.
Task 8: approved follow-on started at 42806a1 — atomically commit the Conversation reconciliation transition and idempotency response under one SQLite transaction; Task 7 remains blocked until Task 8 implementation and independent review are clean.
Task 8: initial implementation committed the Conversation transition and reconciliation response atomically (commit 9db02df); independent review found legacy queued/null-response upgrade states unrecoverable.
Task 8: fix round 1/5 added fail-closed legacy recovery and a single-Effect/single-command-key reservation fence (commit 24cb7a8); review found the recovered Effect was not fully bound to the current Python Term.
Task 8: fix round 2/5 bound recovery through Effect, Step and durable Term identity to the current session/command (commit 722489a); review found the durable records were only partially compared rather than decoded through the runtime domain models.
Task 8: fix round 3/5 reused the Python Term repository's fail-closed aggregate verifier, validated full ToolEffectRecord/StepRecord/TermRecord schemas, mirror columns, identity digests and replay evidence, and replaced synthetic fixtures with repository-produced records (commit 9fa7373).
Task 8: complete (commits 1807676..9fa7373, independent review clean; atomic restart replay, legacy recovery, cross-Term rejection and zero-write corruption cases verified).
Task 7: complete after Task 8 closed the reserved/no-response crash window. The deterministic development gate is eligible to pass on the fixed revision; production GO still requires a valid external CI/KMS signature and is not asserted by repository-local evidence.
