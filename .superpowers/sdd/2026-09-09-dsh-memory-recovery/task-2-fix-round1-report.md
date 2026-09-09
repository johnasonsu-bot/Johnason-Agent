# Task 2 Fix Round 1

日期：2026-09-09

## 结论

复审两项已按真实反例修复，最终 Task 2 定向测试 **18/18 pass、0 fail、0 skip**。仅修改 Task 2 两个模块、对应两个测试和本报告；未删除任何 fixture、未运行全 app 测试、未操作已有服务/会话/Vault。

## 1. Effect 追溯更正的 supersession 与时间顺序

根因：`reconcile/prepare` 用最新 version 作为当前状态，但 `list/graph` 按最大 validTime 选事件。UNKNOWN(valid=30) 被 COMMITTED(valid=25) 更正后，旧 UNKNOWN 仍占据最大 validTime，造成读接口不一致。

修复语义：

- 真正只读核验得到的 reconcile 事件追加 `supersedesVersion`，指向被替换的 UNKNOWN 版本；旧事件不修改或删除。
- 更正事实从其实际 `validTime` 生效。查询先根据 validAt/systemAt 判断更正是否已生效、是否在该知识时点已记录，只有两个条件都满足才排除被更正的 UNKNOWN，然后继续按 valid/system/version 选择可见事实。
- 因此 UNKNOWN30→COMMITTED25 保留实际完成时间 25；当前 prepare/list/graph 一致为 COMMITTED，validAt24 仍 EXECUTING，旧 systemAt 且 validAt31 仍 UNKNOWN，旧 systemAt 且 validAt26 仍 EXECUTING。重开 SQLite 后一致。
- 如果更正时间晚于 UNKNOWN，例如 COMMITTED35，则在 validAt32 仍可见 UNKNOWN，validAt35 起更正生效。不是全时域抹除 UNKNOWN。
- begin/finish/recover 等普通状态转换必须 validTime 不早于上个状态。reconcile 可以早于 UNKNOWN，但不能早于实际 EXECUTING 的 validTime；违反时抛新增 `EFFECT_INVALID_TIME_ORDER`，事务不追加错误事件。

不改变 `user_version=1`、effect_schema=1；supersession 作为追加事件 JSON 元数据保存，无破坏性迁移。此语义适用于本轮修复后新增的 reconcile 记录；没有重写既有历史。

TDD：新增真实 SQLite 反例初次为 6/8 pass，两项失败分别为当前 list 错误 UNKNOWN、普通转换倒序未抛错；修复后 effect focused 8/8 pass。原首个测试的 begin 显式设 valid=15，使原 finish(valid=20) 符合真实时间顺序，而非借系统当前时间反向回填。

## 2. 有界显示与流式诊断分离

根因：原 `runnerFailed()` 和 denial 匹配只读取已按 maxOutputBytes 截断的 stderr 文本；调低显示上限会改变执行状态判定。

修复：

- stderr 每个 data chunk 都进入独立流式 scanner，再进入原有有界显示缓冲；诊断结果不依赖 maxOutputBytes。
- UTF-8 StringDecoder 跨字节块续接；滚动尾部只保留最长签名长度减一，用于跨 chunk 匹配；每条规则只保留“当前行匹配/此前行已确认”布尔值。
- 信息行排除仍按大小写不敏感的完整行精确比较；只保存最长信息行长度加一的前缀与饱和长度计数，超长行不能匹配信息行豁免，但依然扫描全部签名。支持 CRLF 跨 chunk、无末尾换行的最终行。
- 进程 close 后应用 allowedExitCodes 门槛，最后确定 runnerFailure；fatal 与 denial 同时命中时仍优先 runnerFailure。确认 runnerFailure 仍保持 UNKNOWN，不宣称 stderr 证明零副作用。
- 扫描常驻状态空间受公共诊断配置长度约束，不随总 stderr 或单行长度增长；处理期间只额外使用当前 stream chunk 的解码文本。没有另存无限诊断日志。

TDD：新增三项诊断测试全部先 RED，分别复现 limit=8 漏 fatal、长行/跨块漏 fatal、截断点后 native EPERM 漏 denial。修复后全部 GREEN。

测试保持真实 `LocalSandboxProvider.confine()` 和真实受限子进程。为了稳定覆盖分类协议，两个 runner dialect 用例仅替换公共 runnerFailureRules 元数据，由受限 child 故意输出对应诊断，**不把它们冒充真实 launcher 故障**。另一个 denial 用例是真实 Seatbelt 在 protected 目录拒绝写入。

已覆盖相同诊断在 1024 与 8 字节显示上限下都 UNKNOWN/runnerFailure；分次 stderr write 跨块匹配；20 万字符长行；信息行精确豁免；exit=1 不满足 [125]；无末尾换行；native denial 在显示截断之后仍分类。

## 验证

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test apps/dsh-agent/tests/effect-store.test.mjs apps/dsh-agent/tests/task-sandbox.test.mjs
```

最终 18/18 通过，SQLite ExperimentalWarning 按预期保留；两个生产模块另经 node --check。真实隔离继续报告 `native-seatbelt/full`，workspace 写成功，protected/symlink escape 均被拒绝。

本轮最终真实隔离 fixture：`/Users/sushi/dsh-task-sandbox-JEqb6y`；流式诊断 fixture：`/Users/sushi/dsh-task-sandbox-EJEOsF`、`/Users/sushi/dsh-task-sandbox-xFyaSW`、`/Users/sushi/dsh-task-sandbox-iDdaDj`。其余路径随测试 diagnostic 输出；所有文件和临时目录均保留。
