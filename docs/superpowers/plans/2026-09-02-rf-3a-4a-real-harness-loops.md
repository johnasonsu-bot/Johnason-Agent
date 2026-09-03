# RF-3A / RF-4A 真实 Harness 模型循环实施计划

**目标：** 让 Goose 与 DeepSeek Harness 使用模型配置中心中同一份 Provider Profile 和 Vault 凭据，通过统一 Host v2 与一次性私有 Provider Grant 完成真实模型请求；Codex-compatible Python 继续作为已接通的基准运行时。

**架构：** Broker 在一次性 Grant 中冻结 secret-free 的 Provider 路由快照（协议、基础地址、安全元数据头、推理配置和已解析模型），密钥仍只经私有 socket 传递。Sidecar 先校验并 ACK 精确 Grant，再将短生命周期内存中的路由与密钥交给 pinned upstream Harness。普通 Host NDJSON、环境变量、argv、日志和持久化状态不承载凭据。

**范围约束：** 本批只完成真实模型循环和用户可操作 Smoke，不提前实现 Goose RF-3B 的 Plan/Todo/Tool/Intervention，也不授予联合 `GO_RUNTIME_FEDERATION`。固定冒烟路径继续保留为离线合同测试。

## Task 1：冻结跨运行时 Provider 路由快照

- [x] 新增不可变 `ProviderGrantRouteV1`：`protocol`、`base_url`、排序后的安全元数据头、`thinking_enabled`、`reasoning_effort`。
- [x] `ProviderGrantBinding` 必须携带路由快照，canonical digest 覆盖全部字段；禁止 Authorization、Token、密码或 query credential。
- [x] Broker 从已通过 `ProviderProfileRecord` 校验的当前 Profile 生成路由快照；Profile 变化继续由既有 profile digest 拒绝。
- [x] Goose 与 DSH receiver 对精确字段、URL、头部与推理配置 fail closed，并把快照与一次性密钥作为一个不可拆分的消费结果交给模型桥。
- [x] 运行 Python Grant 合同/Repository/Broker/Transport、Goose Cargo 和 DSH Node 回归。

## Task 2：Goose pinned upstream 最小真实 Query

- [x] Slice 2.1：Host 使用锁定的 Rust 1.96.1 与 pinned `goose-providers`，由一次性 Grant 驱动真实 OpenAI-compatible 流；本地 mock 已验证路由、模型、思考参数、元数据头、内存凭据、文本与 usage-only 事件。
- [ ] 通过 pinned Goose 的公开库接口创建单轮 Provider/Session，不读取运行时专属配置文件或 API Key 环境变量。
- [ ] 用 `RuntimeQueryInputV2` 生成有序输入，将 upstream assistant text / tool / terminal 事件映射为 Host v2 唯一有序事件。
- [ ] 固定冒烟与真实 provider 分支明确分离；真实分支失败不得回落 fixture 或其他 Provider。
- [ ] 真实能力验证后才把 Goose `model` capability 改为 true，并生成独立 `GO_GOOSE_QUERY_SMOKE` 用户验收证据。

## Task 3：DeepSeek Harness pinned upstream 最小真实 Session

- [ ] 通过 pinned DeepSeek Harness session/bootstrap API 注入有序 PromptSection、消息和上下文，不读取运行时专属配置文件或 API Key 环境变量。
- [ ] 使用 Grant 的 protocol/base URL/model/thinking 配置和短生命周期密钥；映射原生流事件、唯一终态与 seal。
- [ ] 固定冒烟与真实 provider 分支明确分离；真实分支失败不得回落 fixture 或其他 Provider。
- [ ] 真实能力验证后才把 DSH `model` capability 改为 true，并生成独立 `GO_DSH_PLUGIN_SMOKE` 用户验收证据。

## Task 4：用户可操作验收与联邦证据

- [ ] 前端/测试入口可选择 Codex-compatible Python、Goose、DeepSeek Harness 和已保存 Provider；不显示或复制密钥。
- [ ] 三通道分别验证流式文本、Provider/模型精确绑定、错误诊断、取消、重复命令、进程重启与独立结果。
- [ ] 任一通道失败只阻塞自身 GO；只有三通道和共享合同均通过时，才另行评估 `GO_RUNTIME_FEDERATION`。

## 机器验收

每个 Task 均先提交失败测试再实现。测试必须使用随机测试凭据或本地 mock provider；真实用户凭据只从 Vault 解析，不进入仓库、测试输出或证据文件。P0/P1/P2 完成前不执行广泛安全扫描，本批只检查功能合同中明确禁止的凭据载体。
