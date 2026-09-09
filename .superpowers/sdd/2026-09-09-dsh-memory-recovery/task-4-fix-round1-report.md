# Task 4 review fix round 1

日期：2026-09-09。基于 Task 4 `258f749`，仅修改 Task 4 owned UI、验收脚本、测试与报告；没有修改已审 Task 3 服务/注册模块。

## 修复与证据

1. 新建配置 version 0 原来用缺省值覆盖 read-only、budget、anchors。现在仅有持久化版本时加载这三个配置项，version 0 保留用户草稿。真实浏览器选择 read-only / 5200 / 两条锚点后创建 session，再保存；逐项核对页面和真实配置。RED fixture `/Users/sushi/dsh-memory-web-FdaR94` 显示 workspace-write != read-only。
2. `memories` 直接返回服务完整记录泄漏正文。现在显式投影 id/version/kind/visibility/summary/sourceRefs/validTime/systemTime/status，`memory` 单条显式读取仍返回正文。真实 HTTP 同时断言列表不含 content、来源/版本未丢、单条正文匹配。RED fixture `/Users/sushi/dsh-memory-web-pjXzAk` 显示 content 存在。两项最初 1 pass / 2 fail，修复后 UI 3/3。
3. 抽出纯 `validateMemoryAcceptance`，依据原生 history 工具块和 turn/end，不相信缓存 calls/turnEnd。要求恰好两次依序 page-in/sandbox-run，无脚本 limit/error，唯一 completed turn 和唯一匹配 sessionId/callId 的 COMMITTED Effect，native-seatbelt/full、exit0、startedtrue，无 cancelled/timedOut/denied/runnerFailure；产物字节不变。13 项正反例使用无密钥原生形状 fixture；宽松旧判定 RED 2/13，新判定 GREEN 13/13。明确覆盖额外工具、顺序交换、重复写、限制触发、弱后端/enforcement、错误退出、错 call/session、额外 Effect、取消与伪摘要完成状态。
4. 页面启动调用固定 `defaults` 业务路由，经真实 native `apiProxy.host.describe` 取得绝对 cwd，预填 workspace。项目与会话保持空，不生成伪数据。未实现时 RED：HTTP 404 与 browser 空 workspace（1 pass / 2 fail），修复后真实 API/browser 均通过。

## 最终验证

Node `/Users/sushi/.nvm/versions/node/v22.20.0/bin/node`，login:false。测试均为真实独立 native Web/dataRoot/workspace/空闲端口；HTTP 调实际业务服务，浏览器独立 persistent profile。

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test apps/dsh-agent/tests/memory-recovery-ui.test.mjs apps/dsh-agent/tests/memory-acceptance-result.test.mjs
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test apps/dsh-agent/tests/*.test.mjs
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node apps/dsh-agent/tests/fixtures/memory-reopen.mjs /Users/sushi/dsh-memory-live-c5RjwQ
```

- 定向 **16/16 pass**；最终全 app **102/102 pass，0 fail / 0 skip**（UI 3、纯判定 13、其他 86）。保留 SQLite ExperimentalWarning；Seatbelt 越界 EPERM 为预期负向。
- 最终全 suite：HTTP `/Users/sushi/dsh-memory-web-OSdMtX`；双时态 `/Users/sushi/dsh-memory-web-FY3kUr`；browser `/Users/sushi/dsh-memory-web-mylvC0`，session `session-b48a0d1d-8af7-40a3-ac84-e97eec3b9ccc`；sandbox `/Users/sushi/dsh-task-sandbox-loEgBI`。
- 唯一既有真实模型任务 `/Users/sushi/dsh-memory-live-c5RjwQ/acceptance-report.json` 用新纯函数重新校验 PASS。真实 native cold reopen 再次完整通过，改为先查摘要再显式读正文，其余配置/账本/产物/唯一 completed turn/idle/Vault locked/浏览器最终文本断言保留。**没有新模型调用或历史任务重放**。
- `node --check`、`git diff --check` 通过。没有删除任何文件、夹具或 browser profile。

## 可操作手工环境

仅终止此前自己启动的 64356 父进程并确认退出，再按原命令重启同 dataRoot/端口。当前 URL `http://127.0.0.1:64356/memory-recovery`，dataRoot `/Users/sushi/dsh-memory-live-k6bzYi`，workspace `/Users/sushi/dsh-memory-live-k6bzYi/workspace`，overlay `/Users/sushi/dsh-memory-live-k6bzYi/local-model.patch.json`，默认 provider `local-acceptance` / model `qwen3.8-27b-uncensored-mlx`。不自动建会话或请求模型。重启命令完整保留在正式验收报告。

独立 `/Users/sushi/dsh-memory-live-k6bzYi/manual-review-browser-profile` 实测页面 workspace 预填真实目录，project/session 为空，截图 `manual-default-workspace.png` 已目视检查。服务保持运行，controller 负责打开用户页面。

README 的 controller 已暂存 hunk 与原 4 行未暂存内容均未改动，提交显式限定 owned paths，排除 README；root 设计文档/旧未跟踪报告未操作。3080/3188、旧会话/Vault、LM Studio 全局配置均未触碰。原支持边界保持，不增加任意命令/API 派发或沙箱能力承诺。
