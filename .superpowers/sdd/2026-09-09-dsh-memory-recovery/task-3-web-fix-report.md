# Task 3：真实 Web 新会话启用修复

日期：2026-09-09。

## 根因和最小修复

根因不是跨 context 对象身份变化。原生 `PermissionPresetService` 的较早 `session/created` 监听器会同步追加 3 个初始化事件：`permission/preset`、`sandbox/mode`、`approval/policy`。Task 3 用增长中的 `session.seq === 0` 判断新会话，因此把从未开始 turn 的新 Web 会话误判为历史会话。

实际独立 Web 诊断 `/Users/sushi/dsh-memory-web-diagnose-BNEZol` 的 `session.history` 返回 seq 0/1/2 恰为上述 3 个事件；原生 projections 显示 turns=0、steps=0、blank=true、lastPromptAt=null。

修复仅调整首次创建观察：使用原生公开 `Session.firstLiveSeq === 0`（构造时 seed/live 边界），保留 `!header.seedLength`，且当时的事件只能属于上述 3 种原生初始化策略事件。仍需本插件观察到该创建生命周期；不接纳已存在会话。`session/end-seed`、其他历史事件及 configure 时已出现的 user/message 或 turn/start 仍拒绝。没有放宽 scope、actor 或已有会话自动启用。

## TDD 与实际 Web 证据

- 原 Task 4 首个真实 Web 测试先 RED：configure 返回 `MEMORY_NEW_SESSION_REQUIRED`；本轮 RED fixture `/Users/sushi/dsh-memory-web-DTGfi3`。
- 新增 Task 3 回归使用真实 SessionStore，在更早的创建监听器中按原生顺序追加策略初始化事件，先 RED 为同一错误。修复后新会话可配置，而只含策略的 seeded 会话、空 seeded 会话、已开始 turn 的会话都继续拒绝。
- Task 3 + profile 最终 **16/16 pass，0 fail/skip**。
- 修复后重跑 Task 4 第一个真实 Web 测试：configure、版本化三类记忆、分页、跨项目隔离全部通过，直到其第 72 行 `Host: evil.example` 安全断言才失败（200 而非 403）。这是新显露的 Task 4 HTTP 边界问题，已通知 controller，未改 Task 4 UI 或测试，**不声称该整项测试全绿**。
- 该真实 Web fixture `/Users/sushi/dsh-memory-web-n2RUZ4/memory/recovery.sqlite` 经只读 SQL 确认，alpha 与 beta 两个新会话均保存 enabled=1、version=1 的配置事件。
- Task 4 第二项真实 Web `HTTP audit preserves UNKNOWN, causal edges and past system-time without recovering on reads` **1/1 pass**，fixture `/Users/sushi/dsh-memory-web-DHeqmu`。此测试走原生 `session.create` wire 后 configure，并验证 Effect 历史/因果和只读无恢复。
- 修改模块 `node --check` 和 `git diff --check` 均退出 0；SQLite ExperimentalWarning 原样保留。

所有命令使用 `/Users/sushi/.nvm/versions/node/v22.20.0/bin/node`、login:false；未操作现有 3080，未删除任何 fixture/文件。

## 文件所有权

只修改并提交 Task 3 的新会话判定 hunk、`memory-runtime.test.mjs` 回归及本报告。`memory-recovery-plugin.mjs` 中 Task 4 的 UI import 与 ctx.inject 两个未提交 hunk 原样保留、不纳入本提交；Task 4 UI/tests 未修改。
