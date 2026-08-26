# Task 5 Report

## Fix 1

- RED: 新增 public-text、identity、恶意类型和跨 Runtime resume 红测；初始 mapper/AG-UI 测试出现 23 项预期失败，覆盖私有文本透传、伪造 ID/cursor 以及 unhashable status/operation。
- GREEN: Runtime mapper 与 AG-UI 使用同一套 V2 public-text/opaque-ID 校验；AG-UI 对伪造持久事件 fail closed；所有 set membership 前完成类型检查。
- Cross-runtime: Python、Fake Goose、Fake DSH 轻量 emitter 输出同一 RuntimeEventV2 后，Domain 与 AG-UI 的 type/run/term/step/cursor/public payload 相同；EventStore + `replay_agui(after_sequence=1)` 只输出 cursor 2。
- Verification: Task 5 mapper/AG-UI、AG-UI resume、Task 4 query/lifecycle/run 共 209 项通过；`compileall` 与 `git diff --check` 通过。测试输出仅含既有 Starlette TestClient 弃用警告。
- Risk: public text 策略按私有标签（例如 `reasoning:`、`Traceback (`）和 credential/path 特征拒绝，避免仅出现普通业务词汇时误拒绝；未公开原始 exception、tool arguments/results 或 runtime-specific 字段。

## Fix 2

- RED: V2 伪造 `run.failed`、tool-args 和 state 事件会进入 V1 raw 分支；camel/snake/kebab 私有标签、naive/model-constructed 时间、artifact display text 和敏感 ID 前缀仍有缺口。
- GREEN: `engine_host.v2` 在 AG-UI 顶层仅允许 Task 5 mapper 实际产生的 event type；所有其他 V2 event 均返回空。V1 仍保留其既有 raw event 映射。
- Boundary: shared validator 覆盖 `providerRef`、`workspace_path`、`manifest-digest`、`reasoningContent`、`vault-id`、`secretToken`、`credentialId` 等私有标签和值；opaque ID 同步拒绝敏感前缀，合法相邻词通过。`artifact_ref` 单独按 opaque ID 校验。
- Identity: V2 二次边界要求 timezone-aware `occurred_at`、正整数 `sequence == cursor`，并对 `model_construct` 的 list/dict/bool/naive 值 fail closed。
- Cross-runtime: Python、Fake Goose、Fake DSH 分别通过独立 emitter 构造相同规范 event，再验证公开投影与 EventStore/SSE resume 等价。
- Verification: Task 5 mapper/AG-UI、AG-UI resume、Task 4 query/lifecycle/run 共 242 项通过；`compileall` 与 `git diff --check` 通过。仅既有 Starlette TestClient 弃用警告。

## Fix 3

- RED: `manifestRef`/`manifest_reference`/`manifest-ref`/`manifestReference` 及 workspace/vault 的 ref/reference 变体能绕过 public-text 和 opaque-ID 边界。
- GREEN: shared validator 将 ref/reference 纳入 manifest/workspace/vault 的敏感后缀，统一覆盖 camel、snake、kebab、`:` 与 `=`。term/artifact ID、runtime `artifact_ref`、以及伪造持久 Event 均 fail closed；`manifestation-ref`、`workspace-note`、`vaulted-reference` 等合法邻近 ID 可通过。
- Cross-runtime: Python adapter 使用 typed kwargs；Fake Goose 从 NDJSON-shaped mapping `model_validate`；Fake DSH 从异构 source event 显式规范化。三条独立 decode 路径再共享公开投影断言。
- Verification: Task 5 mapper/AG-UI、AG-UI resume、Task 4 query/lifecycle/run 共 263 项通过；`compileall` 与 `git diff --check` 通过。仅既有 Starlette TestClient 弃用警告。

## Fix 4

- RED: 用 root × metadata suffix × separator/camel/Pascal 组合表覆盖 runtime summary、runtime identity、`artifact_ref` 与伪造 persisted `DomainEvent`；旧枚举正则出现 643 项预期失败。随后单独加入 `historyRef` 根并再次观察到预期红测。
- GREEN: 删除三套易分叉的敏感字符串枚举，统一将 camelCase/PascalCase（含 acronym 边界）以及 snake/kebab/dot/space/colon/equals 拆成 lowercase tokens；public text 只拒绝“敏感根/短语 + 元数据”、敏感赋值标签或 credential/path/traceback 形态，opaque ID 则拒绝敏感词组和 `sk`/bearer 等严格前缀。
- False positives: `secretary`、`token_count`/`tokenCount`/`token-count`、workspace/provider 普通业务句子，以及 `manifestation`/`vaulted` 等合法邻近词均通过；`token=`、`token-ref-*`、`vault-private`、`reasoningRef`、`chainOfThoughtReference`、`privatePromptRef`、`history_reference` 均 fail closed。
- Boundaries: AG-UI 继续直接复用 runtime mapper 的共享 validator，无需额外适配；runtime identity/summary、persisted identity/summary 与 runtime/persisted `artifact_ref` 使用同一规范化语义。
- Verification: Task 5、AG-UI、resume、Task 4 与 V1/Host 回归共 1188 项通过；独立恶意 probe 覆盖 17 个危险文本、8 个危险 ID 和 11 个安全邻近项；`compileall`、`git diff --check` 均通过。仅有既存 Starlette TestClient 弃用警告。

## Fix 5

- RED: 新增精确 compact credential 表驱动测试，覆盖 `APIKEY`/`apikey`、`ACCESSTOKEN`/`accesstoken`、`PRIVATEKEY`/`privatekey`、`GITHUBPAT`/`githubpat`，以及 apiKey、api_token、accessToken、privateKey、clientSecret、secretKey、authToken、bearerToken、githubPat 的 camel/separator 形式；旧实现出现 100 项预期失败，分别证明 runtime public text、runtime identity/`artifact_ref`、persisted public text/identity/`artifact_ref` 可被穿透。另有 33 assignments fail-closed 与 normalization 调用计数红测，证明增长前缀被重复解析。
- GREEN: tokenizer 先对完整 token 规范为 lowercase，仅命中显式 `_CANONICAL_COMPACT_LABELS` 时展开；未进行任意词典分词。credential roots 同步加入 client-secret、secret-key、auth-token、bearer-token、github-pat，三层边界复用同一语义。
- Complexity: `_contains_sensitive_assignment` 改为一次 `_LABEL_TOKEN.finditer` 线性扫描，只保留最长 3 个 label words；公开文本最多允许 32 个 `:`/`=`，第 33 个立即 fail closed。所有正则均为固定 lookaround、字符类或无嵌套量词分支；最大长度 alternating camel 输入用于验证 tokenizer 不存在重复前缀扫描或回溯放大。
- False positives: `apikeyboard`、`apitokens`、`accesskeys`、`accesstokens`、`privatekeynote`、`clientsecrets`、`secretkeys`、`authtokens`、`bearertokens`、`githubpattern` 等非精确邻近词继续通过 runtime 与 persisted 校验。
- Verification: 两份 Task 5 mapper 测试共 1042 项通过；Task 5、AG-UI、resume、Task 4 query/lifecycle/run 与 V1/Host 回归共 1268 项通过。独立组合 probe 覆盖 40 个 credential 风格、5 个安全邻近项，并对 4096 字符/32 assignments 输入执行 1000 次（0.7604 秒）；`compileall`、`git diff --check` 均通过。仅有既存 Starlette TestClient 弃用警告。
