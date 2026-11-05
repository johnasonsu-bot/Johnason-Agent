# DSH Ecosystem Implementation Plan

> **For agentic workers:** Follow the delegated file ownership; do not revert others' changes.

**Goal:** 每日同步官方 DSH 至用户 fork，以插件保留本地特性并构筑可用候选，连接 Codex 与 Ollama。

**Architecture:** 官方 gitlink 固定版本；platform 适配 API，本地 plugins 保留业务；社区插件固定依赖与供应链来源，订阅凭据进入 Keychain。

**Tech Stack:** Node.js / pnpm / Cordis / macOS Keychain / GitHub CLI / Codex automation.

**Spec:** docs/superpowers/specs/2026-11-05-dsh-ecosystem-design.md

## Global Constraints
- 保留所有既有本地数据和未提交修改；新隔离数据根验收。
- 不修改官方实现，不写入明文 token/密码，不强推或删除文件。
- 仅通过兼容校验的候选可升级；daily 同步不等于无条件切换运行版本。

## Review Focus
- fork 分叉或脏工作区时必须失败关闭。
- Vault/Keychain 锁定不回退明文存储。
- 社区插件不得默认开放公网桥接。
- 原生包 API 改变不得以跳过测试宣称成功。
- 固定版本清单、实际依赖、构筑证据必须一致。

### Task 1: 上游和版本适配
- [x] 固定官方 master SHA，更新唯一 platform-release 及 gitlink。
- [x] 对已变化的平台 API 编写针对性回归并修复适配层。
- [x] 冻结安装、官方构筑、平台和本地插件真实测试。

### Task 2: 社区插件和模型
- [x] 固定三个来源及版本，审查安装入口和远程功能。
- [x] subscriptions 增加 Keychain 存储并验证不会生成 auth.json。
- [x] 组合 web/market/subscriptions 和现有本地插件。
- [x] Ollama 原生真实请求通过；Codex 官方 OAuth 等待用户选择账户并授权，未声称已连接。
- [ ] 用户完成 Codex OAuth 后验证订阅真实模型请求。

### Task 3: 每日同步
- [x] 实现默认只检查、显式应用的同步脚本，测试拒绝分叉和错误仓库。
- [x] 同步现有 fork。
- [x] 推送 Johnason 集成分支 `codex/dsh-ecosystem-20261105`（核心提交 `3718ce9`，构筑溯源回执 `cdff6f5`）。
- [x] 配置每日 09:00 Asia/Shanghai 自动化，记录失败保留旧版本的行为。

### Task 4: 交付
- [x] 独立审查与最终构筑证据，更新 README/验证报告。
- [x] 提供运行入口、同步时间、版本和认证操作说明。
