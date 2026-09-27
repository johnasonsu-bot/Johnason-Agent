export const inject = ['settings', 'tools', 'credentials'];

export async function apply(ctx, config = {}) {
  if (typeof config.implementation !== 'string' || !config.implementation) {
    throw new Error('本地模型实现路径尚未配置。');
  }
  const module = await import(config.implementation);
  if (typeof module.apply !== 'function') throw new Error('本地模型实现不可用。');
  return module.apply(ctx, config.settings ?? {});
}
