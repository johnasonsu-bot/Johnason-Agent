# Task 3 报告：外部 DEV_UNTRUSTED 准入与真实端点证据

## 结论

Task 3 已实现并通过离线合同、source gate、组合根和 brief 指定验收。
Workbench 应用进程只验证并导入外部发布的公钥、receipt、manifest 与
secret-free evidence；签名只发生在显式运行的外部准备命令中，一次性 Ed25519
私钥没有序列化或落盘路径。

本任务没有调用 DeepSeek、LM Studio 或其他真实模型端点，也没有生成 Goose/DSH
的 GO 或可选状态。普通 mock/fixture 只能覆盖拒绝和协议分支；真实调用保留给
Task 5，由用户使用已保存 Provider Profile 与 Vault 显式触发。

## 交付

### 外部证据与签名合同

- 新增 `LiveEndpointEvidenceV1`，冻结并精确验证 Runtime、build、Provider Profile
  digest、最终模型、cloud/local 类型、观测时间、延迟、唯一终态与输出摘要；证据
  不包含 URL、credential reference 的值或模型输出正文。
- 只有 `verify_runtime_live_endpoint()` 经精确类型的
  `FederatedConversationExecutor` 完成正式执行后，才能产生进程内私有的 verified
  evidence。公开 Pydantic evidence、fixture evidence 或自行构造的对象不能用于签发
  receipt。
- live execution snapshot 使用严格顶层字段白名单，并再次绑定 Envelope 中的
  Runtime/build、Provider Profile digest 与 resolved model；额外字段在进入执行器前
  即被拒绝。
- `collect_runtime_live_endpoint_evidence()` 只接受 Runtime selector、已保存的
  Provider Profile ID、绝对 runtime directory 与正式 `VaultService`。它依次建立临时
  DEV trust、启动固定 Sidecar、读取实际注册能力、通过正式 Admission 创建
  Assignment，再经 Supervisor、Provider Grant Broker、Host v2 和
  `FederatedConversationExecutor` 取得证据。
- 临时 proof 只授权 sidecar 当时实际广告的能力且不发布；它不会把当前
  `model=false` 的 Goose/DSH 变成应用可选 Runtime。

### 原子发布与 fail-closed 导入

- `prepare_development_environment(runtime_ids, output_dir)` 为每个所选 Runtime
  生成独立 receipt；Python Term 复用既有 DEV Gate 文档，Goose/DSH 必须携带同进程
  live verified evidence。
- 同一次准备只生成一个内存中的 Ed25519 私钥；输出仅含公钥、签名 proof、
  secret-free evidence 与签名 manifest。每个文件通过独占临时文件、文件 `fsync`、
  `os.replace()` 和目录 `fsync` 发布，manifest 最后写入作为 commit marker。
- 顶层/嵌套发布目录 symlink、Runtime 可执行文件 symlink、manifest/proof/evidence
  symlink、runtime record 路径穿越、非规范 JSON、文件摘要漂移、签名/期限/identity/
  capability 漂移均失败关闭。
- 再次准备会重新验证当前 source/build 并重签发布，不会因旧 manifest 自身签名仍
  有效而错误返回旧 bundle。
- `load_development_admission()` 先完整验证整个 bundle，再导入仅含 public
  `DEV_UNTRUSTED` trust root 的 Assignment repository，并生成三个互相独立的
  `RuntimeCatalogEntry`；任一构件无效时返回空导入结果，不部分开放 Catalog。

### 应用组合与 source/build 身份

- 新增显式配置 `federated_runtime_development_trust` 及环境变量
  `WORKBENCH_FEDERATED_RUNTIME_DEVELOPMENT_TRUST=true|false`。未显式启用时不导入
  外部 DEV bundle。
- `main.py` 只调用 loader，不生成签名。组合根按 Runtime 分别计算 provider、executor
  与 enabled readiness，并保留正式 federated executor 供 Conversation 链路使用。
- Goose 与 DeepSeek Harness source gate 新增只读 build identity 导出；它们复用现有
  pinned source/build/protocol smoke 验证，只返回 build/source/build-manifest digest，
  不返回 `model=true` 或 Gate decision。
- Python Term gate manifest 通过官方生成脚本刷新，以覆盖最终 package 与 tests
  摘要：`generated_files=153 build_inputs=7`。

## TDD 证据

### RED

1. 初始 Task 3 单元/集成测试：`15 failed`，缺少统一 admission 模块和外部 CLI。
2. source/build identity 合同：Goose 与 DSH 各因缺少 identity helper 失败。
3. settings、组合根、每 Runtime readiness 与 unified Python trust 测试分别先因缺少
   配置/接线失败。
4. live snapshot/collector CLI 合同先因缺少 builder 或命令失败。
5. capability drift 回归先显示 signed Catalog 与 live sidecar 能力不一致时仍可选择。
6. 嵌套发布目录 symlink 首轮可写到目录外；固定 executable symlink 首轮未拒绝。
7. runtime record `proof_path=../...` 首轮仍可导入；修复后固定文件名与精确字段集。
8. live snapshot 注入额外字段首轮进入正式 executor；修复后在调用 Assignment 前拒绝。
9. CLI 首轮对 `output-dir` 调用 `resolve()`，会把 symlink 变成真实目标路径；修复后保留
   原路径交由核心边界拒绝。
10. 已签名 bundle 的顶层目录 symlink、manifest commit-marker symlink 首轮仍可读取；
    修复后均失败关闭。
11. source gate identity helper 在 `resolve()` 后检查产物，首轮接受仓库内 symlink；
    Goose/DSH 均改为先检查原始固定候选路径。
12. source revision 改变后，准备器首轮错误返回 `already_prepared`；修复后重新签发且新
    bundle 可由 loader 导入。

### GREEN

1. Task 3 自有单元/集成：
   `37 passed in 7.40s`。
2. `main.py` 与相邻 Runtime admission：
   `51 passed in 7.02s`。
3. Goose/DSH source gate 单元：
   `52 passed in 34.35s`。
4. Goose/DSH source acceptance：
   `6 passed in 2.83s`。
5. brief 指定五组最终新鲜验收（含 Python Term runtime gate）：
   `73 passed in 32.62s`。
6. 两套真实工作树 build identity helper：均返回预期 Runtime/build ID 与两个 64 位
   digest；未调用模型端点。
7. `py_compile` 与 `git diff --check`：退出码 0。环境未安装 `ruff` 或 `mypy`，未将其
   列为通过项。

## 相邻合同说明

按 brief，主要修改限定在 admission、两个 source gate、settings、main、两个 CLI 与
对应测试。另修改
`mvp/src/workbench/runtime/engine_host/v2/runtime_admission.py`：既有 Probe 只接受三个
全局 bool，无法表达 Python/Goose/DSH 独立 readiness；现兼容原 bool，并可接受每
Runtime bool mapping。同时要求 live registry capability tuple 与 signed Catalog 所需能力
完全一致，避免 `model=false` sidecar 因存在 `model=true` receipt 而被误报可选。该改动
是实现“独立准入且能力漂移失败关闭”的最小相邻边界。

`mvp/src/workbench/runtime/python_term/gate_manifest.json` 由现有官方生成器更新；因为该
manifest 覆盖整个 `workbench` package 与 tests 摘要，Task 3 新文件及相邻兼容修复会
自然改变其 digest。

## 自审

- 应用启动路径无 `Ed25519PrivateKey.generate()` 调用；签名函数只由外部脚本显式调用。
- CLI 不接受 API key、token、base URL 或原始模型配置；Vault password 只从 stdin
  读取，Provider credential 仍由正式 Broker 在私有 Grant delivery 中解析和清零。
- 没有 fallback 到 fixture、其他 Provider、其他 model 或其他 Runtime。
- 公开 evidence 不能独立成为 authority；receipt 同时绑定 evidence digest、source
  manifest、build manifest 和 capability digest。
- 当前代码没有伪造 Goose/DSH `model=true` 注册，也没有生成任何 live evidence 文件。

## Concerns / Task 5 前置条件

1. 当前固定 Goose 与 DSH sidecar 仍分别广告 `model=false`。即使用户完成真实端点
   证据并签发 `model=true` receipt，应用 Probe 也会按设计保持不可选，直到对应 Host
   build 真实、诚实地广告同一 capability snapshot 并重新生成 source/build evidence。
2. Task 5 必须由用户显式运行外部 verifier/preparer，并使用 Vault 中已保存的真实
   Profile；本任务没有验证外部账号额度、网络可达性或真实 LM Studio 进程状态。
3. 正式 `ProviderGrantBroker` 当前要求 Profile 有 credential reference；无凭据本地
   API 若要完全免 Vault credential，需由 Provider Grant 合同单独支持。本任务未绕过
   Task 2 authority，也未扩大该接口。
4. 未运行仓库无关的全量测试或广泛安全扫描；按设计仅验证本任务功能合同、明确凭据
   载体和相关回归。
