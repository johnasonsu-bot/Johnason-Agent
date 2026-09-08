# Task 3 final fix — distinguish sanitized Vault failures

Base: `5be6030`. Scope: only `apps/dsh-agent/src/vault-ui.mjs`, `tests/vault-ui.test.mjs`, and this report. The existing Chinese page already displays the `error` field, so no HTML change was needed.

## Fix

The handler now maps a whitelist of VaultStore/VaultLock codes to fixed Chinese messages and returns a stable `code` field. All error HTTP statuses remain 400, preserving the controller's existing negative-probe expectations.

- Authentication failure retains both possible causes: wrong password or compromised file integrity. It does not falsely distinguish wrong password from valid-envelope GCM tampering.
- Invalid/unsupported format, busy operation, locked state, existing vault, absent initialization, decryption authentication failure, and lost lock ownership each receive different actionable feedback.
- Authentication, format, and generic failure messages explicitly state that the application will not automatically delete or reset the vault.
- Unknown codes become `VAULT_OPERATION_FAILED`; messages, causes, paths, request contents, and unknown code text never enter the response. `Object.hasOwn` prevents inherited keys such as `constructor` from escaping the whitelist.

## RED → GREEN

Tests first exercised real HTTP requests with actual VaultStore wrong-password and invalid-format failures, plus a narrow service-error boundary table for all eight known storage/lock codes and two unknown/prototype-shaped codes containing secret markers.

```text
cd apps/dsh-agent
node --test tests/vault-ui.test.mjs
RED: tests 4, pass 1, fail 3; duration_ms 282.330625
Failures: identical generic message and missing diagnostic code for authentication, known errors, and format damage.
```

After the controller requested Chinese messages, the revised message expectations were also observed RED (4 tests, 1 pass, 3 fail) before production messages were translated. Final focused verification:

```text
node --test tests/vault-ui.test.mjs
tests 4, pass 4, fail 0
duration_ms 331.886625
```

The final production change was followed by one whole thin-layer run:

```text
npm test
tests 34, pass 34, fail 0
duration_ms 12436.795792

git diff --check
exit 0
```

That run includes the real native Web startup/exit, local MCP/Skill/restart, plugin CLI, occupied port, and Chrome reload tests already owned by the controller. Browser evidence: temporary directory `dsh-browser-refresh-ZlE9Q7`, Chromium `152.0.7977.76`; no model calls were made. An earlier whole run before the subsequently requested Chinese wording also passed 34/34; it is not used as final-change verification.

No dependencies were installed, no upstream files changed, no broad security scan was run, and neither 3080 nor 3188 was stopped or restarted. Their already-loaded modules remain unchanged until the controller's authorized restart. The controller-owned 14-case negative probe was left intact for its controlled rerun.
