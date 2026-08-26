# Task 6 Report

## 实现

- 新增可复用 `HostV2RuntimeFactory` Protocol 与九场景
  `assert_host_v2_conformance()`；每个 mode 使用独立 Client、进程和数据库。
- Fake Host 固定标识为 `contract_fake` / `fake-v2`，revision 为
  `fake-host-v2/r2`，不冒充任何真实 Runtime。
- 新增 v1/v2 并存、v2 默认关闭、稳定导出与 README 边界说明。
- 最终修复去除自证：Host 实际评估 context budget、manifest/workspace grant，
  公共 Event 只回传安全计数、policy 与 result；checkpoint 使用跨进程持久化的
  opaque state，完整性材料不进入公共 Event。
- Factory 暴露安全 generation/nonce/process marker，九场景每次使用独立
  Client/进程/repository/registry/SQLite/cursor，并在正常/异常退出清理临时目录。
- 修复 terminal 后 Event 的竞态：有序 `query.status` seal ack 在 terminal 对
  consumer 可见前完成；timeout 和非法后续 Event 均 fail closed。
- Fix2 参数化覆盖 wrong state、run/term/step identity、cursor、`sealed=False`
  和错误类型 ack；7 个变体均稳定 fail closed、无错误值回显或挂起，生产现有
  exact-response 校验无需修改。
- Fix3 将 Secret scan 的 changed-file 枚举改为独立 checked subprocess；Git
  枚举失败立即非零退出且不输出成功计数，路径限制为仓库内 tracked changed
  files，bytes regex 不跳过二进制内容。
- Fix4 将 Secret scan 改为只读取 Source C/BASE 的 Git blob：checked NUL 枚举
  后始终以 path bytes + strict UTF-8 校验处理，不访问、resolve 或跟随工作树；
  删除项读 BASE blob，其他项读 C blob，非 blob/读取/timeout/decode 错误均
  fail closed 且不泄漏 path、Git stderr、traceback 或成功计数。

## Task 5 承重 P0

- RED：三个明确私有短语的 12 种正文变体在 runtime 与 persisted/AG-UI
  两边界共 `24 failed`。
- GREEN：共享 public-text validator 精确拒绝上述短语；连同普通
  provider/workspace 安全邻例共 `36 passed`。

## 验证

- v2 acceptance（九场景完整执行两遍）：`6 passed`；
- malformed seal：`7 passed`；
- Task 4：`148 passed`；
- mapper + AG-UI + v2 query：`1144 passed`；
- 简报指定 Host 专项：`908 passed`；
- terminal immediate-extra 独立进程重复：`passes=50 failures=0`；
- frontend build：exit 0；既有完整 Playwright 证据：`38 passed`；
- compileall、`git diff --check`、高置信 Secret scan：通过。
- Git-object Secret scan 十态：合法范围 `8/0/0` exit 0、合法空范围 `0/0/0`
  exit 0、invalid revision 安全 exit 2、symlink token blob `2/1/0` exit 1、
  symlink loop `1/0/0` exit 0、gitlink/blob 读取失败安全 exit 3；错误态均无
  path、Git stderr、traceback 或成功计数；删除含合成 token 的 BASE blob 得到
  `1/1/0` exit 1，删除且 BASE/HEAD 均无 blob 时安全 exit 3；合法格式但不存在
  的 revision 枚举安全 exit 2，非 UTF-8 Git path decode 安全 exit 3。

Source revision C 上未取得完整必需后端 `pytest -q` 的 PASS、exit code 与最终
计数；不把实施前 development graph 观测写成本次已证明的 baseline failure。

## 判定

```text
Decision: BLOCKED
Real runtime status: NOT_YET_EVALUATED
```

Source revision under test：`c803de37c6328330fda214ab0b4d9ecffdcd9ab9`。
Fix2 代码/测试提交 C 的历史包含 A/A2/B；D/E 与本次 documentation-only F
均为 C 的可达 descendants，不 amend 任何既有提交。

阻塞与完整命令、版本、HEAD、Fake revision、v1 兼容、Secret scan 和残余风险
详见 `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`。
