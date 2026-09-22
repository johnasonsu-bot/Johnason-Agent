# 平台/插件隔离：验证记录

## 已完成源码改造

基于升级提交 `4c63e51`，隔离 20 个本地业务模块并保留原公开入口；加入平台装载/Profile SDK、本地插件注册表、生命周期、升级边界审计和共享 release 策略。Workbench 使用版本化 adapter 与统一组件清单。官方子模块未修改，无原有跟踪文件缺失。

细节与待讨论选项见 `docs/architecture/dsh-platform-plugin-boundary.md`。旧记忆日志迁移仍是启用阻断；本轮代码隔离不修改真实会话或审计证据。

## 源码验证

| 检查 | 结果 |
|---|---:|
| App 业务、插件、平台及源码回归 | 91 通过 |
| Launch 指定源码用例 | 11 通过 |
| Workbench platform-contract + source-runtime | 15 通过 |
| Python source_gate + prompt_sections | 56 通过 |
| 合计 | 173 通过 |

`node apps/dsh-agent/scripts/check-platform.mjs` 通过：官方 revision/version/packageManager、HEAD gitlink 与依赖边界匹配。JS/Python 构建库存逐项一致，包含 14 个源码输入和 10 个产物。`git diff --check` 通过。

独立审查确认：保持旧插件公开入口后，既有 profile name guard 保持兼容；Grant 消费/确认、取消等待及凭据释放未因 adapter 注入而绕过；原严格 gitlink 与构建 receipt 校验仍保留。

源码测试使用临时夹具和纯源码契约，不启动真实模型或业务服务。阶段日志：`/tmp/dsh-isolation-source-all.log`、`/tmp/dsh-isolation-sidecar-final.log`；Python 使用原工作目录的现成 venv 执行，仅加载候选源码。曾误选原生 task-sandbox 测试，因尚无官方 Cordis 构建依赖在模块加载时失败；该项没有计入通过数，需实际构建后运行。

## 构建状态

无需删除文件的 Sidecar 构建已执行：在 `mvp/sidecars/deepseek-harness` 运行 `node scripts/build.mjs`，退出 0；生成 10 个可执行模块/入口和真实 `build-receipt.json`。

原生兼容修复后再次运行 Sidecar 构建，再依次运行 `verify_deepseek_harness_source.py --write-manifest`、`build_python_term_gate_manifest.py`。结果为 `GO_DSH_SOURCE_READY`，摘要 `67ae0222ab8aee5c2f6c8ed8ee53e5ff1cb7739058b9d12db2b9a7d2aa84dbea`，范围明确是 `source_build_provenance_only`。Python gate manifest 已重生成（163 个 generated files、7 个 build inputs），这不等于完整 Python Term gate。

用户接受方案后，在候选运行 `node apps/dsh-agent/scripts/build.mjs`，退出 0。实际完成 `pnpm@11.7.0 install --frozen-lockfile`、macOS ARM 原生 `system.node`、Host/Client 库及 Web 构建。官方记录了 248 个 client artifacts，子模块源码仍干净。`doctor` 和 `check:platform` 均通过。原工作目录及运行版本未切换。

## 构建后集成验证（2026-09-22 续）

- Sidecar 全量 60 项通过，包含真实本机模拟 provider、native tools、NDJSON 入口、Grant/stream/cancel/error 收尾。
- Python 相关单元与跨进程回归 1,145 项通过；另有 5 项依赖 Goose release 二进制，明确排除。首次并行编译时有 1 项 cancel_waiter 超时，独立复现及完整确认轮均通过，未改断言或超时。
- Source gate acceptance 3 项和 Python → DSH `dsh-built` 1 项通过。
- App 全量 197 项：196 通过、0 失败、0 超时、1 跳过（真实 Data Platform/Forge 环境集成）。覆盖记忆运行时、真实沙箱、Web 启停、端口冲突、本地 MCP/Skill、Vault 重锁、正常重启、真实 Chrome 刷新及记忆 UI。

真实构建暴露并修复了源码 mock 未覆盖的 API 变化：文件 profile 层将模块路径规范化为 file URL；Session 改用 `snapshotEvents()`；sandbox `confine` 改为异步；DeepSeekAdapter 增加 `prepareExtensions`；原生运行时只从受支持的 profile 作用域解析本地浏览器包，前端依赖改为实际提供 slots 的 `dsh-client-ui-renderer`。新增/更新的回归均先观察失败后验证修复。唯一加密 provider、必需事件/hash、准备阶段取消不启动命令等约束保留。

截图检查发现仅恢复记忆按钮还不够：没有 Workspace 成员关系的会话不能编辑聊天输入。平台现在用原生 controller 创建关联工作区的新会话；显式配置现有未开始会话时只添加成员关系，不重建会话或改写历史。按会话互斥并在关联后重新核验版本/运行状态，关联失败不追加配置。真实浏览器检查成员关系、输入可编辑、填入草稿后清空且不提交模型；只读配置及历史拒绝保持原生 Workspace baseline 不变。

日志位于候选 `.vendor/dsh-build/`。App 首轮 138 pass / 51 fail / 1 skip；修复首批接口后第二轮 185 pass / 3 fail / 3 cancelled / 1 skip。Workspace 验收首轮 194 pass / 2 fail / 1 skip 的两项失败来自“冷启动 registry 必为空”的错误夹具假设，已改为前后 baseline 不变。以上均保留为中间结果。最终日志为 `logs/app-full-20260922-complete.log`，197 项在 46.6 秒内完成。端口超时定向复跑 1 秒通过，技能夹具使用独立 Git 根，避免候选内 TMPDIR 使发现逻辑上溯到父仓库。

App 最终命令为在 `apps/dsh-agent` 执行 `node --test --test-concurrency=1 tests/*.test.mjs`，显式取消真实业务测试环境变量，使用现成 Chrome 和本轮临时目录。官方构建日志为 `logs/native-build-20260921.log`；Python/Sidecar 的实际命令、完整原始输出及首次失败/确认结果保存在 `python-tests/logs/verification-summary.json`。截图保留在 `/Users/sushi/dsh-memory-web-VwiQdp/native-memory-composer.png` 和 `/Users/sushi/dsh-memory-web-W0XZ1l/native-selected-chat.png`。

最终保留检查：基线 `4c63e51` 的 890 个跟踪路径均存在；原主目录保持 `a76a9ff`，旧独立工作树保持 `6a40943`，两者都干净。

独立审查未发现阻断问题，覆盖凭据路径白名单、required 事件/hash、异步沙箱取消、官方空扩展回调，以及 Workspace 互斥、失败不提交、旧会话身份/历史保留和 headless 可选服务。源码隔离提交为 `90ee3ad`，构建后兼容修复为 `f5c682c`；构建证据单独提交。

验证不调用真实模型或业务服务。真实 Forge/Data Platform 环境集成显式跳过；macOS 以外平台、生产切换和旧数据迁移均未执行。
