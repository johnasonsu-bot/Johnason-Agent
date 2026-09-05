# DeepSeek Harness 云端人工验收

状态：**MANUAL_PENDING / NOT_RUN**。按用户要求转为人工触发，不等待凭据、不阻塞 Task 4 开发，不计为 `GO_DSH_PLUGIN_SMOKE` 或 `GO_RUNTIME_FEDERATION`。

## 输入

| 输入 | 当前值 / 取得方式 |
|---|---|
| Runtime | `dsh`，使用正式模型 Host 入口 |
| Provider Profile | `deepseek-primary`，已保存于用户 Workbench 数据库 |
| 模型 | 该 Profile 的 `default` 别名；界面显示保存值，后台读取并冻结实际配置 |
| API 地址和 API Key | 从已保存的 Profile 和加密 Vault 读取；不在聊天中传入 Key |
| Vault 解锁密码 | 在客户端密码框隐藏输入；不是 DeepSeek API Key |
| 网络 | 可以访问该 Profile 配置的 DeepSeek 云端 API；会产生一次实际模型调用及相应费用 |

如果需要更换 API Key，先在客户端的模型供应商设置中更新并保存；不要把 Key 或 Vault 密码发送到聊天中。

## 客户端录入（替代命令行）

入口：顶部 **模型供应商 → 解锁保险库 → DeepSeek Harness 人工验收**。

1. 使用已有主密码解锁保险库。需要更新 API Key 时，使用供应商表单中的 API 密钥框保存。
2. 选择已保存且启用的 DeepSeek 供应商，确认显示的默认模型。换模型需先修改并保存供应商配置。
3. 在人工验证区输入与上方解锁相同的保险库密码。外部验证进程只读解密当前凭据快照，不获取主程序的写锁，不需要先锁定主程序。
4. 点击 **开始真实验收** 才会发起真实模型请求；密码提交后立即从表单清空。无需打开终端。
5. 查看运行状态和结果；不想继续时点击 **取消验收**。单次最多运行五分钟，超时不会记为成功。

**测试连接**只检查供应商接口；**开始真实验收**才验证 Harness 执行链。两者不能替代。

这是一次人工发起的实际云端验证，经过固定 DeepSeek Harness 模型 Host、正式准入、Vault/私有 Grant 和事件执行链；不会用 fixture 响应替代真实失败。当前只验收模型请求，不能据此宣布工具、Skill、Workspace 或整个联邦引擎全部通过。

## 结果判定

- 成功：界面显示 `succeeded`；后台外部验证退出码为 `0`，独立目录中生成签名清单和 `runtime-live-evidence-dsh.json`，证据绑定 `dsh`、当前模型/构建、`endpoint_kind=cloud` 和 `terminal=completed`。
- 失败：界面显示 `failed`；保留输出目录，按 Profile、Vault 解锁、网络、模型兼容性和当前构建排查，不把失败记成通过。
- 超时或取消：显示 `timed_out` 或 `cancelled`，不授予验收通过。
- 未运行或未完成：维持 `MANUAL_PENDING / NOT_RUN`。

界面只触发生成独立验证证据，不覆盖原有会话，也不会自动把证据部署到正在运行的客户端。要让会话下拉实际开放 DSH，还需要应用加载对应证据、配置正式模型 Host 并重新检查准入状态；该步骤与前端手测环境接续。证据有有效期，过期或源码/构建变化后需要重新人工验证。

本轮开发和界面交互测试**尚未执行 DeepSeek 云端调用**，真实结果以用户点击后的本次验证为准。Python Term 与 Goose 的真实 LM Studio 验收结果另见 Task 3 fix2 报告，不能替代本项。

## 同密码验收失败的修复与诊断

旧实现中，客户端解锁后持有跨进程独占写锁，外部验收又尝试以可写模式解锁同一保险库，导致在模型调用前失败。这是进程访问冲突，不是另一套密码。修复后外部验收只读解密，主程序的写入锁机制不变。

失败结果现在展示白名单诊断代码：

- `vault_unlock_failed`：保险库密码校验失败。
- `vault_in_use`：保险库进程锁冲突，不代表密码错误。
- `provider_request_failed`：模型 API 请求失败。
- `runtime_build_unavailable`：本地运行时构建不可用。
- `runtime_verification_failed`：执行或验收证据校验未通过。
- `verification_process_failed`：外部验收进程未正常完成。

旧版本的历史失败没有保存这些分类，不能事后凭通用提示判断密码是否正确。未知错误不再统一建议修改密码；请保留验收编号与诊断代码。
