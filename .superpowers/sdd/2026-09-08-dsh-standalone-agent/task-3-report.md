# Task 3 — Encrypted provider, local Vault UI, native profile/CLI

## Result

Implemented the encrypted-only native CredentialProvider, same-origin local master-password UI, complete native base/mode composition, and isolated source-native CLI/profile/plugin dispatch. No upstream files were changed. No old platform, Host, Vault, credentials, or history were migrated. All test writes use newly created temporary directories; no user file was deleted.

## Files

- `apps/dsh-agent/src/credentials-plugin.mjs`: all nine native reference/record methods, JSON record validation, native contained notification dispatch, fresh vault reads per operation, hidden-input terminal unlock.
- `apps/dsh-agent/src/vault-ui.mjs`, `public/vault.html`: GET status/page and POST initialize/unlock/lock, two-password initialization, cleared fields, loopback/Host/Origin/fetch-site checks, JSON-only writes, 16 KiB request limit, no input reflection.
- `apps/dsh-agent/src/profile.mjs`: generated encrypted-base bundle, exact native base preservation except credentials identity, native user/mode layers, final encrypted overlay, pre-execution provider-count check.
- `apps/dsh-agent/src/native-runner.mjs`, `src/cli.mjs`, `src/launch-config.mjs`: allowlisted child environment, explicit native TypeScript resolver, native argument parsing/profile boot/plugin forwarding, no native bin dotenv loader, signal propagation and joined child exit.
- README/package scripts and directed service/profile/CLI/real-Web tests.

## Verified integration corrections

1. This pin's `vendor/include/src/index.ts:116` treats `patch.name` as an identity guard, not a replacement. The initially proposed final name patch was demonstrably skipped. Following the controller's approved ruling, the adapter generates `@johnason/dsh-encrypted-base` from the entire native base YAML and replaces only its one credentials plugin name. A structural equality test compares every other base row. The original base slot in the standalone profile manifest is changed to the generated bundle; other bundle entries and user patches remain intact. The final overlay supplies encrypted path/mode and composition validation rejects extra known credential providers.
2. `@deepseek-ai/dsh-credentials` is not a direct dependency of the CLI package. It is resolved from the same pinned dependency closure via CLI → base → credentials-local → credentials, using `createRequire` anchors. The local provider implementation is never imported or mounted.
3. Source `runProfile` requires the upstream tsconfig paths when cwd is outside the upstream checkout. Without that explicit configuration, source `FiberState` imports failed against built const-enum output. The launcher now supplies its own fixed `TSX_TSCONFIG_PATH`; caller-provided Node/tsx environment settings are not inherited.
4. Native `parseDshArgs`, `runProfile`, `runPlugin`, and config dump dispatch are reused directly. No `loadLayeredEnv` call occurs. Native profile boot retains install-anchor resolution, shipped agent-preset roots, live user patches, native lifecycle, native bundles, and all browser contributions.
5. Same-origin integration uses `ctx.webServer.register({kind:'exact',path,handler})` and `tapIndex`; each disposer is registered through `ctx.effect`. An optional `ctx.inject(['webServer'], ...)` branch keeps headless independent of Web. The native React application remains the main page.

## RED / GREEN evidence

Initial service/UI tests before implementation:

```text
node --test tests/credentials-plugin.test.mjs tests/vault-ui.test.mjs
ERR_MODULE_NOT_FOUND: credentials-plugin.mjs / vault-ui.mjs
tests 2, pass 0, fail 2
```

Initial profile test before implementation:

```text
node --test tests/profile.test.mjs
ERR_MODULE_NOT_FOUND: profile.mjs
tests 1, pass 0, fail 1
```

Native flag regressions demonstrated `Unknown option: --profile`, then `Unknown option: --patch`; both passed after preserving the respective native flags. A further RED assertion caught Web's patch arguments being placed after app flags, which native pass-through parsing would not treat as launcher overlays; the final argument-order fix passed.

Whole standalone app suite after implementation:

```text
cd apps/dsh-agent && npm test
tests 26, pass 26, fail 0
duration_ms 6172.037
```

Final affected-path checks after the last argument-order and description refactors:

```text
node --test apps/dsh-agent/tests/launch-config.test.mjs apps/dsh-agent/tests/native-web.test.mjs
tests 11, pass 11, fail 0
duration_ms 4709.601375

cd apps/dsh-agent
node --test tests/credentials-plugin.test.mjs tests/profile.test.mjs tests/vault-ui.test.mjs
tests 3, pass 3, fail 0
duration_ms 255.523083

git diff --check
exit 0
```

The pre-existing nvm/npmrc advisory appears in shell output. The build test intentionally prints its unpinned-checkout refusal fixture; that is a passing negative test, not the real checkout state. No full upstream rebuild or repository-wide security scan was repeated.

## Actual native startup and UI evidence

- Started `node src/cli.mjs web --data-dir /tmp/dsh-native-web-2HKAPL --port 3188 --no-open` from the adapter directory. Native stdout: `dsh web: http://127.0.0.1:3188`.
- Initial GET `/vault/status`: `200 {"initialized":false,"locked":true}`. Original root returned native HTML including `__ModuleLoader__`, not a replacement chat UI. Vault page returned 200.
- Initialized the temporary vault through its same-origin API with a fictitious test password: `200 {"initialized":true,"locked":false}`. No password or key was written to a plaintext file.
- Controller's visible-browser check reached original Models and saved an LM Studio local test route/model; Models showed the credential configured. Original native directory picker launched `osascript`; subsequent user selection appeared as a workspace in the original UI. No model task was run in that user-selected real directory.
- At controller request, registered only `/tmp/johnason-dsh-acceptance-workspace-oBHBD6` through public native POST `/api/workspace.create`; result was 200/ok/created, workspace ID `8d50001b-289e-4582-b9cc-c2dc6cfed3c5`. No internal state file was hand-edited.
- The separate automated native-Web smoke uses a fresh temporary dataRoot and a fake `.env`. It verifies native HTML, the Vault link/page/status, no `.credentials.yaml`, no secret in process output, graceful SIGTERM exit `{code:0,signal:null}`, and a closed listening port. It passed against the final launcher.
- Real TTY headless test used `/tmp/dsh-tty-unlock-UvEvOB`: showed `Create vault master password:` then `Confirm master password:`; both supplied fictitious inputs were absent from PTY output. After initialization native execution reached `MISSING_CREDENTIAL`, as expected for an empty vault; exit 1. The missing-key text belongs to upstream and mentions environment export, which this encrypted launcher intentionally does not support.
- Real non-TTY headless regression returns visible `/vault`/interactive-terminal unlock instructions rather than hanging. Real native plugin command `plugin --profile plugin-smoke list --depth 0` initialized only its temporary profile and exited 0.

## Running process handoff and remaining validation

At controller request, the manual 3188 process is intentionally still running for broader model acceptance: launcher PID 42476, native PID 42834, exec session 56658, dataRoot `/tmp/dsh-native-web-2HKAPL`. Do not stop it until the controller's current model/UI check finishes. The separately owned automated Web smoke fully stopped and verified exit.

Controller's current Qwen task `3957ab55-f789-4d6b-9a72-d3112b636844` reached native `request/header` and `request/context` with the configured local route. Native PID 42834 had an established connection to `127.0.0.1:1234`; no error or assistant/tool output had appeared when inspected. Real model task completion/performance remains controller-owned and is not claimed by this Task 3 report. No MCP external account or optional TUI installation is claimed tested.

Third-party plugins and user composition remain trusted local code, as in native DSH. The encrypted store prevents ambient credential fallback through the credentials seam; it does not sandbox arbitrary user-installed code that the user authorizes. JavaScript strings cannot be reliably zeroized, although terminal/page references are dropped promptly and VaultStore clears key buffers.
