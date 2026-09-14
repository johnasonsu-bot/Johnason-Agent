/** Native registry tools: arguments never contain actors, project scope, or policy overrides. */
export function installMemoryTools(ctx, service) {
  const string = { type: 'string' };
  const definitions = [
    ['memory_search', 'Search authorized memory; returns summaries and handles only.', { query: string, limit: { type: 'integer' } }, ['query']],
    ['memory_page_in', 'Select a bounded memory JSON-text range for the next logged context. For continuation use the returned version and nextOffset (UTF-16 units) as offset.', { id: string, version: { type: 'integer' }, offset: { type: 'integer' }, maxChars: { type: 'integer' } }, ['id']],
    ['memory_page_out', 'Remove a page from the working set, without deleting source records.', { id: string }, ['id']],
    ['memory_semantic', 'Append a sourced semantic entity/relation version.', { record: { type: 'object' } }, ['record']],
    ['memory_procedural_candidate', 'Propose a sourced candidate procedure. Cannot confirm or protect rules.', { record: { type: 'object' } }, ['record']],
    ['memory_sandbox_run', 'Run literal argv using the task file sandbox and native approval. Examples: {"argv":["pwd"]}, {"argv":["ls","-la","."]}, {"argv":["sed","-n","1,120p","README.md"]}. No implicit shell expansion, pipes, or redirection. Network/resource isolation is not provided. Repeated call IDs never repeat unknown side effects.', { argv: { type: 'array', items: string } }, ['argv']],
  ];
  for (const [name, description, properties, required] of definitions) {
    ctx.tools.register({ name, description,
      parameters: { type: 'object', properties, required, additionalProperties: false },
      output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      async execute(args, exec) {
        if (!args || Array.isArray(args) || typeof args !== 'object'
            || Object.keys(args).some(key => !Object.hasOwn(properties, key))
            || required.some(key => !Object.hasOwn(args, key))) throw new Error('MEMORY_INVALID_TOOL_ARGUMENTS');
        return service.invokeTool(name, args, exec);
      },
    });
  }
}

export const MEMORY_TOOL_NAMES = new Set(['memory_search', 'memory_page_in', 'memory_page_out', 'memory_semantic', 'memory_procedural_candidate', 'memory_sandbox_run']);
