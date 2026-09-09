# Task 1 Fix Round 1 报告

日期：2026-09-09

## 结论

Task 1 独立审查确认的三项 Important 已在本轮修复：episodic 来源组改为有界的精确双向/传递闭包；`sourceRefs` 改为非空的 event/operator 判别联合并核验事件来源；semantic 双时态查询改为先选择最大已生效 valid time、再选择该有效时间最新录入版本。

本轮仍只修改 Task 1 所属模块、测试与任务报告；未修改或删除其他文件、目录。

## 1. Episodic 配对闭包

根因：旧实现只扫描 `list(..., { kind: 'episodic', limit: 1000 })` 建来源组。result 在窗口内、call 在窗口外时只返回 result；call 被显式 page-in 且 result/依赖也在窗口外时，反向依赖同样无法发现。

修复：

- 正向来源按确定性 `event:${sessionId}:${seq}` 通过 `store.get` 精确读取，不依赖最近窗口。
- 反向来源对当前可见 episodic 最新版本使用 SQLite JSON 来源查询，以 event project/session/agent/seq 精确匹配。
- 以 BFS 求双向、传递闭包；page-in 的真实正文页在组中优先于摘要句柄。
- `MAX_LINKED_GROUP = 1000`；单次反向查询使用 `LIMIT 1001` 检测溢出。闭包超限或任一正向来源没有当前 scope 可见的 episodic 投影时，整组不进入上下文。
- 正常 `MemoryStore.list`、`MemoryPager.search` 上限未扩大。

TDD：

- RED A：pager 5/7 pass；窗口外 call 未随 result 返回，缺投影来源仍错误保留 result。
- GREEN A：pager 7/7 pass。
- RED B：补充反向/传递窗口反例后 pager 7/8 pass；page-in call 返回空组而未找到窗口外 result。
- GREEN B：pager 8/8 pass。

## 2. `sourceRefs` 判别联合

`putMemory` 现在要求 `sourceRefs` 为非空数组，每项只能是以下完整来源之一。

事件来源：

```js
{
  type: 'event',
  projectId: string,
  sessionId: string,
  agentId: string,
  seq: nonNegativeSafeInteger
}
```

写入时必须在 `memory_event_sources` 找到完全匹配的持久来源，且 `memory_id` 非空（即存在获准 episodic 投影）；record owner scope 必须与 event project/session/agent 完全一致。不存在/无投影抛 `MEMORY_SOURCE_NOT_FOUND`，对当前 owner scope 不可见抛 `MEMORY_SOURCE_NOT_VISIBLE`。

人工定义来源：

```js
{
  type: 'operator',
  operatorId: string,
  reason: nonEmptyString
}
```

仅当调用参数 `actor` 为 `{ id: operatorId, role: 'operator' }` 时接受；模型不能把字符串或伪造 operator 来源当作历史事件。人工首创的 semantic/procedural 定义应使用此类型，后续 Task 3/4 必须由可信 operator 路径构造 actor 与 sourceRef，不能直接信任模型或 HTTP body。

`ingestEvents` 产生的 episodic event 来源已规范化为完整 event 形状；顶层 `sourceEventSeqs` 中每个关联 seq 与事件自身 seq 都生成独立来源项。

TDD：来源契约 RED 为 store 7/8 pass（空来源被接受）；GREEN 后完整 focused 为 15/15 pass，覆盖空来源、残缺来源、不存在事件、跨 scope 事件、模型冒充 operator、人工定义和真实事件来源。

## 3. Semantic 双时态选择

根因：旧窗口函数在满足 `valid_time <= validAt`、`system_time <= systemAt` 后仅按 `system_time DESC` 选版本。晚录入的 valid=10 更正会在 validAt=25 时错误遮蔽 valid=20 的已生效事实。

修复排序：

```sql
ORDER BY valid_time DESC, system_time DESC, version DESC
```

含义：先选查询时点已经生效的最大 valid time；同一 valid time 有多个知识版本时，再按 system time 选择截至系统时点最新可知的更正。

TDD：valid=10、valid=20、后录入 valid=10 更正的 RED 为 store 7/8 pass，实际错误选 version 3、预期 version 2。GREEN 同时验证：

- `validAt=25` 选择 valid=20/version 2；
- `validAt=15, systemAt=更正前` 选择原 valid=10/version 1；
- `validAt=15, systemAt=更正后` 选择更正 valid=10/version 3。

## 验证与已知提示

最终 focused 命令：

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test \
  apps/dsh-agent/tests/memory-store.test.mjs \
  apps/dsh-agent/tests/memory-pager.test.mjs
```

最终结果：16/16 pass，0 fail。`node:sqlite` 的 `ExperimentalWarning` 仍按绑定要求原样输出，未隐藏或替换底座。
