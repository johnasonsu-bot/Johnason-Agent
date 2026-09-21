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

随后依次运行 `verify_deepseek_harness_source.py --write-manifest`、`build_python_term_gate_manifest.py`，并重新执行 DSH verifier。结果为 `GO_DSH_SOURCE_READY`，摘要 `ee0dfd77cc1801be1861ff2a2d2723cf317e3b97cfc05e9ed440bd70acff8a82`，范围明确是 `source_build_provenance_only`。Python gate manifest 已重生成（163 个 generated files、7 个 build inputs），这不等于完整 Python Term gate 或官方原生运行验收。

官方运行时的冻结依赖安装、native/Web 构建和完整原生验收仍待用户确认自动清理范围；没有执行这部分构建。原工作目录及运行版本未切换。
