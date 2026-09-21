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

待用户确认官方构建的具体生成物/临时文件自动清理范围后执行冻结安装和构建。当前尚未执行本轮原生或 sidecar 构建、生成 receipt/manifest 或完整原生验收；保留的旧产物不能用于证明本轮代码已构建。原工作目录及运行版本未切换。
