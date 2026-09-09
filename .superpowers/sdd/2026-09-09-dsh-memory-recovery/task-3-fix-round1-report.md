# Task 3 Fix Round 1

日期：2026-09-09。

已修复唯一 Important：大结果摘要把展示形式 `event:<session>:<seq>@1` 错当成 `memory_page_in.id`，而实际 id 不带版本后缀。

现在摘要直接提供可调用参数，例如：

```text
Use memory_page_in({"id":"event:new-session:2","version":1}) for a bounded original page.
```

生产修改只涉及摘要格式：id 与 version 分开，使用 JSON.stringify 生成实际 JSON 参数。不改变原始记录、预算、工具 schema、范围或原生执行流程。

回归测试不再从另行检查的 raw event 手工拼正确 id。它读取真正 native result 摘要，提取公布的参数并通过真实 ToolRuntime 调用 `memory_page_in`，验证 selected=true、正文进入真实 native derived messages 且连续投影仍正确。

TDD：测试先按旧摘要实际 advertised id 调用，RED 为 `memory was not found or is not visible`（isError=true），不是解析失败。修正摘要后 GREEN。

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test \
  apps/dsh-agent/tests/memory-runtime.test.mjs \
  apps/dsh-agent/tests/profile.test.mjs
```

最终 15/15 pass、0 fail、0 skip；修改模块 node --check 和 git diff --check 均退出 0。SQLite ExperimentalWarning 继续原样输出。最终摘要回读 fixture `/Users/sushi/dsh-memory-runtime-g3Vyxz`；RED fixture `/Users/sushi/dsh-memory-runtime-58rgEV`，均保留。

只改本轮所属生产模块、测试与本报告；没有新增能力、删除文件或改动其他 Agent 文件。
