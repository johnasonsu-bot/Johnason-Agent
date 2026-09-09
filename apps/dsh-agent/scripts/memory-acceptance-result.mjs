/** Pure acceptance evidence check; does not read files, start services or request a model. */
export function validateMemoryAcceptance(report) {
  if (!report || report.limit !== undefined || report.error !== undefined
      || report.artifact !== 'PAGED_SANDBOX_ACCEPTANCE_OK\n'
      || !Array.isArray(report.history?.events) || !Array.isArray(report.effects?.nodes)) return false;
  const events = report.history.events.map(entry => entry.event);
  // Native history, not the convenient report.calls/turnEnd summaries, is the evidence.
  const calls = events.filter(event => event?.type === 'assistant/message')
    .flatMap(event => event.data?.message?.content ?? []).filter(block => block.type === 'tool-call');
  const ends = events.filter(event => event?.type === 'turn/end');
  if (calls.length !== 2 || calls[0].name !== 'memory_page_in' || calls[1].name !== 'memory_sandbox_run'
      || ends.length !== 1 || ends[0].data?.reason?.kind !== 'completed' || report.effects.nodes.length !== 1) return false;
  const effect = report.effects.nodes[0], result = effect?.result;
  return typeof report.sessionId === 'string' && effect?.sessionId === report.sessionId
    && typeof calls[1].id === 'string' && effect.callId === calls[1].id && effect.toolName === 'memory_sandbox_run'
    && effect.state === 'COMMITTED' && result?.state === 'COMMITTED' && result.started === true && result.exitCode === 0
    && result.backend === 'native-seatbelt' && result.enforcement === 'full'
    && result.timedOut === false && result.cancelled === false && result.denied === false && result.runnerFailure === false;
}
