# Task 6 Report

## 实现

- 新增可复用 `HostV2RuntimeFactory` Protocol 与九场景
  `assert_host_v2_conformance()`；每个 mode 使用独立 Client、进程和数据库。
- Fake Host 固定标识为 `contract_fake` / `fake-v2`，revision 为
  `fake-host-v2/r2`，不冒充任何真实 Runtime。
- 新增 v1/v2 并存、v2 默认关闭、稳定导出与 README 边界说明。
- Fix1 去除自证：Host 实际解析 context budget、manifest/workspace grant，并只
  回传安全 digest/计数/policy；checkpoint 使用跨实例真实 ref/digest/cursor。
- Factory 暴露安全 generation/nonce/process marker，九场景每次使用独立
  Client/进程/repository/registry/SQLite/cursor，并在正常/异常退出清理临时目录。
- 修复 terminal 后 Event 的竞态：有序 `query.status` seal ack 在 terminal 对
  consumer 可见前完成；timeout 和非法后续 Event 均 fail closed。

## Task 5 承重 P0

- RED：三个明确私有短语的 12 种正文变体在 runtime 与 persisted/AG-UI
  两边界共 `24 failed`。
- GREEN：共享 public-text validator 精确拒绝上述短语；连同普通
  provider/workspace 安全邻例共 `36 passed`。

## 验证

- v2 acceptance（九场景完整执行两遍）：`6 passed`；
- Task 4：`141 passed`；
- mapper + AG-UI + v2 query：`1137 passed`；
- 简报指定 Host 专项：`901 passed`；
- terminal immediate-extra 独立进程重复：`50/50`；
- frontend build：exit 0；既有完整 Playwright 证据：`38 passed`；
- compileall、`git diff --check`、高置信 Secret scan：通过。

Source revision A2 上未取得完整必需后端 `pytest -q` 的 PASS、exit code 与最终
计数；不把实施前 development graph 观测写成本次已证明的 baseline failure。

## 判定

```text
Decision: BLOCKED
Real runtime status: NOT_YET_EVALUATED
```

Source revision under test：`652954f5740b68183c97603174c4b660956fff65`。
代码提交 A 为 `cd95147db24fb1547afd63a3374a1e3ebef868a0`，A2 是其可达
child；本报告由 A2 的独立 documentation-only child B 提交，不 amend A/A2。

阻塞与完整命令、版本、HEAD、Fake revision、v1 兼容、Secret scan 和残余风险
详见 `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`。
