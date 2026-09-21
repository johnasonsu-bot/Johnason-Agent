# DSH upstream code merge

## Authorized scope

Merge the official master changes through `ddefc45fbc7f8e46dd73185e68295696d1297887` into a separate candidate while preserving every Johnason feature. The user explicitly requested code merging only on 2026-09-20: no dependency installation, native/sidecar build, generated-file cleanup, or service restart in this stage.

The candidate starts from `6a40943e867b59401cb73fd9d5a44d1329798c2b`, including the system authorization and Forge integration that was absent from the original main checkout. The upstream submodule had no custom edits. Original worktrees and user data remain untouched.

## Required behavior

- Retain encrypted credentials and allowlisted process environments.
- Retain custom headless profiles, plugin management, native Web and browser integrations.
- Retain memory activation eligibility, persistence barriers, audit and recovery semantics; do not make required recovery events ignorable.
- Retain Data Platform and generic system/Forge authorization, tools and UI.
- Retain Workbench DSH streaming, history, tool calls and cancellation with the new upstream APIs.
- Keep source verification strict. Generated receipts/manifests remain pending until an authorized build; do not fabricate build attestation.

## Implementation and ownership

- [x] Establish isolated candidate and merge the complete `6a40943` user feature commit.
- [x] Materialize official target directly in the empty candidate submodule without removing files from the original checkout.
- [x] Launcher task: adapt async plugin exit, native version reporting and nonduplicated profile arguments; preserve system provider configuration and encrypted profile composition. Add focused source tests.
- [x] Memory task: replace removed API proxy and persistence read API, preserve first-use and recovery rules, adapt source tests to the new interfaces. Correct initial system-head ordering and replacement endpoints.
- [x] Workbench task: update source pin/build policy input paths and native-session streaming/adapter/session composition; adapt focused unit fixtures. Preserve a valid system head in history seeds. Leave dist/receipts untouched.
- [x] Coordinator: update native build pin and maintenance documentation, run available source-only checks, inspect the combined diff, obtain an independent review, and commit the candidate.

### Integration finding: browser authentication before Vault unlock

The new upstream Connection initializes `client-connection/browser-session` with `credentials.modifyRecord()` before Web startup. The locked Vault provider would reject that bootstrap. Preserve encrypted API keys/system grants while storing only this exact native browser-signing record in process memory, scoped to the Cordis root and serialized across concurrent plugin activations. Never persist or log the signing secret. Native browser cookies require the fresh launch URL after a process restart. Cover root isolation, concurrent initialization and rejection of non-signing records with source-only tests; preserve a native provider test for the build stage.

## Verification boundaries

Source-only Node and Python tests may run when they do not build/install or delete files. Upstream/native tests requiring newly built packages, browser integration, sidecar generated artifacts, source-manifest regeneration and complete preservation verification are deferred by user instruction. Record exact commands and outcomes in the integration report, including any unverified behavior.

Final source check: 134 selected tests passed. The upstream historical migration validator is confirmed to refuse custom memory events in v0 logs; this is an activation blocker requiring a separate migration design, not an issue that a rebuild alone resolves. See `docs/testing/2026-09-21-dsh-upstream-code-merge.md` for evidence and deferred verification.
