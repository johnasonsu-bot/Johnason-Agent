import test from 'node:test';
import assert from 'node:assert/strict';
import { validateMemoryAcceptance } from '../scripts/memory-acceptance-result.mjs';

// Minimal, nonsecret shape captured from the retained c5RjwQ real model report.
// No model or native service is started to check this pure evidence predicate.
const sessionId = 'session-69ffac30-7357-4e17-a8fe-ebfdcc09af36';
const call = (name, id) => ({ event: { type: 'assistant/message', data: { message: { content: [{ type: 'tool-call', name, id }] } } } });
function fixture() {
  const turnEnd = { type: 'turn/end', data: { reason: { kind: 'completed' } } };
  return {
    sessionId, artifact: 'PAGED_SANDBOX_ACCEPTANCE_OK\n', calls: ['memory_page_in', 'memory_sandbox_run'], turnEnd,
    history: { events: [call('memory_page_in', '173949047'), call('memory_sandbox_run', '545367629'), { event: turnEnd }] },
    effects: { nodes: [{ sessionId, callId: '545367629', toolName: 'memory_sandbox_run', state: 'COMMITTED',
      result: { state: 'COMMITTED', backend: 'native-seatbelt', enforcement: 'full', started: true, exitCode: 0, timedOut: false, cancelled: false, denied: false, runnerFailure: false } }] },
  };
}
test('accepts the exact page-in then native-seatbelt write evidence from the completed fixture', () => {
  assert.equal(validateMemoryAcceptance(fixture()), true);
});

for (const [name, change] of [
  ['an extra native tool despite a correct cached calls summary', r => r.history.events.splice(1, 0, call('read', 'extra'))],
  ['reversed page/write order', r => r.history.events.splice(0, 2, r.history.events[1], r.history.events[0])],
  ['a repeated write invocation', r => r.history.events.splice(2, 0, call('memory_sandbox_run', 'second-write'))],
  ['a triggered acceptance bound', r => { r.limit = 'step limit reached'; }],
  ['an unconfined backend', r => { r.effects.nodes[0].result.backend = 'unconfined'; }],
  ['missing full enforcement', r => { r.effects.nodes[0].result.enforcement = 'none'; }],
  ['a nonzero process exit', r => { r.effects.nodes[0].result.exitCode = 1; }],
  ['an unrelated effect callId', r => { r.effects.nodes[0].callId = 'other-call'; }],
  ['an effect from another session', r => { r.effects.nodes[0].sessionId = 'other-session'; }],
  ['an extra effect', r => { r.effects.nodes.push(structuredClone(r.effects.nodes[0])); }],
  ['a cancelled process', r => { r.effects.nodes[0].result.cancelled = true; }],
  ['a missing native turn end despite a completed summary', r => r.history.events.pop()],
]) {
  test(`rejects ${name}`, () => {
    const report = fixture(); change(report);
    assert.equal(validateMemoryAcceptance(report), false);
  });
}
