# DSH 本地模型生成停滞：诊断与调优

## 范围

2026-09-08，独立 DSH Web（3080）→ 原生 pi-ai adapter → LM Studio（1234/v1）。本机模型 ID 为 `qwen3.8-27b-uncensored-mlx`，模型元数据架构为 `qwen3_5`、27B、MLX 4bit。没有替换原生 DSH 代码，没有调用云 API，没有重放用户的原任务或删除会话。

## 已确认的原因

- 原会话 `session-539372f3-1e9e-4748-a285-5607d9c9b99b` 共 12 步、11 次工具调用，最后为用户停止（不是已完成）。第 5 步重试 2 次，第 12 步重试 5 次；7 次均为 `pi-ai stream idle timeout after 300000ms`。
- UI 累计模型耗时约 119 分钟、工具耗时约 2.7 秒。217K 输入 token 是多步累计，不是单次上下文；末段服务端实际提示约 29K token。
- 当前模型模板在未覆盖时默认 `reasoning_effort= xhigh`，自动插入长思考指令，并以未闭合的 `<think>` 开始生成。
- 本机实测 `chat_template_kwargs.enable_thinking=false` 和 `reasoning_effort=none` 都未关闭思考，不能把请求成功等同于参数生效。
- 原生模型能力缺省值转为请求时为 `max_completion_tokens: 32768`。此前没有为这个本地模型单独设置合适的输出限额。
- 模型反复读材料，使任务迟迟未进入最终交付。长思考、长输出预算与超时重复生成共同放大耗时。没有证据表明凭据或文件工具故障是主因。
- LM Studio 后段日志的提示缓存复用效率达 92.74%；UI 显示的 0% 不能用来判断本机 KV 缓存未生效。

## 已应用设置（本机，不含凭据）

1. LM Studio → My Models → 当前 Qwen 模型 → Inference → Prompt Template：仅在原模板首行加入 `{%- set enable_thinking = false %}`，其余工具、视觉、历史消息模板保留。这是该模型的默认模板设置，其他应用使用同一模型也会受影响；不影响其他模型或云模型。
2. 通过 DSH 原生 `settings.mutate` 热更新 `llm-pi-ai.providers.lmstudio`，带 revision 检查，保留其他字段与加密凭据：
   - 当前 Qwen 模型 `maxTokens: 4096`。
   - `contextWindow: 208384`，与当前 LM Studio 实际加载容量一致；不是重新加载或扩容。
   - `compat.maxTokensField: max_tokens`。
   - `retryPolicy: {mode: normal, maxRetries: 1, retryableCodes: [RATE_LIMIT, SERVER, TRANSPORT]}`。超时与空响应不再自动重跑。
   - `streamIdleTimeoutMs: 300000` 保留原值。测试中曾试设 120000，但原生完整工具目录冷启动约 9K token、预填充较慢，因此恢复 300000；不能用过短超时制造假故障。

模型设置实时生效，无需重启 DSH 或重新解锁 Vault。没有将密钥写入普通配置；现有 credential reference 不变。

## 实测记录

| 测试 | 时间 | 输出 | 结论 |
| --- | --- | --- | --- |
| 关闭思考参数（chat_template_kwargs），256 token | 15.9 秒 | 255 推理 token，正文为空，length | 本机该参数未生效 |
| 关闭思考参数（reasoning_effort=none），256 token | 15.9 秒 | 255 推理 token，正文为空，length | 本机该参数未生效 |
| 修改模板前，四节需求初稿，1536 token | 110.0 秒 | 1535 推理 token，正文为空，length | 只加短提示、采样调整不能解决 |
| 修改模板后，短验收标准，256 token | 3.2 秒 | 36 正文 token，0 推理，stop | 正文通路恢复；短样本未严格遵守 GWT 排版 |
| 修改模板后，四节需求初稿，1536 token | 40.0 秒 | 419 正文 token，0 推理，stop | 四节均有正文；未进行业务准确性验收 |

以上是同类真实本地 API 样本，不是同一输入的严格性能基准，不代表原来含大量材料的任务已完成。生成的业务指标仍需人工确认，不能视为已确定需求。

原生 Agent 只读闭环验证会话：`session-3aadcf74-bd3b-4564-9d77-229db9e602e6`。该测试读取测试应用 package.json 并输出 GWT 标准，不操作用户业务文件。

- 第 1 轮使用试调的 120 秒停顿上限，在约 9161 token 的冷预填充阶段超时；没有自动重试。这说明 120 秒对本机该模型的原生工具目录太短，并不能作为最终默认值。
- 恢复 300 秒上限后，由测试者手动继续该**测试会话**的第 2 轮（不是用户原会话）：18,340 ms，2 步、1 次 `read`、0 次重试，`turn/end.reason.kind=completed`。
- 实际读回包名 `@johnason/dsh-agent`，最终正文包括 Given、When、Then 三项及重复请求返回同一 ID、不产生第二条记录的标准。
- LM Studio 请求日志确认原生 Agent 发出的字段为 `max_tokens: 4096`。第二步 `cached_tokens=9262`、`uncached_tokens=190`，因此 18.3 秒是已预热闭环成绩，不是冷启动承诺。
- 原业务会话停留在用户停止状态；未替用户恢复、追加消息或重跑文件调查。

## 继续原会话

在原会话追加：

> 基于当前已读取的材料直接输出初稿，不再检索或读取文件。按“用户角色与场景、GWT 验收标准与指标、用户旅程、字段范围”四节组织。无法确定的信息标注“待确认”，先在聊天中输出完整正文，完成后结束。

这是一条由用户自行发送的收敛指令，不是清空历史或重发原任务。复杂研究仍可用思考模式，但应另行明确预算和查阅范围。

## 回退

- 在上述 LM Studio 模板中只移除新增首行即可恢复原有默认 xhigh 行为；不要点击重置整个模型设置，以免丢失用户原有调整。
- 通过 DSH 设置恢复本次新增的模型 maxTokens、contextWindow、maxTokensField 和 retryPolicy 覆盖项（原来均未设置）；无需动 Vault、模型 ID、Base URL 或已有凭据引用。
- 如以后重新加载模型改变了上下文容量，按 `/api/v0/models` 的 `loaded_context_length` 同步 DSH，而非固定沿用本机数值。

参考：[LM Studio Chat Completions 参数](https://lmstudio.ai/docs/developer/openai-compat/chat-completions)、[模型 Prompt Template](https://lmstudio.ai/docs/app/advanced/prompt-template)。实际生效以本机模型模板及输出统计为准。
