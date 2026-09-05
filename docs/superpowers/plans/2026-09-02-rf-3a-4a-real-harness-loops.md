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
- [x] Slice 2.2：`RuntimeQueryInputV2` 已形成稳定 Prompt/Context/Message 顺序；真实 Provider 在独立异步任务运行，Host 可并行接收 cancel，并把文本、推理计数、唯一终态与 seal 写回统一事件流。
- [x] Slice 2.3：release Host 已通过非 fixture 的本地 mock 端到端调用，证明正式私有 Grant → pinned Goose → Host v2 流式事件链路，且公共帧、环境和诊断不含测试凭据。
- [x] Slice 2.4：release Host 已证明请求抵达真实 Provider 后仍可并行接收 `query.cancel`，中止在途请求并以 cursor 2 形成唯一 cancelled 终态。
- [x] 通过 pinned Goose 的公开库接口创建单轮 Provider/Session，不读取运行时专属配置文件或 API Key 环境变量。
- [ ] 用 `RuntimeQueryInputV2` 生成有序输入，将 upstream assistant text / tool / terminal 事件映射为 Host v2 唯一有序事件。（text、reasoning、terminal 已完成；tool 等待持久化 Tool bridge）
- [x] 固定冒烟与真实 provider 分支明确分离；真实分支失败不得回落 fixture 或其他 Provider。
- [ ] 真实能力验证后才把 Goose `model` capability 改为 true，并生成独立 `GO_GOOSE_QUERY_SMOKE` 用户验收证据。

## Task 3：DeepSeek Harness pinned upstream 最小真实 Session

- [x] Slice 3.1：Sidecar 已直接绑定 pinned upstream `DeepSeekAdapter`，由正式 Grant 注入 endpoint、Broker 解析模型、thinking/effort 与一次性内存凭据；不读取运行时专属配置文件或 API Key 环境变量。
- [x] Slice 3.2：源码路径和发布后的 NDJSON Sidecar 均通过本地真实 SSE Provider；验证增量文本、完整消息、唯一完成态，以及请求到达 Provider 后仍可并发取消并形成 cursor 2 的 cancelled 终态。
- [x] 通过 pinned DeepSeek Harness session/bootstrap API 注入有序 PromptSection、消息和上下文，不读取运行时专属配置文件或 API Key 环境变量。
- [x] 使用 Grant 的 protocol/base URL/model/thinking 配置和短生命周期密钥；映射原生 Session 文本流、消息、生命周期、唯一终态与 seal。（原生工具执行仍等待持久化 Tool bridge）
- [x] 固定冒烟与真实 provider 分支明确分离；真实分支失败不得回落 fixture 或其他 Provider。
- [x] 用户通过 GUI 完成当前 public bundle 的单次真实云端请求：job `4898e6381c184ba19a84cc321617c91e`，Provider `deepseek-primary`，模型 `deepseek-v4-flash-vision-exp`，终态 `completed`，evidence 延迟 `1015 ms`；正式 loader 已验证 public proof 与当前 Profile digest 匹配，信任层 `DEV_UNTRUSTED`。此项仅记为 `PASS_SINGLE_COMPLETION`。
- [ ] 真实能力验证后才把 DSH `model` capability 改为 true，并生成独立 `GO_DSH_PLUGIN_SMOKE` 用户验收证据。

## Task 4：用户可操作验收与联邦证据

- [ ] 前端/测试入口已实现四模式、保存 Provider 和不回显密钥，但最近一次全量前端回归仍出现 Goose 模式恢复为空的失败，关闭前不标记完成。
- [ ] 三通道分别验证流式文本、Provider/模型精确绑定、错误诊断、取消、重复命令、进程重启与独立结果。
- [ ] 任一通道失败只阻塞自身 GO；只有三通道和共享合同均通过时，才另行评估 `GO_RUNTIME_FEDERATION`。

## Task 5：用户路径回归与独立 Gate 决策

- [x] 新增 `tests/acceptance/test_federated_runtime_user_path.py`：离线 case 经真实 Conversation HTTP/SQLite 路径覆盖 Goose/DSH 消息、唯一终态、幂等、身份冲突、恢复、取消和故障隔离；外部 Runtime 为确定性测试实现，明确不计 live GO。
- [x] Python Term、Goose、DSH 的真实 HTTP 用户路径均要求 `WORKBENCH_RUN_LIVE_RUNTIME_ACCEPTANCE=1`，默认 3 个 case 为 `SKIPPED`；测试不读取 Vault 或明文凭据。
- [x] live 客户端只接受无 userinfo/path/query 的 loopback HTTP origin，不跟随 redirect；自动检查要求精确回复 marker 与显式预期 build，不再以任意文本或任意 ready build 计作通过。
- [x] 离线回归补充 chat 省略 selector 的固定通道（显式空字符串返回 422），以及同一 command 在环境从 build A 切至 build B 后仍冻结引用 A、新 command 才引用 B 的护栏。
- [x] 记录 DSH 当前 bundle 的一条用户 GUI 真实云端 completion，并与 fixture/CLI `prepared` 证据分开。
- [ ] 当前自动 live case 仅为 `LIMITED_COMPLETION_CHECK`；公共 API 未暴露实际 Provider Profile digest、resolved model 或 fallback attestation，不能用请求字段代替执行绑定证据。最小后续方案是增加 secret-free、command-scoped 的签名执行 attestation，绑定冻结 Provider digest、resolved model、Runtime build 与 proof identity。
- [ ] Python Term 与 Goose 当前 bundle 的真实用户路径仍为 `MANUAL_PENDING`；既往旧 build 证据不迁移。
- [ ] DSH 当前 bundle 的新模型命令取消、错误、重复与执行恢复仍为 `MANUAL_PENDING`；已通过的历史 timeline 重启读取不能替代这些路径，因此 `GO_DSH_PLUGIN_SMOKE=HOLD`。
- [ ] `GO_GOOSE_QUERY_SMOKE=HOLD`；需补当前 bundle 的独立真实证据。
- [ ] `GO_RUNTIME_FEDERATION=HOLD`；三通道、共享合同、持久恢复和前端全量验收未同时通过。
- [x] commit `9fd5626` 已为真实用户旧会话 `ui-session-0` 实现不删除用户历史的有界分页；focused 验证与真实 SQLite 的 15,341 frame、20 页逐字节匹配均通过。
- [x] 实际客户端 session `88583` 已经由 Task 3 REST/SSE cursor 读回 `ui-session-0` 旧流式回复和 248 条可见 timeline items，未再出现 too-large/“等待本地服务”；真实 GUI 历史重启读取复验通过，但不代表新模型请求、取消或整体 GO。
- [x] `manual_hold` guard 独立复审通过后，根 repository API 已精确 hold 31 个 DeepSeek 历史 turn：20 queued、11 retryable、覆盖 3 个 session；前后全部 target row hash 一致，235 turns / 248 messages / 22,073 events 总量不变，重开 repository 后 31 个 hold 保留。
- [ ] 当前客户端数据库为 DSH `ready`、`cloud_running=0`、`active_holds=31`；Provider 主密码框仍待用户解锁，新会话模型发送尚未实测。DSH proof 已于北京时间 19:25:13 按既定 TTL 到期且不延长，精确 Provider/Model attestation 与相关 Gate 继续 `HOLD`。

回归记录：Task 5 review focused 为 `19 passed, 3 skipped`；从 revision `cb40882` 启动的唯一
一次全量 Python `pytest -q` 在 898.60 秒上限中断，当时 `17 passed, 2 skipped`，
停留于 Development Graph blueprint 内嵌前端流程。该结果不是全量 PASS，也不覆盖
随后进行的事件分页改动；按约束不重复运行另一轮全量。

验收台账内部用 `live_acceptance_pending` 表示尚未人工执行（它不是公共 API 错误码）；
前端选择/恢复冲突沿用 `runtime_selection_conflict`。Provider/Grant/Host 的实际失败
继续使用设计中既有公开分类，不得用 fixture 成功覆盖。

## 机器验收

每个 Task 均先提交失败测试再实现。测试必须使用随机测试凭据或本地 mock provider；真实用户凭据只从 Vault 解析，不进入仓库、测试输出或证据文件。P0/P1/P2 完成前不执行广泛安全扫描，本批只检查功能合同中明确禁止的凭据载体。
