# Final scoped A/E review fixes

Base: `b96a791`. This pass addresses exactly the five final-review findings.
No model request was made. No third-party source, old Vault/session, running
3080 instance, worker 4's 64356 service, or the preexisting four README lines
were changed. All fixtures remain on disk.

## 1. Native durability before projection

The native `SessionStore.flush()` dispatches its listeners concurrently via
`Promise.allSettled`; ordering the plugin listener after the JSONL listener did
not establish durability. The previous synchronous memory flush could commit a
source/cursor before `appendBatch` failed.

The plugin now has one asynchronous drain per session. Its flush listener waits
for that drain. The drain calls the public native `sessions.flush()` within an
`AsyncLocalStorage` marker; only this plugin's recursively invoked observer is
skipped, while all native persistence/other observers still run and are awaited.
This avoids listener-order assumptions, avoids replacing native methods, and
uses no private persistence API in production. Concurrent external flush calls
join the same drain. Failure becomes sticky in `status.error` and every later
barrier; no memory ingestion runs before the native checkpoint succeeds.

After the checkpoint, native `sessionPersistence.readFrom()` supplies the actual
stored prefix/tail without cold recovery. The checkpoint length and the live
immutable event identity must agree. At first drain after reopening, every
already-confirmed source is checked in bounded batches before fresh tail
ingestion; a missing, shortened or changed prefix raises
`MEMORY_SOURCE_CONFLICT`. Nothing is deleted, reset, repaired, or silently
replaced in the memory database on conflict.

### Additive schema v2

`memory_native_sources` stores the full original native event's canonical hash
for newly ingested events, transactionally with the existing projected source
and cursor. This also protects the full native body of a new metadata-only
derived context projection. Existing `memory_event_sources` hashes are unchanged.

For v1 rows with no native hash, reopening verifies the deterministic original
projection against the existing source hash; it does not infer or re-sign a new
native hash from the current log. Consequently historical v1 metadata-only
derived pages retain their original metadata-level identity guarantee; v2 does
not retroactively claim a full-body signature that never existed. Ordinary v1
events/tool IO remain checked against their original full projected content.
The migration test confirms no native hash is added on legacy verification or
duplicate ingestion, and altered legacy content is refused. New native hash
verification catches a changed full body even when its domain metadata is equal.
EffectStore's regression now checks that constructing EffectStore leaves the
MemoryStore-owned schema version unchanged instead of assuming version 1.

## 2. Long streams and bounded draining

The observer no longer duplicates every native payload into a 512-entry queue.
It retains a constant-size latest-sequence/byte-count range and schedules one
drain, not one pending promise per event. The native log remains the payload
owner. Projection batches are bounded to 64 events and 8 MiB and yield between
batches; new stream arrivals extend the range and are drained automatically.
Pre-step/tool/explicit native checkpoints await completion and provide
backpressure. A single event over 8 MiB remains a visible sticky overflow, and
real persistence failures still block later dispatch. The native JSONL reader
retains its documented sequential read behavior; this is not a claim to change
native log storage or make the native in-memory session itself bounded.

## 3. Search before limiting

`MemoryStore.search()` applies project/private visibility, latest version, and
text match in SQLite before ordering and limiting to at most 100 handles.
Ranking preserves the earlier summary/content preference. Only result metadata
is returned to JavaScript, not all historical records or matched full bodies.
The plugin excludes self-derived context before limiting. A semantic fact older
than 1001 unrelated records is now found.

## 4. Protected rules cannot disappear behind unrelated records

`MemoryStore.protectedRules()` iterates the complete scoped latest confirmed
protected rule set after filtering, rather than filtering an already-truncated
1000-record list. Visible protected rules are conservatively mandatory; no new
applicability language was introduced. Accumulated rules are bounded by the
configured context budget, and excess raises `MEMORY_BUDGET_EXCEEDED` rather
than omitting a rule. The pager still checks combined anchors/prefix overhead.

## 5. Version-fixed body continuations across all layers

`MemoryStore.readPage`, pager, service, native tool, HTTP route and browser now
support `offset` (default 0), `maxChars` (1–100000), and `version`.
Nonzero offset requires an explicit version. Offsets count UTF-16 units of the
canonical JSON text. Results include `offset`, `maxChars`, `totalChars`,
`nextOffset` (null at end), `hasMore`, `offsetUnit: 'utf16'`, and `truncated`.
Store queries return the bounded range rather than the full body. Offset beyond
that fixed version's end is rejected. A continuation can be repeated after
process restart and after a newer record version has been created.

`memory_page_in` returns range metadata and selects that exact body range for the
next native logged surface replacement; the page-selection event records its
offset/range. Repeated identical steps do not grow duplicate pages. The browser
exposes an offset input and a next-page button which carries the exact returned
version. Old calls omitting offset retain the initial-page behavior.

API note: `ctx.memoryRecovery.flush`, `search`, and `pageIn` are asynchronous
durability boundaries; callers must await them. Native tools and the HTTP handler
were updated accordingly. Page-in still does not execute a model itself.

## RED → GREEN evidence

- Native append failure: RED advanced cursor 1 instead of 0; GREEN retains
  cursor 0, native log length 1, absent failed record, sticky status and failed
  later barrier. Only the actual backend `appendBatch` failure is injected;
  native Context, SessionStore, coordinator, JSONL storage and plugin are real.
- 1200 native stream events: RED hit the 512-entry overflow; GREEN drains all
  1201 total events (including config), pending 0, with three simultaneous
  external/service flushes and no recursion/deadlock.
- Cold false-durability prefix: RED barrier incorrectly succeeded; GREEN
  refuses the conflicting seq while retaining the prior memory content.
- Old semantic retrieval: RED returned no handle after 1001 noise records;
  GREEN returns the old matching handle with no body.
- Old protected invariant: RED omitted it after 1001 normal confirmed rules;
  GREEN retains it and separately refuses an oversized mandatory rule set.
- Tail continuation: RED lacked total/next-offset; GREEN reads beyond character
  100000 and repeats the identical historical-version tail after store reopen.
- Native tool/HTTP: RED rejected offset as an unknown argument; GREEN admits
  the bounded tail. The true native model surface contains `MODEL-TAIL-ONLY`,
  not just the source prefix, and a repeated projection does not grow the log.
- Browser: fixed-version next page advances offset to 20 and keeps version 1.

Final verification:

```text
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test apps/dsh-agent/tests/*.test.mjs
110 tests, 110 passed, 0 failed (13.7 seconds)

TSX_TSCONFIG_PATH=<worktree>/third_party/deepseek-harness/tsconfig.json node --import ./third_party/deepseek-harness/node_modules/tsx/dist/esm/index.mjs --test --test-name-pattern='native append failure|long native streams|cold recovery refuses|native page-in tool' apps/dsh-agent/tests/memory-runtime.test.mjs
4 tests, 4 passed, 0 failed

node apps/dsh-agent/tests/fixtures/memory-reopen.mjs /Users/sushi/dsh-memory-live-c5RjwQ
exit 0; reopened true; config version 1; COMMITTED; artifact unchanged;
semantic version 1; Vault locked; newModelRequests 0
```

The original successful fixture's complete API/browser restart passed at
`http://127.0.0.1:61252`; the script stopped only its own temporary process.
`reopen-report.json` and both restart screenshots remain in that fixture.
Latest full-suite browser evidence is retained at
`/Users/sushi/dsh-memory-web-IwFMnW`; native scoped tail/stream/source tests also
run under the actual tsx source-alias loader. This pass verifies native component
and replay behavior, not an additional cloud/local model run.

Only the scoped source/UI/test files and this report are committed. README and
unrelated untracked analysis/tuning documents remain outside the commit.
