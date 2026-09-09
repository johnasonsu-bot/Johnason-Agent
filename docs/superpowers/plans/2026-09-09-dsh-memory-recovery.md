# DSH Memory Recovery Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 按任务实现并审查，用户已授权设计后直接开发，不重复询问执行方式。

**Goal:** 在独立 DSH 分支交付三类持久化记忆、流式分页、双时态提交恢复与按任务启动的原生 OS 沙箱。
**Architecture:** 原生 DSH 不改 Loop，新增 Cordis 插件和本地 SQLite 领域服务；原生日志保持会话权威。先完成纯业务组件，再接原生生命周期、工具和 UI。
**Tech Stack:** ESM JavaScript、Node node:sqlite、node:test、DSH Cordis/native sandbox、原生 Web 附加页面。
**Spec:** docs/superpowers/specs/2026-09-09-dsh-memory-recovery-design.md

## Global Constraints

- 只改 apps/dsh-agent 与本任务文档，不改 third_party、旧工作区及原有未提交内容；不删除文件、目录或分支。
- Node ^22.19.0 或 >=24.0.0；测试使用 /Users/sushi/.nvm/versions/node/v22.20.0/bin/node；node:sqlite 的实验性提示记录为已知提示。
- 不记录密钥/密码，不读取旧 Vault；不启动历史任务；真实模型仅新测试，未测试不能标通过。
- 用 apply_patch 编辑；TDD 先 RED 后 GREEN；独立实现者不再派发子 Agent；仅提交自己负责的明确文件。
- 保留 DSH session 为会话事实源；领域索引不能代替聊天历史。系统规程不可由模型覆盖；不支持沙箱要求必须阻断。
- 不新增 B/C/D 功能，不构建独立审核 Agent 或四级门控；不得以通用 shell 日志冒充真正 2PC。

## Task 1: 三类持久化记忆与分页服务

**Files:** Create src/memory-store.mjs, src/memory-pager.mjs, src/record-sanitizer.mjs, tests/memory-store.test.mjs, tests/memory-pager.test.mjs under apps/dsh-agent.

**Interfaces:** `new MemoryStore(path)`; `.close()`; `.ingestEvents({projectId,sessionId,agentId,events})`; `.putMemory({id?,kind,projectId,sessionId,agentId,visibility,content,summary,sourceRefs,validTime?,expectedVersion?,status?},{actor})`; `.list(scope,filters)`; `.get(scope,id,version?)`; `.graph(scope,{validAt?,systemAt?})`; `.cursor(sessionId)`; `.db` for Task 2 transaction tables (database owned/closed only by MemoryStore). `new MemoryPager(store)` with `.search(scope,query,{limit})`, `.pageIn(scope,id,{version?,maxChars})`, `.pageOut(scope,id)`, `.select(scope,{query,budgetTokens,anchors})` returns `{pages,estimatedTokens,contextText}`. Scope is `{projectId,sessionId,agentId}`. Results JSON serializable. Memory kind exactly episodic/semantic/procedural; semantic content nodes/edges; procedural content includes rules/applicability, status candidate/confirmed, protected system rules actor=operator only. Page selection always respects visibility and returns handles by default, explicit pageIn bounded content.

- [x] RED: create real temporary SQLite tests for restart, source duplicate, cursor atomicity, conflicting revision, three categories, semantic edges/version as-of, private scope, model cannot confirm/overwrite protected rule, paging source preservation and budget. Example:
```js
const store = new MemoryStore(join(dir, 'memory.sqlite'));
store.ingestEvents({projectId:'p',sessionId:'s',agentId:'a',events:[{seq:0,type:'tool/result',data:{value:'needle'},time:1}]});
assert.equal(store.cursor('s'),0);
assert.equal(store.list({projectId:'p',sessionId:'s',agentId:'a'},{kind:'episodic'}).length,1);
```
- [x] Run `node --test apps/dsh-agent/tests/memory-store.test.mjs apps/dsh-agent/tests/memory-pager.test.mjs`, record missing behavior failures.
- [x] GREEN: implement SQLite WAL/FULL transactions, additive schema/version check, durable unique sources, deep-detached records, source-bound revision graph, bounded queries. Validate whole batch before advancing cursor, handle sequence gaps explicitly. Sanitizer filters credential fields/events and recognizable secrets, preserves ordinary data shape; record redaction not silent content invention. Streams/chunks ingested as domain projections, not second conversation store. Bound limits and negative/empty/invalid inputs.
- [x] Run focused suite, self-review, commit only Task 1 files; report API details and RED/GREEN output for next task.

## Task 2: Effect 双时态状态机与原生任务沙箱

**Files:** Create src/effect-store.mjs, src/task-sandbox.mjs, tests/effect-store.test.mjs, tests/task-sandbox.test.mjs.

**Interfaces:** `new EffectStore(memoryStore)` uses `.db`, methods `.prepare(input)`, `.begin(effectId,owner)`, `.finish(effectId,owner,{state,result,evidence})`, `.recover(scope)`, `.list(scope,{validAt?,systemAt?})`, `.graph(scope,filters)`, `.reconcile(effectId,proof)`. TaskSandbox exports `validateSandboxRequirements(requirements)` and `runSandboxTask({sandbox,argv,requirements,signal})`; sandbox is actual native provider `.confine(argv,{mode,workspaceRoot})`; returns exitCode/stdout/stderr/backend/enforcement. Caller owns effect transaction. Network host supported native, network none/CPU/memory/container requirements rejected unsupported, no misleading fallback.

- [x] RED: actual SQLite effect tests PREPARED→EXECUTING→COMMITTED, conflicting payload, owner CAS, restart unknown, historical valid/system query and causal edges; no auto retry. Actual child side-effect counter must remain one under duplicate admission.
```js
const prepared = effects.prepare(input);
effects.begin(prepared.id, 'owner-a');
assert.throws(() => effects.begin(prepared.id, 'owner-b'));
```
- [x] RED: real native sandbox test in temporary directories, allow workspace write and deny sibling protected write, missing runner failure, cancellation, unsupported network. Do not clean temp files without user confirmation; record temp paths.
- [x] GREEN: SQL transactions and append-only effect events, immutable identity binding, scope checks and deterministic canonical fingerprints. True participant protocol distinct from reservation-execution-confirmation. Implement awaited native confined process with timeout/cancel, bounded outputs, no shell interpolation of argv, canonical workspace and explicit requirement validation. Consult packages/sandbox/sandbox and sandbox-local READMEs, not private runner implementation.
- [x] Run targeted tests; report actual backend evidence/unsupported capabilities, commit only Task 2 files.

## Task 3: DSH 生命周期工具分页与沙箱集成

**Files:** Create src/memory-recovery-plugin.mjs, src/memory-tools.mjs; Modify src/profile.mjs; Create tests/memory-runtime.test.mjs; extend tests/profile.test.mjs only as needed.

**Interfaces:** consume Task 1/2 services. Plugin export `name`, `inject`, `apply(ctx,config)`; config path to database and enablement only, no secrets. Expose domain services for Task 4 through `ctx.memoryRecovery` or explicit install callback, documented in report. Per-session settings hold project/sandbox policy version and enablement; existing sessions disabled until explicit configured. Same API used by UI and tools.

- [x] RED: use real Cordis/native session/tool components to show profile loads plugin, event stream flush ingests, per-session scopes isolate, memory tools invoke correct services, page selection enters real logged context, repeated steps do not grow duplicate page blocks. Test sandbox required tool path cannot run unconfined through native Bash/file write; malformed requirements fail before dispatch.
- [x] GREEN: register bounded event queue and awaited flush/pre-step barriers; capture source events without recursive self-ingestion; expose memory_search/page_in/page_out/semantic/procedural-candidate through native tool registration. Native event types and returned user-message source metadata must match upstream contract. Inspect source rather than guessing API. Replace only owned model-facing page projections with cited source events, preserving native turn/tool pairing. Never let returned contexts bypass budget/scope.
- [x] GREEN: wrap supported execution paths with effect prepare/begin/finish and task sandbox requirement validation, preserve native tool authorization and result shape. For unsupported tools under required isolation reject explicitly; unknown writes not auto-retried. Native read-only/write policy cannot be silently loosened by task or model. Record unknown outcomes across interrupted sessions without restart side effects.
- [x] Run integration plus profile regression; resolve missing real API assumptions before commit. Commit only Task 3 files.

## Task 4: 本地页面交付与真实验收

**Files:** Create src/memory-recovery-ui.mjs, public/memory-recovery.html, tests/memory-recovery-ui.test.mjs; modify src/memory-recovery-plugin.mjs for registration; update apps/dsh-agent/README.md preserving existing edits; create docs/testing/2026-09-09-dsh-memory-recovery-acceptance.md.

- [x] RED: real HTTP tests scoped read/write/configuration, semantic graph revisions, procedural confirmation, paging, effect as-of and unknown states; stale version and cross-project access failures, local same-origin restriction. No arbitrary execution route.
- [x] GREEN: native web link /memory-recovery, select session and explicit project/workspace, enable memory and declare supported sandbox requirements. Chinese forms show three memory categories, sources/versions/pages, effects/causal edges/valid-system time, errors and raw-vs-summary distinction. Do not create another chat window. Normal UI actions do not delete history or execute models.
- [x] Run all app tests and real native Web profile in a new temporary data directory/port (not the user's running 3080). Test real sandbox tool invocation and persist/reopen. If real local API is reachable and allowed, run one bounded NEW session, never existing tasks; preserve credentials in Vault, no key extraction. If model unavailable record pending, not success.
- [x] Create acceptance report with exact commands, results, supported sandbox isolation and limits, remaining manual steps. Update README build/start and new page path. Commit only new changes; do not commit pre-existing README changes accidentally, coordinate controller for overlapping hunks.

## Review and delivery

- Each task gets independent spec+quality review, covering findings fixed by original implementer.
- Final review spans these four tasks, not old P0 or unrelated history.
- No automatic push/merge/service replacement; preserve ledger and test output. Final response reports design path, tested functionality, actual sandbox/model status and any unverified acceptance.
