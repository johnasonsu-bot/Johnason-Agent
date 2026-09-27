import { readFileSync } from 'node:fs';
import { Schema } from '../../platform/local-model-api.mjs';
import { DEFAULT_SETTINGS, MODELS_PATH, createLocalModelHandler, createLocalModelService, createLocalModelTools, normalizeSettings } from './service.mjs';

export const name = 'johnason-local-models';
export const inject = ['settings', 'tools', 'credentials'];
export const SettingsSchema = Schema.object({
  enabled: Schema.boolean().default(DEFAULT_SETTINGS.enabled),
  baseURL: Schema.string().default(DEFAULT_SETTINGS.baseURL),
  apiKeyRef: Schema.string().default(DEFAULT_SETTINGS.apiKeyRef),
  decisionModel: Schema.string().default(DEFAULT_SETTINGS.decisionModel),
  workerModel: Schema.string().default(DEFAULT_SETTINGS.workerModel),
  timeoutMs: Schema.number().min(1).max(120000).default(DEFAULT_SETTINGS.timeoutMs),
  maxTokens: Schema.number().min(1).max(4096).default(DEFAULT_SETTINGS.maxTokens),
  threshold: Schema.number().min(0).max(1).default(DEFAULT_SETTINGS.threshold),
  maxCallsPerTurn: Schema.number().min(1).max(32).default(DEFAULT_SETTINGS.maxCallsPerTurn),
  maxConcurrent: Schema.number().min(1).max(4).default(DEFAULT_SETTINGS.maxConcurrent),
});

export function apply(ctx, config = {}) {
  const settings = ctx.settings.register(name, SettingsSchema, { base: config, validate: normalizeSettings });
  const service = createLocalModelService({
    getSettings: () => settings.get(),
    resolveCredential: ref => ctx.credentials.resolve(ref),
    getTurn: exec => {
      const session = exec?.agent?.session;
      if (!session) return undefined;
      const projection = ctx.get('sessionProjections')?.stateOf(session, 'turnBoundary');
      return projection?.openTurnStartSeq != null ? projection.lastTurn : undefined;
    },
  });
  ctx.effect(() => () => service.dispose());
  for (const tool of createLocalModelTools(service)) ctx.tools.register(tool);
  ctx.inject(['webServer'], web => {
    web.effect(() => web.webServer.register({ kind: 'exact', path: MODELS_PATH, handler: createLocalModelHandler(service) }));
  });
  ctx.inject(['skills'], skills => {
    const content = readFileSync(new URL('./skills/SKILL.md', import.meta.url), 'utf8').replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, '');
    skills.skills.register({
      name,
      description: 'Use the configured loopback LM Studio worker and Jev-style decision model for small, explicit text tasks.',
      source: 'bundled', provider: name, content,
      invocation: { modelInvocable: true, userInvocable: true },
    });
  });
}
