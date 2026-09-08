# Task 2 Report: Encrypted VaultStore

## Result

Implemented the standalone encrypted credential store in:

- `apps/dsh-agent/src/vault-store.mjs`
- `apps/dsh-agent/src/vault-lock.mjs`
- `apps/dsh-agent/tests/vault-store.test.mjs`

## RED / GREEN evidence

RED, before production files existed:

```text
node --test tests/vault-store.test.mjs
ERR_MODULE_NOT_FOUND: Cannot find module .../src/vault-store.mjs
tests 1, pass 0, fail 1
```

GREEN, directed VaultStore suite:

```text
node --test tests/vault-store.test.mjs
tests 6, pass 6, fail 0
```

GREEN, complete `apps/dsh-agent` suite after implementation:

```text
npm test
tests 18, pass 18, fail 0
```

The environment prints an existing nvm/npmrc compatibility advisory before commands; it does not affect the test exit status.

## API semantics

- `new VaultStore(path)` is synchronous and retains only the vault path until unlocked.
- `status()` is asynchronous and returns `{ initialized, locked }`. `initialized` reflects whether the vault path is currently a file; `locked` reflects whether this instance has a derived key in memory.
- `initialize(password)` is asynchronous. It creates the parent directory if needed, takes the cross-process vault lock, refuses an existing vault with `VAULT_EXISTS`, writes a new empty encrypted envelope, and leaves this instance unlocked only after commit succeeds.
- `unlock(password)` is asynchronous. It validates the fixed envelope format, derives a candidate key with scrypt, authenticates and parses the encrypted data, then replaces the instance key. Authentication failure is `VAULT_UNLOCK_FAILED`; it deliberately does not claim to distinguish a wrong password from authenticated-ciphertext tampering.
- `lock()` is synchronous. It zero-fills the in-memory derived key and invalidates current operations. It is idempotent.
- `read()` is asynchronous and returns the decrypted `{ refs, records }`. A locked instance rejects with `VAULT_LOCKED`.
- `update(mutator)` is asynchronous. It takes an atomic cross-process `mkdir` lock, rereads/decrypts the current disk state inside that lock, awaits the mutator, validates the result, and atomically commits through an exclusively created same-directory temporary file and rename.
- If `lock()` occurs while an `update()` mutator is in flight, the update detects the generation/key change before commit, rejects with `VAULT_LOCKED`, and does not persist the mutation.

## Error and locking boundaries

- `VAULT_LOCKED`: instance has no active key, including an update invalidated by `lock()`.
- `VAULT_NOT_INITIALIZED`: vault file is absent when reading/unlocking.
- `VAULT_EXISTS`: initialization refuses to overwrite an existing path.
- `VAULT_FORMAT_ERROR`: JSON/envelope fields, encoded field sizes, KDF parameters, or decrypted data shape are invalid.
- `VAULT_UNLOCK_FAILED`: AES-GCM authentication failed during unlock.
- `VAULT_DECRYPT_FAILED`: an already-unlocked instance cannot authenticate subsequently read disk content.
- `VAULT_BUSY`: the bounded lock wait elapsed. Lock directories are never treated as stale or stolen.
- Lock release verifies its random ownership token and removes only the owner file and directory created by that acquisition.

Passwords are copied into temporary byte buffers for derivation and those buffers are zero-filled. Passwords and derived keys are never persisted. No environment or `.env` credential fallback exists.

## Review round 1

Three regression tests were added first and observed failing:

```text
node --test tests/vault-store.test.mjs
tests 9, pass 6, fail 3
```

The failures demonstrated that a non-string `ciphertext` leaked `ERR_INVALID_ARG_TYPE`, an in-flight `unlock()` ignored a subsequent `lock()`, and an update could still rename its prepared temporary file after `lock()`.

After the fixes, the directed result is:

```text
node --test tests/vault-store.test.mjs
tests 9, pass 9, fail 0
```

`initialize()` and `unlock()` now capture the instance generation synchronously at invocation. Any `lock()` before they install a candidate key invalidates that operation; the candidate key is zero-filled and the operation rejects with `VAULT_LOCKED`. Initialization also checks the generation before doing work and before its atomic commit.

Atomic commit has an explicit linearization boundary: after the exclusive temporary file has been fully written, synced, and closed, the generation is checked immediately before `rename`. A `lock()` observed by that check prevents publication and removes only the owned temporary file. Once `rename` has been issued, the commit is linearized and cannot be retroactively withdrawn; a later `lock()` still clears instance access normally.

Encoded envelope fields are type-checked and canonical-base64-checked before decoding. Fixed-size salt, nonce, and tag fields retain their length validation; malformed fields consistently produce `VAULT_FORMAT_ERROR`.
