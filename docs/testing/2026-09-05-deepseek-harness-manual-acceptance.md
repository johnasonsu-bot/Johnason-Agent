# DeepSeek Harness 云端人工验收

状态：**MANUAL_PENDING / NOT_RUN**。按用户要求转为人工触发，不等待凭据、不阻塞 Task 4 开发，不计为 `GO_DSH_PLUGIN_SMOKE` 或 `GO_RUNTIME_FEDERATION`。

## 输入

| 输入 | 当前值 / 取得方式 |
|---|---|
| Runtime | `dsh`，使用正式模型 Host 入口 |
| Provider Profile | `deepseek-primary`，已保存于用户 Workbench 数据库 |
| 模型 | 该 Profile 的 `default` 别名当前为 `deepseek-v4-flash`；命令读取并冻结实际配置 |
| API 地址和 API Key | 从已保存的 Profile 和加密 Vault 读取；不在命令或聊天中传入 Key |
| Vault 解锁密码 | 运行时在本机终端隐藏输入；不是 DeepSeek API Key |
| 网络 | 可以访问该 Profile 配置的 DeepSeek 云端 API；会产生一次实际模型调用及相应费用 |

如果需要更换 API Key，先在客户端的模型供应商设置中更新并保存；不要把 Key 或 Vault 密码发送到聊天中。

## 手工运行

在 macOS 终端中运行以下整段命令。密码由 `getpass` 从终端读取，不回显、不落盘，也不作为子进程命令行参数或环境变量传递。

```bash
cd /Users/sushi/Downloads/Johnason-Agent/.worktrees/batch-3-4-a-host-v2/mvp
.venv/bin/python - <<'PY'
from getpass import getpass
from pathlib import Path
import subprocess
import sys
import tempfile

runtime_dir = Path("/Users/sushi/Library/Application Support/hermes-canvas-spike/workbench-runtime")
output_dir = Path(tempfile.mkdtemp(prefix="johnason-dsh-manual-")).resolve()
password = getpass("Vault 解锁密码（隐藏输入）: ")
exit_code = 1
try:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_runtime_live_endpoint.py",
            "--runtime", "dsh",
            "--provider-profile-id", "deepseek-primary",
            "--runtime-dir", str(runtime_dir),
            "--output-dir", str(output_dir),
            "--vault-password-stdin",
        ],
        input=password + "\n",
        text=True,
        timeout=300,
    )
    exit_code = result.returncode
except subprocess.TimeoutExpired:
    print("超过 5 分钟，已停止本次命令；本轮不计为通过。")
finally:
    password = None
print(f"人工验收证据目录：{output_dir}")
sys.exit(exit_code)
PY
```

这是一次人工发起的实际云端验证，经过固定 DeepSeek Harness 模型 Host、正式准入、Vault/私有 Grant 和事件执行链；不会用 fixture 响应替代真实失败。当前只验收模型请求，不能据此宣布工具、Skill、Workspace 或整个联邦引擎全部通过。

## 结果判定

- 成功：命令退出码为 `0`，输出准备结果 JSON，并在打印的独立目录中生成签名清单和 `runtime-live-evidence-dsh.json`；证据应绑定 `dsh`、当前模型/构建、`endpoint_kind=cloud` 和 `terminal=completed`。
- 失败：退出码非 `0`，返回稳定的 `blocked` 结果。保留输出目录，按 Profile、Vault 解锁、网络、模型兼容性和当前构建排查，不把失败记成通过。
- 未运行或未完成：维持 `MANUAL_PENDING / NOT_RUN`。

命令只生成独立验证证据，不覆盖原有会话，也不会自动把证据部署到正在运行的客户端。要让会话下拉实际开放 DSH，还需要应用加载对应证据、配置正式模型 Host 并重新检查准入状态；该步骤与前端手测环境接续。证据有有效期，过期或源码/构建变化后需要重新人工验证。

本说明中的命令仅完成参数/代码路径核对，**尚未执行 DeepSeek 云端调用**。Python Term 与 Goose 的真实 LM Studio 验收结果另见 Task 3 fix2 报告，不能替代本项。
