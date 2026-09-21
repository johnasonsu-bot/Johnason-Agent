const REDACTED = '[REDACTED]';

const SENSITIVE_FIELD = /^(?:api[_-]?key|authorization|cookie|credential|credentials|password|passwd|passphrase|secret|token|access[_-]?token|refresh[_-]?token)$/i;
const SENSITIVE_EVENT = /(?:^|[/.:_-])(?:credential|credentials|vault)(?:$|[/.:_-])/i;
const RECOGNIZABLE_SECRET = /(?:\bBearer\s+[A-Za-z0-9._~+/-]{8,}|\bsk-[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)/i;

function sanitizerError(code, message) {
  const error = new TypeError(message);
  error.code = code;
  return error;
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function cloneAndSanitize(value, path, redactions, seen) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    if (typeof value === 'string' && RECOGNIZABLE_SECRET.test(value)) {
      redactions.push(path);
      return REDACTED;
    }
    return value;
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw sanitizerError('MEMORY_INVALID_EVENT', 'event data must be JSON serializable');
    return value;
  }
  if (typeof value !== 'object') {
    throw sanitizerError('MEMORY_INVALID_EVENT', 'event data must be JSON serializable');
  }
  if (seen.has(value)) throw sanitizerError('MEMORY_INVALID_EVENT', 'event data must not be cyclic');
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      return value.map((item, index) => cloneAndSanitize(item, `${path}[${index}]`, redactions, seen));
    }
    if (!isPlainObject(value)) {
      throw sanitizerError('MEMORY_INVALID_EVENT', 'event data must contain only JSON objects and arrays');
    }
    const result = {};
    for (const [key, item] of Object.entries(value)) {
      const itemPath = path ? `${path}.${key}` : key;
      if (SENSITIVE_FIELD.test(key)) {
        redactions.push(itemPath);
        result[key] = REDACTED;
      } else {
        result[key] = cloneAndSanitize(item, itemPath, redactions, seen);
      }
    }
    return result;
  } finally {
    seen.delete(value);
  }
}

export function sanitizeEvent(event) {
  if (!isPlainObject(event)
      || !Number.isSafeInteger(event.seq)
      || event.seq < 0
      || typeof event.type !== 'string'
      || event.type.trim().length === 0
      || !Object.hasOwn(event, 'data')) {
    throw sanitizerError('MEMORY_INVALID_EVENT', 'event must have a non-negative seq, type, and data');
  }
  if (event.time !== undefined && (!Number.isSafeInteger(event.time) || event.time < 0)) {
    throw sanitizerError('MEMORY_INVALID_EVENT', 'event time must be a non-negative safe integer');
  }
  if (event.sourceEventSeqs !== undefined
      && (!Array.isArray(event.sourceEventSeqs)
        || event.sourceEventSeqs.some(seq => !Number.isSafeInteger(seq) || seq < 0 || seq >= event.seq))) {
    throw sanitizerError('MEMORY_INVALID_EVENT', 'sourceEventSeqs must contain prior non-negative sequences');
  }
  if (SENSITIVE_EVENT.test(event.type)) return { event: null, redactions: [] };

  const redactions = [];
  const data = cloneAndSanitize(event.data, 'data', redactions, new Set());
  const sanitized = { seq: event.seq, type: event.type, data };
  if (Object.hasOwn(event, 'time')) sanitized.time = event.time;
  // Keep the stable event field order used by callers: seq, type, time, data.
  const ordered = Object.hasOwn(event, 'time')
    ? { seq: sanitized.seq, type: sanitized.type, time: sanitized.time, data: sanitized.data }
    : sanitized;
  if (event.sourceEventSeqs !== undefined) ordered.sourceEventSeqs = [...event.sourceEventSeqs];
  if (event.surfaceOp !== undefined) {
    ordered.surfaceOp = cloneAndSanitize(event.surfaceOp, 'surfaceOp', redactions, new Set());
  }
  if (event.ignorable === true) ordered.ignorable = true;
  return { event: ordered, redactions };
}
