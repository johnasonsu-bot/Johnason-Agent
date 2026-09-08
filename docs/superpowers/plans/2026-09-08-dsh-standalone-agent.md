# DSH 独立 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付原生 DSH Web/CLI Agent，独立保存数据，加密管理模型凭据，并提供真实任务验收入口。

**Architecture:** 使用已锁定 DSH 子模块，外层 Node 启动器负责独立数据根和命令分派；原生 credentials 服务由加密 provider 替换。保留上游 Web、profile、工具、模型和会话实现，不接旧控制面。

**Tech Stack:** Node.js ESM、Node 内置测试/密码库、DSH Cordis 插件、原生 React/Vite Web。

**Spec:** `docs/superpowers/specs/2026-09-08-dsh-standalone-agent-design.md`

## Global Constraints

- 只在 `codex/dsh-standalone-agent` 与当前独立 worktree 中修改；旧工作区和未提交修改保留暂停。
- DSH 固定提交 `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`；Node `^22.19.0 || >=24.0.0`，pnpm `11.7.0`。
- 不把 API Key、Token、密码写入代码、普通配置、日志或浏览器持久化；允许专用加密凭据数据文件。
- 不加载旧 Vault、旧历史任务、旧 `.runtime`、默认 `~/.dsh` 或项目 `.env`；默认本应用数据根为用户目录下 `.johnason-dsh`。
- 默认 Web 回环监听 3080，端口占用明确报错；新 Web 不自动选择 workspace，CLI 执行必须显式选择 workspace。
- 不以 mock 代替真实 API/Agent 验收；单元测试可验证确定性组件；全库安全检查集中于最后一次。
- 不删除用户文件；测试产生的临时目录保留并报告位置，不运行自动清理旧目录。

## 文件职责

| 文件 | 职责 |
|---|---|
| `apps/dsh-agent/package.json` | 独立启动、测试、构建命令 |
| `apps/dsh-agent/src/launch-config.mjs` | 无副作用参数/路径/环境解析 |
| `apps/dsh-agent/src/cli.mjs` | 启动原生进程、错误与退出处理 |
| `apps/dsh-agent/scripts/build.mjs` | 校验固定源版本并调用原生构建 |
| `apps/dsh-agent/src/vault-store.mjs` | 加密文件读写与互斥，不依赖 UI |
| `apps/dsh-agent/src/credentials-plugin.mjs` | 实现原生 credentials 服务 |
| `apps/dsh-agent/src/vault-ui.mjs`、`public/vault.html` | 本地解锁录入入口及状态 |
| `apps/dsh-agent/src/profile.mjs` | 原生 profile 与最终加密 provider patch |
| `apps/dsh-agent/tests/` | 定向单元和集成测试 |
| `apps/dsh-agent/README.md` | 中文安装、运行、测试与限制 |
| `docs/testing/2026-09-08-dsh-standalone-acceptance.md` | D1–D12 实测证据，真实未测项不得标通过 |

### Task 1: 独立启动参数、版本校验与原生构建入口

Files: 创建 `apps/dsh-agent/package.json`、`src/launch-config.mjs`、`src/cli.mjs`、`scripts/build.mjs`、`tests/launch-config.test.mjs`、`README.md`。本任务不修改凭据代码或上游源文件。

Interfaces: `resolveLaunch(argv, {homeDir, repoRoot, env, nodeVersion})` 返回 `{mode, dataRoot, workspace, upstreamRoot, args, env}`；模式 `web/headless/plugin/doctor`。启动过程暴露准备 profile 的边界，由 Task 3 接入；在安全 profile 尚未可用时，执行模式报 `PROFILE_NOT_READY`，不得启动明文默认模式。doctor 可检查已构建状态。

- [ ] 先写测试，以缺少 resolver 的断言失败证明 RED；使用动态 import 并断言存在，避免把拼错模块当成功的 RED。

```js
const mod = await import('../src/launch-config.mjs').catch(() => ({}));
assert.equal(typeof mod.resolveLaunch, 'function');
const r = mod.resolveLaunch(['web'], {homeDir: '/tmp/user', repoRoot: '/tmp/repo', env: {DSH_HOME: '/tmp/old'}, nodeVersion: '22.20.0'});
assert.equal(r.dataRoot, '/tmp/user/.johnason-dsh');
assert.throws(() => mod.resolveLaunch(['headless', 'hello'], {homeDir: '/tmp/user', repoRoot: '/tmp/repo', env: {}, nodeVersion: '22.20.0'}), /workspace/i);
```

- [ ] 运行 `node --test apps/dsh-agent/tests/launch-config.test.mjs`，记录失败。
- [ ] 实现显式 `--data-dir`、`--workspace`、`--port`、`--no-open`，profile/plugin 的透传在 Task 3 完成。参数解析拒绝缺值/无效端口/未知模式；版本只接受 Node 22.19+ 的 22 系或 24+。

```js
const childEnv = Object.fromEntries(Object.entries(env).filter(([key]) => ['PATH','HOME','USERPROFILE','SystemRoot','TMPDIR','TEMP','TMP','LANG','LC_ALL'].includes(key)));
childEnv.DSH_HOME = dataRoot;
childEnv.DSH_TELEMETRY_DISABLED = '1';
```

- [ ] 构建入口检查子模块 HEAD 等于固定 SHA，再顺序执行 `corepack pnpm@11.7.0 install --frozen-lockfile`、`corepack pnpm@11.7.0 run build`。失败原样保留退出状态但不输出环境变量；不下载 latest，不修改旧工作区。
- [ ] 测试覆盖带空格路径、旧 DSH_HOME 不继承、凭据环境不透传、无效版本/参数、doctor 缺少构建产物、执行前 profile 未准备错误。更新 README 写清当前阶段尚不能录入凭据。
- [ ] 测试变绿并提交本任务文件。控制器独立运行新工作区原生安装/构建，记录实际输出，不要求执行者重复长构建。

### Task 2: 独立加密凭据存储

Files: 创建 `src/vault-store.mjs`、`tests/vault-store.test.mjs`；需要分离文件锁时新增 `src/vault-lock.mjs` 和对应测试。只负责存储，不修改启动器。

Interfaces: `new VaultStore(path)`；`status()` 返回 `{initialized, locked}`；`initialize(password)`、`unlock(password)`、`lock()`；`read()` 返回 `{refs,records}`；`update(mutator)` 在独占锁下读取当前磁盘数据、执行异步变更并提交。调用者不直接编辑磁盘内容。锁定后读写抛 `VAULT_LOCKED`。密码不保留为字符串，不写盘；已派生密钥只留内存并在 lock 时填零。

- [ ] 写 RED 测试：初始化后写入测试用假凭据，文件不含测试值，锁定后禁止读，正确密码可重新读取。

```js
await vault.initialize('unit-test-passphrase');
await vault.update(d => { d.refs.TEST_KEY = 'unit-test-value'; });
assert.equal((await readFile(file, 'utf8')).includes('unit-test-value'), false);
vault.lock();
await assert.rejects(() => vault.read(), {code:'VAULT_LOCKED'});
await vault.unlock('unit-test-passphrase');
assert.equal((await vault.read()).refs.TEST_KEY, 'unit-test-value');
```

- [ ] 实现 scrypt + AES-256-GCM 信封、随机盐/nonce、固定受支持格式；校验磁盘字段长度/格式后解密，失败返回可区分的锁定、解锁失败、文件格式错误；不声称能从认证标签失败区分错误密码与密文篡改。

```js
const salt = randomBytes(16);
const key = await scryptAsync(password, salt, 32);
const cipher = createCipheriv('aes-256-gcm', key, randomBytes(12));
// 仅持久化版本、派生参数、salt、nonce、tag、ciphertext，绝不持久化密码或 key。
```

- [ ] 使用跨进程原子 mkdir 锁及有界等待，所有 update 在锁内重新读磁盘。临时文件独占创建后 rename 原子提交。锁释放仅移除本次创建且持有的锁资源；测试目录不删除。锁过期不武断抢占活动进程；报忙错误而非覆盖。
- [ ] 增加两个实例/独立进程并发不同记录写入、异步记录变更、锁定与写入交错、初始化不覆盖已有文件、错误密码不改磁盘、损坏文件不重置等测试。所有测试只用虚构凭据。
- [ ] 运行定向测试并提交，报告 RED/GREEN 与边界语义。

### Task 3: 加密 provider、Web 解锁与完整原生 profile

Files: 创建 `src/credentials-plugin.mjs`、`src/vault-ui.mjs`、`public/vault.html`、`src/profile.mjs`；修改 Task 1 `src/cli.mjs`、`package.json`、`README.md`；测试 `tests/credentials-plugin.test.mjs`、`tests/vault-ui.test.mjs`、`tests/profile.test.mjs`。

Interfaces: profile 提供 `prepareProfile(config)` 并返回原生启动参数和最终 overlay 路径；VaultStore 使用 Task 2 API。credentials provider 必须覆盖该 pin 的完整 `CredentialProvider` 方法，不重写模型 adapter。Web 用户只在本地页面录入主密码；API Key 在原生 Models 页录入。解锁页面可作为原生 Web 同源附加页面，入口不替代原生聊天。

- [ ] 读取并遵循当前子模块 AGENTS；在新增外部插件实现前核对 `credentials/src/index.ts`、bundle patches、原生 Web HTTP server 扩展点和 profile 解析路径。先写服务测试，断言锁定不能解析；保存后只返回脱敏描述，下一次请求能读取变更。

```js
await provider.set('DEEPSEEK_API_KEY', 'unit-value');
assert.deepEqual(await provider.describe('DEEPSEEK_API_KEY'), {configured:true, source:'vault', writable:true});
assert.equal((await provider.resolve('DEEPSEEK_API_KEY')).value, 'unit-value');
assert.equal(JSON.stringify(await provider.listRecords()).includes('unit-value'), false);
```

- [ ] 完整实现 refs 和 records、JSON 验证、通知事件。引用读取不查询 `.env`；加密 provider 替换 base `credentials` 行，原生 native/web bundles 不裁剪，执行前检查仅一个凭据 provider。
- [ ] 解锁 UI 先测试 GET 状态与初始化/解锁/锁定操作；仅允许同源本地请求，限制请求体，错误信息不回显密码。初始化需要二次密码匹配；提交后清空输入；错误密码不删除数据；锁定不取消历史。
- [ ] 原生 Web 默认不选择目录；CLI 明确 workspace 后保留 headless/profile/plugin 语义，不把自定义插件管理当终端 TUI。CLI 密码无回显，不通过参数或文件传递；遇非 TTY 说明需使用 Web 解锁入口，不等待不可见输入。
- [ ] 运行 Node 定向测试与实际无密钥 Web 构建启动，检查配置组合不含 credentials-local、页面可达、Models 页面可操作、停止后进程退出。
- [ ] 提交本任务；记录同源集成所用上游扩展点，不伪造原生 UI 的可用状态。

### Task 4: 用户环境与能力验收交付

Files: 修改 `apps/dsh-agent/README.md`、根 `README.md`，创建 `docs/testing/2026-09-08-dsh-standalone-acceptance.md`，新增必要真实进程集成测试及本地测试 Skill/MCP/plugin fixtures。

Interfaces: 使用前三项实现的真实入口；真实凭据只能用户在新 Web 录入。

- [ ] 写并运行启动/退出、端口占用、刷新恢复的真实进程测试；只读检查独立数据根，不调用旧 runtime 的任何任务。
- [ ] 启动 Web 并在浏览器实际查看原生 UI 与解锁页；给用户配置入口。用户未录入时其他不需要密钥的验证继续，真实 API 标“等待用户录入”，不标通过。
- [ ] 用户录入后按设计 D2–D11 真实执行：连续两轮对话、文本/HTML 文件、工具状态、审批、中断、Plan/Todo、compact、goal、子 Agent、Skill、本地 MCP 和 profile/plugin。验收结果采用下列记录结构，API 不通保存原始错误类别与会话标识，不统一包装 retryable。

```json
{"case":"D4","status":"passed|failed|not_run","provider":"user-configured","model":"user-configured","session":"native-session-id","evidence":["artifact-path"],"reason":""}
```

- [ ] 一次性集中检查新增代码的凭据落盘/回显、跨源请求、错误 workspace 与受控故障注入；只使用测试数据，不执行破坏用户文件或外部系统的攻击。
- [ ] 验收文档列出 D1–D12、实际命令、时间、结果、人工步骤；安装说明给出前置环境、构建目录、启动入口、停止和恢复、升级固定版本流程，明确 Windows/Linux 未实测状态。
- [ ] 全分支规格与质量复核，提交已验证变更；不擅自合并旧分支、不删除 worktree、不自动发布公网。

## 自检

Task 1 对应 D1/D12；Task 2–3 对应 D2/凭据独立性；Task 3–4 对应 D3–D11。Task 3 消费 Task 1 resolver 和 Task 2 VaultStore，没有另一套会话库。真实调用依赖用户的新凭据，不能使用旧 Vault 规避人工录入。每任务先 RED 后 GREEN，记录可恢复进度，完成后逐任务审查；不在中途增加旧 P0 功能。
