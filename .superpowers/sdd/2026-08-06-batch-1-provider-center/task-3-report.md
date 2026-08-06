# Task 3 Report: Provider Repository and API

## Status

Implemented the Provider Center persistence and REST API. Provider metadata is
stored in SQLite; credential input is accepted only by the secret endpoint and
written directly to the unlocked encrypted vault.

## RED evidence

```bash
cd mvp && .venv/bin/python -m pytest tests/unit/api/test_providers.py -v
```

Observed before implementation: the first Provider API test failed because
`AppSettings` did not accept a vault dependency. The next red cycle had five
expected route failures (404) for the unimplemented secret, connection-test,
and model-discovery endpoints. Two subsequent security red cycles proved that
FastAPI/Pydantic validation reflected rejected credential values before route
body parsing was made explicitly redacted.

## GREEN evidence

```bash
cd mvp && .venv/bin/python -m pytest tests/unit/api/test_providers.py -v
# 11 passed, 1 warning

cd mvp && .venv/bin/python -m pytest -v
# 126 passed, 4 skipped, 1 warning
```

The warning is the existing Starlette `TestClient` HTTPX deprecation warning.
The skipped tests are pre-existing live integrations.

## Files

- Added `mvp/src/workbench/providers/repository.py`
- Added `mvp/src/workbench/providers/__init__.py`
- Added `mvp/src/workbench/api/providers.py`
- Added `mvp/tests/unit/api/test_providers.py`
- Updated `mvp/src/workbench/workflow/schema.py`
- Updated `mvp/src/workbench/api/app.py`
- Updated `mvp/src/workbench/models/gateway.py`
- Updated `mvp/src/workbench/models/lmstudio.py`

## API contract

- `GET /api/providers` returns only serializable metadata and
  `credential_status` (`missing`, `configured`, or `locked`); it omits
  `secret_id`, `Authorization`, and every credential value.
- `POST /api/providers` creates (201) or updates (200) metadata while
  preserving the opaque random vault reference on updates.
- `POST /api/providers/{id}/secret` accepts `value` (also `secret` or
  `api_key` as input aliases), calls `CredentialVault.put` directly, and
  returns only provider id plus configured status. Locked vault responses are
  423 and never echo the submitted body.
- `POST /api/providers/{id}/test` returns normalized
  `status`, `latency_ms`, `models`, and `error_code`; transport, HTTP auth,
  vault, and provider errors are mapped without surfacing raw messages.
- `GET /api/providers/{id}/models` returns normalized online/offline/error
  status, discovered model ids, and a redacted error code.

## Review remediation

- Model discovery and connection tests now use each saved LM Studio profile URL,
  rather than the adapter construction URL.
- LM Studio 401/403 responses retain their HTTP type long enough to normalize
  to `authentication_failed` at the API boundary.
- Profile create-or-update now holds an SQLite immediate transaction while it
  either allocates or preserves the opaque `secret_id`; concurrent first
  creates cannot return different durable secret references.
- The normal application entrypoint intentionally leaves the vault locked and
  gateway unconfigured until the Provider Center/launcher supplies them. The
  additive `AppSettings` dependencies support that explicit configuration
  without changing existing callers.

## Commit

`feat: expose model provider management API`

## Concerns

- Live provider requests remain intentionally unperformed; unit tests use
  local HTTP transports and the provided fake gateway. No credential has been
  supplied or recorded.
