# Task 1 报告：三类持久化记忆与分页服务

日期：2026-09-09

## 结论

Task 1 已实现为本地同步领域服务：`MemoryStore` 使用 Node 内置 `node:sqlite`，以 WAL/FULL 和 `BEGIN IMMEDIATE` 事务持久化三类记忆、事件来源和游标；`MemoryPager` 默认仅返回摘要句柄，只有显式 `pageIn` 才把有界正文放入工作集；`record-sanitizer` 跳过 Vault/credential 事件并显式记录可识别脱敏路径。focused suite 最终 12/12 通过。

未修改 `third_party`、profile、UI 或 README；未删除测试临时目录或其他文件。

## 文件

- `apps/dsh-agent/src/memory-store.mjs`
- `apps/dsh-agent/src/memory-pager.mjs`
- `apps/dsh-agent/src/record-sanitizer.mjs`
- `apps/dsh-agent/tests/memory-store.test.mjs`
- `apps/dsh-agent/tests/memory-pager.test.mjs`

## 公共 API 与返回形状

所有方法为同步方法，结果均为 JSON 可序列化、与数据库行深度分离的普通对象。

### `MemoryStore`

- `new MemoryStore(path)`：创建父目录、打开数据库、校验 `PRAGMA user_version`，启用 `journal_mode=WAL`、`synchronous=FULL`、foreign keys 和 busy timeout。高于当前版本 1 的 schema 抛 `MEMORY_SCHEMA_UNSUPPORTED`。
- `.db`：公开的 `DatabaseSync` 实例，供 Task 2 在同一数据库增加事务表；生命周期仍由 `MemoryStore` 独占，调用方不得关闭。
- `.close()`：幂等关闭；关闭后的业务调用抛 `MEMORY_STORE_CLOSED`。
- `.ingestEvents({ projectId, sessionId, agentId, events })` → `{ inserted, skipped, cursor }`：
  - `inserted` 是新增 episodic 投影数；
  - `skipped` 包含完全相同的重复来源及受限 credential/Vault 事件；
  - `cursor` 是该 session 已原子确认的最后 seq，尚无游标时为 `null`。
  - 整批先验证，来源/投影/游标在同一事务提交；gap 抛 `MEMORY_SEQUENCE_GAP`，同 session+seq 不同内容抛 `MEMORY_SOURCE_CONFLICT`。
- `.putMemory(record, { actor })` → `MemoryRecord`：追加版本；`id` 省略时生成 UUID；`expectedVersion` 不匹配抛 `MEMORY_VERSION_CONFLICT`。
- `.list(scope, filters = {})` → `MemoryRecord[]`：每个 id 仅返回当前版本，默认上限 100，最大 1000；支持 `kind`、`visibility`、`status`、`limit`。
- `.get(scope, id, version?)` → `MemoryRecord | null`：不可见和不存在均返回 `null`，避免越权枚举。
- `.graph(scope, { validAt?, systemAt? } = {})` → `{ nodes, edges, records }`：按 valid/system as-of 选择每个 semantic id 的版本；edge 形状为原始 `{ from, relation, to, ... }` 加 `{ memoryId, version }`；`records` 保留完整来源和版本审计。
- `.cursor(sessionId)` → `number | null`。

`scope` 固定为 `{ projectId, sessionId, agentId }`。project 记录可被同项目 scope 读取；private 记录还必须同时匹配 session 和 agent。

`MemoryRecord` 形状：

```js
{
  id, version, kind,
  projectId, sessionId, agentId, visibility,
  content, summary, sourceRefs,
  validTime, systemTime, status,
  actor: { id, role }
}
```

写入 actor 必须是 `{ id: nonEmptyString, role: 'model' | 'operator' | 'system' }`；字符串（包括 `'operator'`）拒绝。只有可信调用方传入的 `role: 'operator'` 能确认 procedural 或创建/修改 `content.protected === true` 的规程。模型只能写 candidate，不能通过 actor id 提权。

semantic `content` 为 `{ nodes: [...], edges: [...] }`，每条 edge 至少含 `from/relation/to`。procedural `content` 至少含非空 `rules` 和对象 `applicability`，`status` 仅为 `candidate/confirmed`。

### `MemoryPager`

- `new MemoryPager(store)`。
- `.search(scope, query, { limit } = {})` → `MemoryHandle[]`：query 非空；默认 20、最大 100；匹配摘要/正文，但返回值不含正文。
- `.pageIn(scope, id, { version?, maxChars? } = {})` → `MemoryPage`：默认 4000 chars，最大 100000；加入该 scope 的进程内工作集。
- `.pageOut(scope, id)` → `boolean`：只移出工作集，不删除数据库记录。
- `.select(scope, { query = '', budgetTokens, anchors = [] })` → `{ pages, estimatedTokens, contextText }`：anchors、摘要句柄、真实调入内容和来源标识全部计入预算；估算公式为 `ceil(contextText.length / 4)`。anchors 单独超预算时抛 `MEMORY_BUDGET_EXCEEDED`，不会截断不变量。通过顶层 `sourceEventSeqs` 关联的 episodic 工具调用/结果按原子组纳入或排除，避免拆对。

`MemoryHandle`：`{ id, version, kind, visibility, summary, sourceRefs, validTime, systemTime, status }`。

`MemoryPage`：`MemoryHandle` 加 `{ contentText, truncated }`；不返回未截断的 `content`。

## 持久化与清洗

- `memory_records` 采用 `(project_id, id, version)` 追加版本主键。
- `memory_event_sources` 采用 `(session_id, seq)` 唯一来源主键并保存 canonical SHA-256 指纹；相同 JSON（不受对象键顺序影响）可幂等重放。
- `memory_cursors` 与事件来源、episodic 投影在同一 `BEGIN IMMEDIATE` 事务推进。
- episodic 是 DSH 事件的领域投影，不是第二份可恢复聊天；保留 seq/type/time/data、顶层 `sourceEventSeqs`/surface 元数据及显式 `redactions`。
- credential/Vault 类型事件只确认来源和游标，不保存内容；敏感字段及可识别 Bearer/OpenAI/GitHub/private-key 形状替换为 `[REDACTED]`，路径记录在 `content.redactions`。该规则不声称识别任意秘密。

## TDD 证据

命令均使用：

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test \
  apps/dsh-agent/tests/memory-store.test.mjs \
  apps/dsh-agent/tests/memory-pager.test.mjs
```

RED 1：退出码 1，0 pass / 2 fail；两个测试文件分别因 `memory-store.mjs` 和 `memory-pager.mjs` 不存在而报预期 `ERR_MODULE_NOT_FOUND`。

GREEN 1：退出码 0，11/11 pass。

自审 RED：加入真实 DSH `sourceEventSeqs` 工具配对用例后，pager 为 4/5 pass；失败值只包含 `event:session:1` result，预期同时包含 seq 0 call。

边界 RED：加入非数值 DSH event time 用例后，store 为 6/7 pass；实际错误码为通用 `MEMORY_INVALID_RECORD`，预期在整批验证阶段返回 `MEMORY_INVALID_EVENT`。清洗器改为按 DSH 契约只接受非负 safe integer 时间后转绿。

最终 GREEN：退出码 0，12/12 pass，0 fail，耗时约 70 ms。三个生产模块另经 `node --check`，均退出码 0。

Node 22.20.0 每个引入 `node:sqlite` 的测试进程均打印预期 `ExperimentalWarning: SQLite is an experimental feature and might change at any time`；未隐藏、未当作失败，也未替换为其他底座。

按控制方要求，本任务只运行 focused suite，未执行可能自行清理临时目录的全 app/native 集成；Task 4 统一执行最终集成回归。

## 已知限制与后续注意

- Token 数是明确标注的字符估算，不是真实模型 usage。
- 进程内 page working set 不跨重启；源记录和版本全部持久化，可重新 `pageIn`。
- 搜索与 episodic 配对候选均有 1000 条读取上限；这是首版有界查询策略。
- 脱敏为字段表和可识别模式的 best effort；Vault 仍是凭据权威，不应把已授权任意工具输出视为绝对无敏感信息。
- `actor.role` 是业务授权输入，不是身份认证；Task 3/API 层必须只由可信 operator 路径构造 operator actor，绝不能直接信任模型或任意 HTTP body。
