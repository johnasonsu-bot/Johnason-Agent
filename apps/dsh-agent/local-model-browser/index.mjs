import Schema from '@deepseek-ai/schemastery';

const DEFAULT_SETTINGS = Object.freeze({
  enabled: true,
  baseURL: 'http://127.0.0.1:1234/v1',
  apiKeyRef: '',
  decisionModel: 'jev-style-qwen3.5-2b-decision',
  workerModel: 'qwen3.8-27b-uncensored-mlx',
  timeoutMs: 60000,
  maxTokens: 2048,
  threshold: 0.75,
  maxCallsPerTurn: 256,
  maxConcurrent: 2,
});
const SettingsSchema = Schema.object({
  enabled: Schema.boolean().default(DEFAULT_SETTINGS.enabled),
  baseURL: Schema.string().default(DEFAULT_SETTINGS.baseURL),
  apiKeyRef: Schema.string().default(DEFAULT_SETTINGS.apiKeyRef),
  decisionModel: Schema.string().default(DEFAULT_SETTINGS.decisionModel),
  workerModel: Schema.string().default(DEFAULT_SETTINGS.workerModel),
  timeoutMs: Schema.number().min(1).max(120000).default(DEFAULT_SETTINGS.timeoutMs),
  maxTokens: Schema.number().min(1).max(4096).default(DEFAULT_SETTINGS.maxTokens),
  threshold: Schema.number().min(0).max(1).default(DEFAULT_SETTINGS.threshold),
  maxCallsPerTurn: Schema.number().min(1).max(2000).default(DEFAULT_SETTINGS.maxCallsPerTurn),
  maxConcurrent: Schema.number().min(1).max(4).default(DEFAULT_SETTINGS.maxConcurrent),
});

// Native settings are scoped to this installed profile entry; only settings are editable.
export const Config = Schema.object({
  implementation: Schema.string().required(),
  settings: SettingsSchema.volatile(),
});

export const inject = ['settings', 'tools', 'credentials'];

export async function apply(ctx, config = {}) {
  if (typeof config.implementation !== 'string' || !config.implementation) {
    throw new Error('本地模型实现路径尚未配置。');
  }
  const module = await import(config.implementation);
  if (typeof module.apply !== 'function') throw new Error('本地模型实现不可用。');
  return module.apply(ctx, config.settings ?? {});
}
