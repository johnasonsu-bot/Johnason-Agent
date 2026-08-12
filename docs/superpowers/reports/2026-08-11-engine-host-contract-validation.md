# Engine Host Contract Validation

## Decision

`GO_G1_DIAGNOSTIC_UI`

## Verified commands

From `mvp/`:

```bash
.venv/bin/python -m pytest tests/acceptance/test_engine_host_contract.py -v
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q
.venv/bin/python -m compileall -q src tests
TASK5_SCAN_ROOT="$(mktemp -d /tmp/hermes-task5-leak.XXXXXX)"
.venv/bin/python -m pytest tests/acceptance/test_engine_host_contract.py::test_protocol_artifacts_do_not_contain_sensitive_values -q --basetemp="$TASK5_SCAN_ROOT"
! rg -n -i 'reasoning_content|api_key|password' "$TASK5_SCAN_ROOT"
```

## Results

- Offline Engine Host acceptance: 15 passed.
- Backend unit, integration, and acceptance suites: 439 passed, 6 skipped.
- Python compile check: passed.
- Isolated artifact leak acceptance: 1 passed; the follow-up scan found no matches.
- Existing upstream warning: one Starlette `httpx` deprecation warning.

## Protocol and capabilities

- Protocol: `workbench.engine-host/v1`.
- Maximum frame size: 1,048,576 bytes.
- Negotiated in the offline fixture: model and AG-UI enabled; tools, skills, and workspace disabled.
- Lifecycle coverage: handshake, normal run, cancellation, drain, graceful shutdown, and forced shutdown.
- Failure outcomes: pre-start, accepted-before-tool, read-only-effect, and protocol failures without an unknown write effect are retryable; an unfinished write effect requires reconciliation.
- Retry routing remains pinned to the Engine Host and is released only by a new Host generation; Python fallback is not used.
- Protocol capture records metadata only: message names and identifiers, sequence, direction, and byte count.

## Known limitations

- G1 admits only the configured local LM Studio provider path; secret-bearing providers remain outside this boundary.
- Recovery is run replay at a new Host-generation boundary, not token-level continuation.
- Unknown write effects require manual reconciliation and are never replayed automatically.
- This validation uses a deterministic offline Host; it does not claim live-provider or production-sidecar validation.
