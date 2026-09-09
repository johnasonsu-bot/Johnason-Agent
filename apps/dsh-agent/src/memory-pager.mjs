const MAX_SEARCH_LIMIT = 100;
const MAX_PAGE_CHARS = 100_000;
const MAX_BUDGET_TOKENS = 1_000_000;
const MAX_LINKED_GROUP = 1_000;

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
  const range = Object.hasOwn(page, 'offset') ? `;offset=${page.offset};total=${page.totalChars};next=${page.nextOffset}` : '';
  const header = `[memory:${page.id}@${page.version}${range};sources=${sources}] ${page.summary}`;
  return Object.hasOwn(page, 'contentText') ? `${header}\n${page.contentText}` : header;
}

function eventSeqs(page, sessionId) {
  if (page.kind !== 'episodic') return [];
  return page.sourceRefs
    .filter(source => source.sessionId === sessionId && Number.isSafeInteger(source.seq))
    .map(source => source.seq);
}

function relatedEpisodes(store, scope, seq) {
  return store.db.prepare(`
    WITH latest AS (
      SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id, id ORDER BY version DESC
      ) AS rank
      FROM memory_records
      WHERE project_id = ? AND kind = 'episodic'
        AND (visibility = 'project' OR (session_id = ? AND agent_id = ?))
    )
    SELECT DISTINCT latest.id, latest.version
    FROM latest, json_each(latest.source_refs_json) AS source
    WHERE latest.rank = 1
      AND json_extract(source.value, '$.type') = 'event'
      AND json_extract(source.value, '$.projectId') = ?
      AND json_extract(source.value, '$.sessionId') = ?
      AND json_extract(source.value, '$.agentId') = ?
      AND json_extract(source.value, '$.seq') = ?
    LIMIT ?
  `).all(
    scope.projectId,
    scope.sessionId,
    scope.agentId,
    scope.projectId,
    scope.sessionId,
    scope.agentId,
    seq,
    MAX_LINKED_GROUP + 1,
  );
}

function groupFor(page, scope, loadedById, store) {
  if (page.kind !== 'episodic') return [page];
  const exactEpisodes = new Map([[page.id, page]]);
  const pendingSeqs = [...eventSeqs(page, scope.sessionId)];
  const visitedSeqs = new Set();
  while (pendingSeqs.length > 0) {
    const seq = pendingSeqs.shift();
    if (visitedSeqs.has(seq)) continue;
    visitedSeqs.add(seq);

    const sourceId = `event:${scope.sessionId}:${seq}`;
    const sourceRecord = store.get(scope, sourceId);
    if (sourceRecord === null || sourceRecord.kind !== 'episodic') return null;
    if (!exactEpisodes.has(sourceId)) {
      exactEpisodes.set(sourceId, loadedById.get(sourceId) ?? handleFor(sourceRecord));
      if (exactEpisodes.size > MAX_LINKED_GROUP) return null;
    }

    const related = relatedEpisodes(store, scope, seq);
    if (related.length > MAX_LINKED_GROUP) return null;
    for (const relation of related) {
      if (exactEpisodes.has(relation.id)) continue;
      const record = store.get(scope, relation.id, relation.version);
      if (record === null || record.kind !== 'episodic') return null;
      const exact = loadedById.get(record.id) ?? handleFor(record);
      exactEpisodes.set(record.id, exact);
      pendingSeqs.push(...eventSeqs(exact, scope.sessionId));
      if (exactEpisodes.size > MAX_LINKED_GROUP) return null;
    }
  }
  return [...exactEpisodes.values()]
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
    return this.#store.search(scope, query, { limit }).map(handleFor);
  }

  pageIn(scope, id, options = {}) {
    requireOptions(options, 'pageIn options');
    const maxChars = validateLimit(options.maxChars, 4_000, MAX_PAGE_CHARS);
    if (typeof id !== 'string' || id.length === 0) {
      throw pagerError('MEMORY_INVALID_QUERY', 'id must be a non-empty string', TypeError);
    }
    const record = this.#store.readPage(scope, id, { ...options, maxChars });
    if (record === null) throw pagerError('MEMORY_NOT_FOUND', 'memory was not found or is not visible');
    const { contentText, truncated, offset, totalChars, nextOffset, hasMore, offsetUnit } = record;
    const page = { ...handleFor(record), contentText, truncated, offset, maxChars, totalChars, nextOffset, hasMore, offsetUnit };
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
    const pages = [];
    const processedIds = new Set();
    for (const page of [...loaded, ...handles]) {
      if (processedIds.has(page.id)) continue;
      const group = groupFor(page, scope, loadedById, this.#store);
      if (group === null) {
        processedIds.add(page.id);
        continue;
      }
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
