# 2026-09-08 原生浏览器实测

临时服务 `http://127.0.0.1:3188`，数据根 `/tmp/dsh-native-web-2HKAPL`；正式用户目录未初始化。本文件不包含真实密钥、密码或私有文件内容。

1. `/vault` 页面可见首次创建密码库、二次确认、返回原生聊天入口。初始化由测试 API 使用虚构密码完成，未让用户输入；返回 initialized=true / locked=false。
2. 原生聊天 UI 可见内测声明、DeepSeek 可稍后配置、设置/模型/插件/Agent预设。未选择 workspace 时输入禁用。
3. 浏览器 Settings → 模型 → 添加自定义提供方，保存 `local-acceptance` / `LM Studio 本地验收`，`http://127.0.0.1:1234/v1`，协议 openai-completions，模型 `qwen3.8-27b-uncensored-mlx`，使用无授权能力的本地占位值。页面显示“API 密钥已配置”，模型菜单真实出现 Qwen 本地模型。
4. 点击“添加工作区”确实拉起本机 chooser 子进程；自动化工具无法选取该进程窗口。随后系统对话框返回一个实际目录并创建空会话，证实选择结果能投射到 UI；该目录未发送任何请求、未读取文件。
5. 为保护实际工作目录，通过原生 POST /api/workspace.create 注册隔离目录 `/tmp/johnason-dsh-acceptance-workspace-oBHBD6`（规范化 /private/tmp），workspaceId `8d50001b-289e-4582-b9cc-c2dc6cfed3c5`；浏览器实时出现并成功选取，输入解锁。
6. 从浏览器发送“约200字中文小说 story.md + 纯 HTML/CSS animation.html + 工具验证非空”的真实任务。当前进行中，可见“停止生成”和原生上下文条目；不能据此标记任务通过。

原生测试会话目录：`sessions/--private-tmp-johnason-dsh-acceptance-workspace-oBHBD6--/session-3957ab55-f789-4d6b-9a72-d3112b636844/`。

7. 首轮本地模型流式内容确实返回，但首 token 3m55s；7m29s 时仍在推理、尚无文件。通过浏览器“停止生成”主动取消，立即显示“已停止”，发送框恢复。D11 不标通过；不是模型完成。
8. 运行中刷新页面，仍能看到相同会话、原始消息、流式状态、停止按钮和本地模型选择，未重新创建任务。
9. 在同一会话追加“创建 ready.txt → 读取 → 回复文件名与 DSH_NATIVE_READY”，第二轮真实完成：浏览器显示 Write ready.txt、Read ready.txt，最终回复 `ready.txt — DSH_NATIVE_READY` 与可点击产物路径。磁盘复核文件存在且内容正确。原生统计用时1分18秒，首token33秒，8.2 tok/s。D3中断后继续与D4真实写读已有证据；不代表全部计划用例通过。
10. 第三轮从同一会话请求短版故事+不超过50行HTML；长时间仍只有推理，未观察到工具写入。随后点击停止时页面返回 `Failed to fetch (internal)`。只读检查确认临时3188已无监听，原PID42476/42834已退出，原终端session56658不可读；没有足够退出日志归因，不能认定模型超时或运行时崩溃。正式3080仍HTTP200，LM Studio1234仍监听。磁盘只有已验证的ready.txt，D11未完成。
11. 控制器以最新已审查代码从自己的终端重新启动3188，同一临时dataRoot、exec session47792；不迁移/删除数据。浏览器刷新后原工作区、同session、用户三轮消息、ready.txt工具记录及本地模型选择全部保留；第三轮显示“已停止”，无进行中标识或自动续跑。该观察证明此次非预期服务退出后的持久记录可恢复，不证明退出原因或exactly-once。Vault默认重锁，未启动新模型请求。

## 正式用户入口

控制器于Task3修复复审通过后启动 `http://127.0.0.1:3080/vault`，数据根 `/Users/sushi/.johnason-dsh`，exec session85574。真实status为 initialized=false / locked=true，页面首次初始化表单已打开并保留。用户被请求亲自初始化主密码、在原生Models保存云端Key；没有将旧凭据或临时测试密码导入正式目录。
