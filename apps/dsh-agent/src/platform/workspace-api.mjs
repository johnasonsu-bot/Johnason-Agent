/** Native Workspace request shapes used by the local memory UI. */
export async function createWorkspaceSession(sessionController, workspaceController, { sessionId, workspaceRoot }) {
  const result = await workspaceController.create({ path: workspaceRoot });
  const workspace = result?.workspace;
  if (typeof workspace?.workspaceId !== 'string' || !workspace.workspaceId || workspace.path !== workspaceRoot) {
    throw new Error('Native workspace identity mismatch');
  }
  const created = await sessionController.create({ sessionId, workspaceId: workspace.workspaceId });
  if (created?.sessionId !== sessionId) throw new Error('Native session identity mismatch');
  return created;
}

/** Associate a proven existing Session without resuming it or rewriting its log. */
export async function attachSessionWorkspace(workspaceRegistry, { sessionId, workspaceRoot }) {
  // Native create serializes canonical-path deduplication across concurrent callers.
  const workspace = await workspaceRegistry.create(workspaceRoot);
  if (workspace?.path !== workspaceRoot || typeof workspace.attachSession !== 'function') {
    throw new Error('Native workspace identity mismatch');
  }
  // The native entity validates the persisted/live Session's immutable cwd.
  await workspace.attachSession(sessionId);
}
