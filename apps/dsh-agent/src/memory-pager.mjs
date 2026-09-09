const MAX_SEARCH_LIMIT = 100;
const MAX_PAGE_CHARS = 100_000;
const MAX_BUDGET_TOKENS = 1_000_000;

function pagerError(code, message, ErrorClass = Error) {
  const error = new ErrorClass(message);
  error.code = code;
  return error;
}

function requireOptions(options, name) {
  if (options === null || typeof options !== 'object' || Array.isArray(options)) {
    throw pagerError('MEMORY_INVALID_QUERY', `${name} must be an object`, TypeError);
  }
  return options;
}

function validateLimit(value, defaultValue, maximum) {
  const limit = value ?? defaultValue;
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > maximum) {
    throw pagerError('MEMORY_INVALID_LIMIT', `limit must be from 1 to ${maximum}`, RangeError);
  }
  return limit;
}

function handleFor(record) {
  return {
    id: record.id,
    version: record.version,
    kind: record.kind,
    visibility: record.visibility,
    summary: record.summary,
    sourceRefs: structuredClone(record.sourceRefs),
    validTime: record.validTime,
    systemTime: record.systemTime,
    status: record.status,
  };
}

function scopeKey(scope) {
  return JSON.stringify([scope.projectId, scope.sessionId, scope.agentId]);
}

function tokensFor(text) {
  return Math.ceil(text.length / 4);
}

function anchorText(anchor) {
  if (typeof anchor === 'string' && anchor.length > 0) return `[anchor] ${anchor}`;
  if (anchor
      && typeof anchor === 'object'
      && !Array.isArray(anchor)
      && typeof anchor.text === 'string'
      && anchor.text.length > 0
      && (anchor.label === undefined || (typeof anchor.label === 'string' && anchor.label.length > 0))) {
    return `[anchor${anchor.label === undefined ? '' : `:${anchor.label}`}] ${anchor.text}`;
  }
  throw pagerError('MEMORY_INVALID_ANCHOR', 'anchors must be strings or { label?, text } objects', TypeError);
}

function contextForPage(page) {
  const sources = JSON.stringify(page.sourceRefs);
  const header = `[memory:${page.id}@${page.version};sources=${sources}] ${page.summary}`;
  return Object.hasOwn(page, 'contentText') ? `${header}\n${page.contentText}` : header;
}

function eventSeqs(page, sessionId) {
  if (page.kind !== 'episodic') return [];
  return page.sourceRefs
    .filter(source => source.sessionId === sessionId && Number.isSafeInteger(source.seq))
    .map(source => source.seq);
}

function groupFor(page, scope, episodes, loadedById) {
  if (page.kind !== 'episodic') return [page];
  const seqs = new Set(eventSeqs(page, scope.sessionId));
  const groupedIds = new Set([page.id]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const episode of episodes) {
      if (groupedIds.has(episode.id)) continue;
      const episodeSeqs = eventSeqs(episode, scope.sessionId);
      if (!episodeSeqs.some(seq => seqs.has(seq))) continue;
      groupedIds.add(episode.id);
      for (const seq of episodeSeqs) seqs.add(seq);
      changed = true;
    }
  }
  return episodes
    .filter(episode => groupedIds.has(episode.id))
    .sort((left, right) => Math.max(...eventSeqs(left, scope.sessionId))
      - Math.max(...eventSeqs(right, scope.sessionId)))
    .map(episode => loadedById.get(episode.id) ?? episode);
}

export class MemoryPager {
  #store;
  #workingSets = new Map();

  constructor(store) {
    if (!store || typeof store.list !== 'function' || typeof store.get !== 'function') {
      throw pagerError('MEMORY_INVALID_STORE', 'store must be a MemoryStore-compatible object', TypeError);
    }
    this.#store = store;
  }

  search(scope, query, options = {}) {
    requireOptions(options, 'search options');
    if (typeof query !== 'string' || query.trim().length === 0) {
      throw pagerError('MEMORY_INVALID_QUERY', 'query must be a non-empty string', TypeError);
    }
    const limit = validateLimit(options.limit, 20, MAX_SEARCH_LIMIT);
    const needle = query.trim().toLocaleLowerCase();
    const candidates = this.#store.list(scope, { limit: 1_000 });
    return candidates
      .map(record => {
        const summary = record.summary.toLocaleLowerCase();
        const content = JSON.stringify(record.content).toLocaleLowerCase();
        const summaryIndex = summary.indexOf(needle);
        const contentIndex = content.indexOf(needle);
        if (summaryIndex < 0 && contentIndex < 0) return null;
        return {
          record,
          score: (summaryIndex >= 0 ? 100 - Math.min(summaryIndex, 99) : 0)
            + (contentIndex >= 0 ? 10 - Math.min(contentIndex, 9) : 0),
        };
      })
      .filter(Boolean)
      .sort((left, right) => right.score - left.score
        || right.record.systemTime - left.record.systemTime
        || left.record.id.localeCompare(right.record.id))
      .slice(0, limit)
      .map(({ record }) => handleFor(record));
  }

  pageIn(scope, id, options = {}) {
    requireOptions(options, 'pageIn options');
    const maxChars = validateLimit(options.maxChars, 4_000, MAX_PAGE_CHARS);
    if (typeof id !== 'string' || id.length === 0) {
      throw pagerError('MEMORY_INVALID_QUERY', 'id must be a non-empty string', TypeError);
    }
    const record = this.#store.get(scope, id, options.version);
    if (record === null) throw pagerError('MEMORY_NOT_FOUND', 'memory was not found or is not visible');
    const serialized = JSON.stringify(record.content);
    const page = {
      ...handleFor(record),
      contentText: serialized.slice(0, maxChars),
      truncated: serialized.length > maxChars,
    };
    const key = scopeKey(scope);
    const pages = this.#workingSets.get(key) ?? new Map();
    pages.set(id, structuredClone(page));
    this.#workingSets.set(key, pages);
    return structuredClone(page);
  }

  pageOut(scope, id) {
    if (typeof id !== 'string' || id.length === 0) {
      throw pagerError('MEMORY_INVALID_QUERY', 'id must be a non-empty string', TypeError);
    }
    // Validate scope and store lifecycle through the same authorization boundary as pageIn.
    this.#store.list(scope, { limit: 1 });
    const pages = this.#workingSets.get(scopeKey(scope));
    if (!pages) return false;
    const removed = pages.delete(id);
    if (pages.size === 0) this.#workingSets.delete(scopeKey(scope));
    return removed;
  }

  select(scope, { query = '', budgetTokens, anchors = [] } = {}) {
    if (!Number.isSafeInteger(budgetTokens)
        || budgetTokens < 1
        || budgetTokens > MAX_BUDGET_TOKENS) {
      throw pagerError(
        'MEMORY_INVALID_BUDGET',
        `budgetTokens must be from 1 to ${MAX_BUDGET_TOKENS}`,
        RangeError,
      );
    }
    if (typeof query !== 'string') {
      throw pagerError('MEMORY_INVALID_QUERY', 'query must be a string', TypeError);
    }
    if (!Array.isArray(anchors)) {
      throw pagerError('MEMORY_INVALID_ANCHOR', 'anchors must be an array', TypeError);
    }
    // This also validates scope and ensures a closed store cannot serve stale working pages.
    this.#store.list(scope, { limit: 1 });

    const sections = anchors.map(anchorText);
    let contextText = sections.join('\n');
    if (tokensFor(contextText) > budgetTokens) {
      throw pagerError('MEMORY_BUDGET_EXCEEDED', 'mandatory anchors exceed the memory budget', RangeError);
    }

    const loaded = [...(this.#workingSets.get(scopeKey(scope))?.values() ?? [])]
      .map(page => structuredClone(page));
    const loadedById = new Map(loaded.map(page => [page.id, page]));
    const loadedIds = new Set(loaded.map(page => page.id));
    const handles = query.trim().length === 0
      ? []
      : this.search(scope, query, { limit: MAX_SEARCH_LIMIT }).filter(page => !loadedIds.has(page.id));
    const episodes = this.#store.list(scope, { kind: 'episodic', limit: 1_000 }).map(handleFor);
    const pages = [];
    const processedIds = new Set();
    for (const page of [...loaded, ...handles]) {
      if (processedIds.has(page.id)) continue;
      const group = groupFor(page, scope, episodes, loadedById);
      for (const groupedPage of group) processedIds.add(groupedPage.id);
      const pageText = group.map(contextForPage).join('\n');
      const candidate = contextText.length === 0 ? pageText : `${contextText}\n${pageText}`;
      if (tokensFor(candidate) > budgetTokens) continue;
      pages.push(...group);
      contextText = candidate;
    }
    return { pages, estimatedTokens: tokensFor(contextText), contextText };
  }
}
