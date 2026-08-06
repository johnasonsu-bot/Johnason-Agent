# Batch 1 Provider Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cross-platform encrypted credential vault and an operable Provider Center for LM Studio and DeepSeek V4 Flash thinking mode.

**Architecture:** Provider metadata is durable SQLite state while secret material is encrypted in a separate vault using Argon2id-derived AES-GCM keys. Provider adapters expose discovery, health, completion, and streaming through the existing Model Gateway.

**Tech Stack:** Python, FastAPI, SQLite, Pydantic, HTTPX, `cryptography`, `argon2-cffi`, React, TypeScript, Electron, Playwright.

## Global Constraints

- Never persist or log plaintext credentials.
- Do not depend on an operating-system credential store.
- DeepSeek thinking calls omit sampling parameters and preserve `reasoning_content` across tool-call turns.
- The batch gate requires UI creation, unlocking, connection testing, and model selection.

---

### Task 1: Encrypted Credential Vault

**Files:**
- Modify: `mvp/pyproject.toml`
- Create: `mvp/src/workbench/credentials/vault.py`
- Create: `mvp/src/workbench/credentials/models.py`
- Test: `mvp/tests/unit/credentials/test_vault.py`

**Interfaces:**
- Produces: `CredentialVault.create(path: Path, password: str)`, `unlock(password: str)`, `put(secret_id: str, value: str)`, `get(secret_id: str) -> str`, `lock()`.

- [ ] **Step 1: Write failing vault tests**

```python
def test_vault_encrypts_and_requires_correct_password(tmp_path):
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    vault.put("provider/deepseek", "secret-value")
    vault.lock()
    assert b"secret-value" not in (tmp_path / "vault.bin").read_bytes()
    with pytest.raises(VaultUnlockError):
        vault.unlock("wrong")
    vault.unlock("correct horse")
    assert vault.get("provider/deepseek") == "secret-value"
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_vault.py -v`

Expected: FAIL because `workbench.credentials.vault` does not exist.

- [ ] **Step 3: Implement the minimal vault**

Use Argon2id with a random 16-byte salt to derive a 32-byte key and AES-GCM with a random 12-byte nonce. Store only version, KDF parameters, salt, nonce, and ciphertext. Zero the in-memory secret dictionary on `lock()`.

```python
class CredentialVault:
    @classmethod
    def create(cls, path: Path, password: str) -> "CredentialVault": ...
    def unlock(self, password: str) -> None: ...
    def put(self, secret_id: str, value: str) -> None: ...
    def get(self, secret_id: str) -> str: ...
    def lock(self) -> None: ...
```

- [ ] **Step 4: Verify GREEN and leak resistance**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_vault.py -v`

Expected: PASS, including wrong-password, tamper, locked-access, and plaintext scan cases.

- [ ] **Step 5: Commit**

```bash
git add mvp/pyproject.toml mvp/src/workbench/credentials mvp/tests/unit/credentials
git commit -m "feat: add encrypted credential vault"
```

### Task 2: Provider Profiles and DeepSeek Compatibility

**Files:**
- Create: `mvp/src/workbench/models/profiles.py`
- Create: `mvp/src/workbench/models/deepseek.py`
- Modify: `mvp/src/workbench/models/contracts.py`
- Modify: `mvp/src/workbench/models/gateway.py`
- Test: `mvp/tests/unit/models/test_deepseek.py`
- Test: `mvp/tests/unit/models/test_profiles.py`

**Interfaces:**
- Produces: `ProviderProfileRecord`, `ProviderCapability`, `DeepSeekProvider.complete()`, `DeepSeekProvider.stream()`.
- Consumes: `CredentialVault.get(secret_id)` and existing `ModelRequest`, `ModelEvent`.

- [ ] **Step 1: Write failing request-shape and reasoning replay tests**

```python
async def test_thinking_tool_turn_replays_reasoning_content(mock_transport):
    provider = DeepSeekProvider(client=httpx.AsyncClient(transport=mock_transport))
    request = ModelRequest(model="deepseek-v4-flash", messages=tool_turn_messages())
    await provider.complete(request, deepseek_profile())
    sent = json.loads(mock_transport.requests[-1].content)
    assert sent["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "high"
    assert "temperature" not in sent
    assert sent["messages"][1]["reasoning_content"] == "preserved"
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/unit/models/test_deepseek.py tests/unit/models/test_profiles.py -v`

Expected: FAIL because DeepSeek contracts and capability fields are absent.

- [ ] **Step 3: Implement Provider profiles and adapter**

Extend model messages to preserve provider-neutral reasoning metadata internally. Add DeepSeek request normalization that disables unsupported `tool_choice` and sampling fields in thinking mode.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/unit/models/test_deepseek.py tests/unit/models/test_profiles.py tests/unit/models/test_gateway.py -v`

Expected: PASS for streaming text, reasoning deltas, fragmented tool calls, replay, HTTP errors, and secret redaction.

- [ ] **Step 5: Commit**

```bash
git add mvp/src/workbench/models mvp/tests/unit/models
git commit -m "feat: add DeepSeek thinking provider"
```

### Task 3: Provider Repository and API

**Files:**
- Create: `mvp/src/workbench/providers/repository.py`
- Create: `mvp/src/workbench/api/providers.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Modify: `mvp/src/workbench/api/app.py`
- Test: `mvp/tests/unit/api/test_providers.py`

**Interfaces:**
- Produces REST endpoints: `GET/POST /api/providers`, `POST /api/providers/{id}/secret`, `POST /api/providers/{id}/test`, `GET /api/providers/{id}/models`.

- [ ] **Step 1: Write failing API tests**

```python
def test_provider_response_never_contains_secret(client, unlocked_vault):
    response = client.post("/api/providers", json=deepseek_payload())
    assert response.status_code == 201
    assert "api_key" not in response.text.lower()
    assert response.json()["credential_status"] == "missing"
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/unit/api/test_providers.py -v`

Expected: FAIL with 404.

- [ ] **Step 3: Implement schema, repository, and routes**

Store non-secret metadata in `model_provider_profiles`; store only a random `secret_id` reference. Test connections through Model Gateway and return normalized latency, models, and redacted error codes.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/unit/api/test_providers.py -v`

Expected: PASS for locked vault, create/update, model discovery, LM Studio offline, DeepSeek auth failure, and response redaction.

- [ ] **Step 5: Commit**

```bash
git add mvp/src/workbench/providers mvp/src/workbench/api mvp/src/workbench/workflow/schema.py mvp/tests/unit/api/test_providers.py
git commit -m "feat: expose model provider management API"
```

### Task 4: Provider Center UI and Batch Gate

**Files:**
- Create: `mvp/canvas-spike/src/renderer/providers/ProviderCenter.tsx`
- Create: `mvp/canvas-spike/src/renderer/providers/ProviderForm.tsx`
- Create: `mvp/canvas-spike/src/renderer/api.ts`
- Modify: `mvp/canvas-spike/src/renderer/App.tsx`
- Test: `mvp/canvas-spike/tests/providers.spec.ts`
- Create: `mvp/tests/acceptance/test_batch1_provider_center.py`

**Interfaces:**
- Consumes: Batch 1 Provider REST endpoints.
- Produces: usable vault unlock, Provider CRUD, model discovery, connection test, and default-model selection UI.

- [ ] **Step 1: Write failing Playwright test**

```ts
test("unlocks vault and selects an LM Studio model", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: "模型供应商" }).click();
  await page.getByLabel("主密码").fill("test-password");
  await page.getByRole("button", { name: "解锁" }).click();
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.getByText("连接正常")).toBeVisible();
});
```

- [ ] **Step 2: Verify RED**

Run: `npm test --prefix canvas-spike -- --grep "unlocks vault"`

Expected: FAIL because Provider Center navigation is absent.

- [ ] **Step 3: Implement Provider Center UI**

Use masked credential status, never repopulate secret inputs, and clear input state immediately after successful submission. Add LM Studio and DeepSeek presets.

- [ ] **Step 4: Verify batch gate**

Run:

```bash
.venv/bin/python -m pytest tests/unit/credentials tests/unit/models tests/unit/api/test_providers.py tests/acceptance/test_batch1_provider_center.py -v
npm test --prefix canvas-spike
```

Expected: PASS. Live DeepSeek test remains explicitly pending until the user enters the API key in the completed UI.

- [ ] **Step 5: Commit**

```bash
git add mvp/canvas-spike mvp/tests/acceptance/test_batch1_provider_center.py
git commit -m "feat: add provider center interface"
```
