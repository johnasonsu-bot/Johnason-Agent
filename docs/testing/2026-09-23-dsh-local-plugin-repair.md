# 本地请求错误与插件入口修复

## 问题与修复

用户实际发送消息时出现 `DeepSeek request extension preparation failed / REQUEST_EXTENSION`。官方默认插件 `plugin-package-inventory-deepseek` 在请求发送前读取活跃插件的包身份；本地启动包与浏览器插件包缺少 `version`，因此清单生成失败。已给两个源包、根 lockfile 及生成的 encrypted-base 包补齐版本，保留官方校验。

现场重启后用户再次发送消息，发现另一个错误：已初始化但锁定的 Vault 被官方 Messages adapter 包装为 `TRANSPORT`，引发 5 次无效重试。本机无凭据网络探测正常；真实锁定 Vault 回归确认 HTTP 次数为 0。已在本地凭据适配边界仅将 `VAULT_LOCKED` / `VAULT_NOT_INITIALIZED` 转为官方同一类身份的 `LlmError`，保留拒绝访问与原错误码，明确提示通过 `/vault` 解锁或初始化。其他错误原样抛出，业务授权的 record 路径不变。

旧功能已有独立后端，但浏览器插件没有 bundle 元信息、profile dependency 和插件详情槽注册。已将现有 `@johnason/dsh-memory-ui` 注册为原生「已安装」插件，详情「Johnason 本地功能」提供八项入口：Vault、三类记忆、恢复审计、语义子图、任务沙箱、Forge、Data Platform、统一授权。

状态来自只读本地接口；系统未配置与接口不可用分别显示。系统状态不自动发起登录或业务请求。新的元数据接口只输出 schema 版本、功能标识与 provider 的 id/label/type，不输出路径或凭据。

插件开关仅控制界面，凭据 provider 唯一性、已有会话策略和沙箱限制继续执行。首次引入记录保存在 profile manifest 的 `johnason:provisioned-bundles` 字段；主动停用或卸载不会在下次启动时被撤销。未激活界面的 provider 配置仍留在 config-only overlay，支持原生管理器重新启用。

## 验证证据

- `node --test apps/dsh-agent/tests/*.test.mjs`：207 项，206 通过，1 跳过，0 失败；跳过项依赖真实业务环境。
- 最终日志：`.vendor/dsh-build/logs/local-plugin-vault-fix-20260923-verified.log`。
- 新请求回归使用真实原生 Web profile、官方扩展注册表和 DeepSeek adapter。修复前复现同款 REQUEST_EXTENSION 且 HTTP 次数为 0；修复后向本机 HTTP/SSE 服务发送完整插件版本清单并读取 `LOCAL_EXTENSION_OK`。缺版本的第三方测试包仍被拒绝。
- 真实原生 Messages adapter + 隔离 Vault 回归确认锁定时返回 `VAULT_LOCKED` 与解锁提示，HTTP 次数为 0，Vault 保持锁定。官方默认重试清单不包含此错误码。
- 真实 Chromium 从侧栏「插件」进入「已安装」中的 `memory-ui`，打开详情并验证状态、Vault/记忆链接及未配置系统无失效链接。
- 新增 HTTP 边界测试覆盖 loopback、Host、Origin、跨站、method 与输出字段白名单；浏览器渲染测试覆盖刷新及异常状态。
- profile 回归覆盖重复启动不重复挂载、停用后重启、带系统摘要重新启用、卸载后重启不自动装回，以及原有凭据安全检查。
- `check-platform.mjs`、`cli.mjs doctor`、`git diff --check` 均通过；官方子模块未改动。

本轮修改均为本地插件 JavaScript 与包/profile 元信息；官方原生产物沿用此前已构建版本。启动器在重启时重新生成本地 overlay、刷新 profile 内浏览器包，无需清理官方构建目录。

## 使用与边界

入口：侧栏「插件」→「已安装」→ `memory-ui` →「Johnason 本地功能」。重启同一数据根后需重新解锁 Vault，再重试用户消息。

Forge/Data Platform 在当前本地启动参数中尚未配置；保留受信任适配器与 `--systems` / `--core` 接入方式，在详情页如实显示未配置。记忆与沙箱仍按会话显式启用。旧格式日志迁移不属于此次修复，不自动迁移旧目录。

验证没有使用真实模型 API Key，没有调用云模型或业务系统，不能将本机协议回归等同于真实模型账户的端到端验收。
