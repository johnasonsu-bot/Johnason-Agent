# Forge RBAC and Unified DSH Authorization Implementation Plan

User approved full Forge accounts/passwords/RBAC and generic DSH integration, plus login tests for both platforms.

Architecture: Forge AuthCore wraps shared Store with mandatory identity checks, persisted hashed passwords and hashed expiring session grants, role action permissions enforced on every request. HTTP browser/CLI/MCP and local Python JSON bridge share AuthCore. DSH provider registry reuses native authorization/Vault and invokes registered cores directly; no DSH-to-platform HTTP. Existing dataplatform_execute remains compatible.

Constraints: No real secrets in code/config/files. No file deletions. No destructive business mutations. Test accounts/passwords generated only in memory in isolated DBs. Production administrator is initialized by a masked local terminal prompt, never a default credential. Existing Forge business data must be backed up before starting upgraded service. No anonymous legacy fallback.

## Task 1: Forge identity and shared enforcement
- Create auth.py AuthCore(db_path): bootstrap(username,password), login(username,password)->{token,user}, profile(token)->user, logout(token), execute(token,command,input)->JSON.
- Users and roles support list/create/update/user.role.set and permission.catalog; admin/editor/viewer roles. Granular action allowlists, default deny. Prevent last active admin disable/demotion. Password change/disable and role changes reflected immediately. Password hashes use stdlib scrypt and session tokens use SHA256 storage/expiry.
- Add local bridge.py: one stdin JSON {operation,username,password,token,command,input}; output {ok:true,value} or {ok:false,error,status}. Startup fixed --db argument. bootstrap is local interactive CLI only, never exposed through DSH bridge/HTTP.
- Protect server.py /api/state, /api/call, imports using AuthCore; session cookie HttpOnly SameSite=Strict plus Bearer CLI. auth status/login/profile/logout endpoints. No auth bypass via actor. Public health/static/login only.
- Write isolated auth tests first: missing session, bad password, disabled user, viewer write deny, editor action deny, revocation, last admin protection, persistence, bridge.

## Task 2: Forge browser/CLI/MCP
- Add login/account & roles administration browser UI; clear success/error states. UI uses same-origin cookie, never localStorage password/token. Bootstrapping required state prompts local masked setup.
- Backend accepts token via environment/in-memory only, no token files; CLI auth login uses hidden password and in-process authenticated REPL, supports profile/logout/admin operations; MCP inherits env grant and no passwords in model tools.
- Tests browser/CLI use isolated server; legacy tests updated to authenticate, no auth-disabled test fallback.

## Task 3: DSH provider registry and generic UI
- Add fixed host JSON provider config --systems; each {id,label,url,type,corePath} or {id,label,url,type:forge,python,sourceRoot,dbPath}. Secrets prohibited, unknown/duplicate IDs rejected.
- Generic native service commands/labels with system-bound Vault grant; Forge Python subprocess fixed argv, timeout/size limits, JSON-only stdout. Reject model credential fields.
- /systems shows providers, URL, username/password, session status/logout, commands and safe manual user-password input; /dataplatform remains compatible. system_execute takes systemId,command,input/projectId; fixed command allowlists.
- Tests wrong provider/url, unauthorized, real DP fixture login, real Forge AuthCore login/revocation/RBAC and no secret leakage.

## Task 4: Integrate and verify
- Run Python auth/server/backend and Node/UI regression tests; repair failures.
- Independent review for auth bypass, role mutation and credentials exposure; fix findings.
- Backup live Forge SQLite before migration; start upgraded service without default account. Restart DSH configured both providers, verify health/UI. Run full login tests on isolated fixtures, not existing unknown account passwords.
- Deliver reproduction instructions and exact limits of live/manual verification.

## Execution ledger
- Tasks 1–3 implemented. Task 4 automated tests/review/deployment complete; production first administrator requires user's own hidden password input.
- Ruling: action-level Forge RBAC, no project restriction claim; full multi-entry identity enforcement is mandatory.
- Ruling: do not replace old editable environment or mutate original Forge checkout; run isolated worktree via PYTHONPATH and provide wrapper scripts/Wheel.
- Fixed review findings: virtualenv interpreter resolution, Unicode stream boundaries, subprocess teardown, reserved provider ID, browser password residue, stale-response identity races.
- Production DB backup and per-table unchanged verification complete. See docs/testing/SYSTEM-AUTH-VERIFICATION.md.
