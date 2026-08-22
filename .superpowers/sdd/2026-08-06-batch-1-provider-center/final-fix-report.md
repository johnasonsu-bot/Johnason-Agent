# Batch 1 Provider Center final fix report

## 结论

Batch 1 最终评审中的 3 条 Critical、7 条 Important 和 2 条顺带 Minor 均已在实现提交 `e0af38649c1917a9ffce06c06f9fb59d0d8f8101` 中处理。完整 Python 套件、Electron 套件及真实 UI-to-real-Workbench 验收全部通过；高置信凭据、运行时生成值、SQLite 元数据、浏览器存储及测试产物扫描未发现明文泄漏。

## 发现与修复映射

| # | 级别 | 修复 | 主要代码 | 验证 |
|---|---|---|---|---|
| 1 | Critical | Electron 主进程生成每次启动唯一的 256-bit capability 和实例 ID，启动由子进程自行绑定的随机端口；token 仅通过 stdin bootstrap 传递，renderer 不可见。后端验证 capability、loopback Host 和服务身份。供应商 protocol/authority 变化先使旧凭据失效，再轮换 opaque `secret_id`。 | `mvp/canvas-spike/src/main.ts`; `mvp/src/workbench/main.py`; `mvp/src/workbench/api/app.py`; `mvp/src/workbench/api/providers.py`; `mvp/src/workbench/providers/repository.py` | `tests/unit/api/test_security.py`; `canvas-spike/tests/security.spec.ts`; authority-rotation API tests |
| 2 | Critical | 同一 vault 路径共享进程级 `RLock`，所有内存与磁盘状态迁移均受保护；解锁全生命周期持有非阻塞 OS 单写者锁（POSIX `flock` / Windows `msvcrt`）。API 将竞争稳定映射为 423。 | `mvp/src/workbench/credentials/vault.py`; `mvp/src/workbench/credentials/service.py`; `mvp/src/workbench/api/providers.py` | 不同 provider write/write、write/delete、lock/write 的确定性测试；真实 subprocess 单写者测试；API 423 测试 |
| 3 | Critical | Electron 完整拥有 backend 的生成、身份检查、关闭和强制终止；窗口关闭、renderer crash、app quit 均触发 vault lock 与进程回收。FastAPI lifespan 在 provider close 失败时仍于嵌套 `finally` 锁 vault。 | `mvp/canvas-spike/src/main.ts`; `mvp/src/workbench/api/app.py`; `mvp/src/workbench/models/gateway.py` | renderer crash 后重启并成功解锁；正常 restart 验收；shutdown failure tests |
| 4 | Important | Playwright 只 fake LM Studio 上游；Workbench 后端、SQLite、vault 和 Electron 都是真实临时实例。UI 覆盖 create/unlock、CRUD、discovery/test/default、enabled、reasoning、authority rotation、lock/restart、recovery。 | `mvp/canvas-spike/tests/providers.spec.ts`; `mvp/tests/acceptance/test_batch1_provider_center.py` | 真实 lifecycle Playwright 用例及 Python batch gate |
| 5 | Important | `ModelMessage` 改为禁止额外字段的类型化消息，continuation 存入 Pydantic `PrivateAttr`；只有由真实 `ModelResponse` 构造的 assistant turn 才会由 DeepSeek adapter 转换为 `reasoning_content`。其他 adapters 只序列化公开字段。 | `mvp/src/workbench/models/contracts.py`; `deepseek.py`; `lmstudio.py`; `openai_compatible.py` | 实际首轮 DeepSeek response 到第二轮 tool continuation；公开 dict 注入拒绝；序列化不泄漏 |
| 6 | Important | create 使用 fsync 完整临时文件加 no-overwrite 原子发布，不再创建空占位；持久化错误携带 committed 状态；启动结构校验识别 incomplete/corrupt；显式 recovery 保留损坏副本；所有临时文件均在 `finally` 清理。 | `mvp/src/workbench/credentials/vault.py`; `service.py`; vault recovery API/UI | 无 placeholder、pre-publication cleanup、committed/uncommitted、recovery tests；真实 UI recovery |
| 7 | Important | 连接测试先发现模型，再选择已保存 default 或第一个模型执行 completion；LM Studio adapter 解析 profile alias；UI 对默认模型保存提供确定完成状态。 | `mvp/src/workbench/api/providers.py`; `mvp/src/workbench/models/lmstudio.py`; `ProviderCenter.tsx` | API 路径测试、alias 单测、真实 LM Studio 上游请求断言 |
| 8 | Important | `enabled` 全链路持久化并由 gateway 强制执行；UI 提供开关且 disabled 时禁用测试/发现。DeepSeek thinking 由 model validator 强制开启，reasoning effort 仅允许 `high`/`max` 并在 UI 可选。 | `profiles.py`; `gateway.py`; provider API/UI | profile/API/gateway/UI persistence and enforcement tests |
| 9 | Important | IPC 校验唯一主窗口、mainFrame 与精确本地文档 URL；拒绝新窗口、webview、外部 navigation/redirect；CSP 禁止 renderer 网络连接。 | `mvp/canvas-spike/src/main.ts`; `mvp/canvas-spike/index.html` | 带同一 preload 的不可信 frame 无法调用 API；navigation/window/CSP Playwright tests |
| 10 | Important | `ModelGateway.aclose()` 对共享实例只关闭一次、尝试关闭全部 provider，并用 `ExceptionGroup` 聚合失败；lifespan 无条件锁 vault。 | `mvp/src/workbench/models/gateway.py`; `mvp/src/workbench/api/app.py` | aggregate-close 与 lock-on-close-failure tests |
| M1 | Minor | README 改为 Electron-owned 随机端口、capability、加密 vault 与重启默认锁定的真实运行说明。 | `mvp/README.md` | 文档审阅及 secret scan |
| M2 | Minor | Provider 选择按钮增加 `aria-pressed`；LM Studio 凭据状态显示 `not_required` / “无需凭据”。 | Provider Center/API | API 与 Playwright assertions |

## TDD 证据

以下为本轮实际记录的 RED → GREEN 过程；实现前失败均由对应缺口触发，修复后使用同一 focused scope 回归。

| 范围 | RED | GREEN |
|---|---|---|
| Vault 并发、原子创建与恢复 | `pytest tests/unit/credentials/test_vault.py`：新增场景初次 6 failed；临时移除 OS lock 后 subprocess 用例退出码 17；pre-publication fsync cleanup 用例随后单独失败 | `24 passed in 21.77s` |
| Provider authority、enabled、LM default | focused provider/profile/LM suite 初次 8 failed | `pytest tests/unit/api/test_providers.py tests/unit/models/test_lmstudio.py tests/unit/models/test_profiles.py -q`：`58 passed` |
| Typed DeepSeek continuation | 首次 collection 因 `ModelMessage` 缺失失败 | `pytest tests/unit/models/test_deepseek.py tests/unit/models/test_gateway.py tests/unit/models/test_lmstudio.py -q`：`21 passed` |
| Gateway/lifespan shutdown | 两个新增 shutdown 用例初次 2 failed | aggregate close 与 unconditional vault lock 均通过，并包含于全量套件 |
| Backend capability/identity | 初次因 `AppSettings` 不接受 capability/identity 失败；spawn 用例初次无合法 handshake | `pytest tests/unit/api/test_security.py -q`：`2 passed` |
| IPC/navigation/CSP/lifecycle | 最初 3 个 Electron 安全用例分别命中 fixed-port、untrusted IPC 和 CSP 缺口 | 当前 Electron security 5 个用例全部通过 |
| 跨平台 Python 默认路径 | 打包契约初次缺少 `.venv/Scripts/python.exe`，`1 failed` | 同一 focused Playwright test：`1 passed`，并纳入全量套件 |
| Vault single-writer API | 第二实例 unlock 初次抛出未处理的 `VaultInUseError` | 同一 API 用例返回 423，`1 passed` |
| 真实 Batch 1 验收 | 原 gate 仍断言 fake backend；真实 UI 首轮暴露 enabled/recovery 缺口及 response-only 字段回传导致的 422 | 独立 gate `2 passed`；真实 lifecycle 与 recovery Playwright 均通过 |

## 最终验证

- Python 全量：`cd mvp && .venv/bin/python -m pytest -q` → `171 passed, 4 skipped, 1 warning in 48.52s`。
- Batch 1 独立验收：`cd mvp && .venv/bin/python -m pytest tests/acceptance/test_batch1_provider_center.py -v` → `2 passed in 6.95s`。
- Electron 全量：`cd mvp/canvas-spike && npm test` → build 成功，`11 passed in 18.3s`。
- Patch hygiene：`git diff --check` 与 staged diff check 均通过。
- Source/report 高置信 secret scan：private-key、AWS、Google、GitHub、Slack、OpenAI-style token patterns 均无匹配。
- Runtime scan：所有 Playwright runtime 二进制/SQLite/vault 产物未命中本轮 UUID 生成值模式或高置信 token patterns；7 个临时 SQLite 的 provider metadata 敏感字段计数均为 0；测试产物中无日志文件。
- Browser storage/DOM：真实 UI 验收断言 `localStorage.length === 0`、`sessionStorage.length === 0`，且 DOM 不含本轮生成的 password/credential。

## 提交与剩余关注项

- 实现提交：`e0af38649c1917a9ffce06c06f9fb59d0d8f8101` (`fix: harden provider center lifecycle`)。
- 未使用任何真实 API key；按原计划 live DeepSeek 网络调用仍需用户自行输入凭据，当前由 mock transport 完整验证请求形状、认证头和 continuation。
- 当前验证主机为 macOS。Windows 默认 Python 路径已有构建契约，Windows vault lock 分支已实现，但仍建议在 Windows CI 上执行真实 lifecycle 回归。
- 保留两项非阻塞工具链 warning：Starlette TestClient/httpx2 迁移提示，以及 Vite native config loader 的 ESM 提示。
- Reviewer 明确 defer 的 malformed DeepSeek SSE/JSON/tool-argument normalization 仍留待 Batch 2；公开 response 已移除 private reasoning trace。
