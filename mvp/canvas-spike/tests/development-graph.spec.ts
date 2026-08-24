import { _electron as electron, expect, test } from "@playwright/test";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const python = path.resolve("../.venv/bin/python");

function installDevelopmentGraphFixtures(runtimeDir: string, interruptKind: "release_approval" | "branch_review" = "release_approval"): void {
  fs.mkdirSync(runtimeDir, { recursive: true });
  const script = String.raw`
import sys
from pathlib import Path
from workbench.agents.repository import AgentProfileRepository
from workbench.conversations.repository import ConversationRepository
from workbench.models.profiles import ProviderProfileRecord
from workbench.protocol.events import DomainEvent
from workbench.providers.repository import ProviderRepository
from workbench.workflow.event_store import EventStore
from workbench.orchestration.development_jobs import DevelopmentJobRepository
from workbench.orchestration.development import CommandPolicy, DevelopmentNodeSpec, DevelopmentPlan, FileOwnership, GitOutputContract

db = Path(sys.argv[1]) / "workbench.sqlite"
providers = ProviderRepository(db)
providers.save(ProviderProfileRecord(id="lmstudio", name="LM Studio", protocol="openai", base_url="http://127.0.0.1:1234/v1"))
ConversationRepository(db).create_session("ui-session-0")
events = [
  ("development.plan.approved", {"graph_run_id":"development-run.fixture", "plan_id":"development-plan.fixture", "status":"approved"}),
  ("development.branch.progress", {"graph_run_id":"development-run.fixture", "branch_id":"backend", "attempt":1, "worktree_display_name":"backend-worktree", "worker_branch":"graph/development-run.fixture/backend", "base_sha":"a" * 40, "commit_sha":"b" * 40, "owned_path_summary":["mvp/src/workbench/api/conversations.py"], "test_label":"Backend unit tests", "test_result":"passed", "private_environment":{"API_KEY":"secret-value"}, "raw_command":["git", "reset", "--hard"]}),
  ("development.local_review.decided", {"graph_run_id":"development-run.fixture", "branch_id":"backend", "attempt":1, "decision":sys.argv[2], "findings":["historical review"] if sys.argv[2] == "needs_human" else []}),
  ("development.merge.completed", {"graph_run_id":"development-run.fixture", "status":"merged", "integration_branch":"graph/development-run.fixture/integration", "base_sha":"a" * 40, "commits":["b" * 40], "integration_sha":"c" * 40}),
  ("development.global_verification.decided", {"graph_run_id":"development-run.fixture", "decision":"approved", "test_label":"临时集成分支测试", "test_result":"passed", "global_verifier":"approved"}),
  ("development.interrupt.required", {"graph_run_id":"development-run.fixture", "interrupt_id":"branch.fixture.current" if sys.argv[3] == "branch_review" else "release.fixture", "interrupt_kind":sys.argv[3], "pending_branch_ids":["frontend"] if sys.argv[3] == "branch_review" else [], "status":"needs_human"}),
]
store = EventStore(db)
for index, (kind, payload) in enumerate(events):
    store.append(DomainEvent.new(kind, "fixture", payload, run_id="ui-session-0"), command_id=f"development-fixture-{index}")
jobs = DevelopmentJobRepository(db)
plan = DevelopmentPlan(plan_id="development-plan.fixture", nodes=(DevelopmentNodeSpec(
    node_id="backend", repository_root=Path.cwd().parent, base_commit="a" * 40,
    ownership=FileOwnership(writable_paths=("src/workbench/api/conversations.py",)),
    command_policy=CommandPolicy(allowed_commands=(("python", "-m", "pytest", "-q"),), tests=(("python", "-m", "pytest", "-q"),)),
    output=GitOutputContract(branch="graph/development-run.fixture/backend"),
),))
jobs.admit("development-run.fixture", "ui-session-0", plan)
jobs.mark_needs_human("development-run.fixture", interrupt_id="branch.fixture.current" if sys.argv[3] == "branch_review" else "release.fixture", interrupt_kind=sys.argv[3], interrupt_payload={"kind":"branch_reviews", "reviews":{"frontend":{"attempt":1}}} if sys.argv[3] == "branch_review" else {"kind":"release_approval"})
`;
  const result = spawnSync(python, ["-c", script, runtimeDir, interruptKind === "branch_review" ? "needs_human" : "approved", interruptKind], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || "development fixture setup failed");
}

function appendDevelopmentInterrupt(runtimeDir: string, interruptId: string, interruptKind: string, pendingBranchIds: string[] = []): void {
  const script = String.raw`
import sys
from pathlib import Path
from workbench.protocol.events import DomainEvent
from workbench.workflow.event_store import EventStore

store = EventStore(Path(sys.argv[1]) / "workbench.sqlite")
if sys.argv[3] == "branch_review":
  store.append(DomainEvent.new("development.local_review.decided", "fixture", {
    "graph_run_id": "development-run.fixture", "branch_id": "backend", "attempt": 1,
    "decision": "needs_human", "findings": ["historical review"],
  }, run_id="ui-session-0"), command_id="development-fixture-historical-review")
store.append(DomainEvent.new("development.interrupt.required", "fixture", {
  "graph_run_id": "development-run.fixture",
  "interrupt_id": sys.argv[2],
  "interrupt_kind": sys.argv[3],
  "pending_branch_ids": __import__("json").loads(sys.argv[4]),
  "status": "needs_human",
}, run_id="ui-session-0"), command_id=f"development-fixture-next-{sys.argv[2]}")
`;
  const result = spawnSync(python, ["-c", script, runtimeDir, interruptId, interruptKind, JSON.stringify(pendingBranchIds)], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || "development fixture event append failed");
}

test("development graph shows isolated branches and waits for release approval", async ({}, testInfo) => {
  const runtimeDir = testInfo.outputPath("runtime");
  installDevelopmentGraphFixtures(runtimeDir);
  const app = await electron.launch({ args: [path.resolve(".")], env: { ...process.env, HERMES_PYTHON: python, HERMES_RUNTIME_DIR: runtimeDir } });
  try {
    const page = await app.firstWindow();
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    const graph = page.getByRole("region", { name: "开发图运行" });
    await expect(graph.getByText("backend · 独立 Worktree")).toBeVisible();
    await expect(graph.getByText("临时集成分支测试通过")).toBeVisible();
    await expect(graph.getByRole("button", { name: "批准进入目标分支" })).toBeVisible();
    await expect(graph).not.toContainText("secret-value");
    await expect(graph).not.toContainText("reset");
    await graph.getByRole("button", { name: "批准进入目标分支" }).click();
    await expect(graph.getByRole("button", { name: "审批已提交" })).toBeVisible();
    appendDevelopmentInterrupt(runtimeDir, "integration.fixture.next", "integration_approval");
    await expect(graph.getByRole("button", { name: "批准临时集成" })).toBeVisible();
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/sessions/ui-session-0/development-runs/development-run.fixture/interrupts/release.fixture" }))).rejects.toThrow("invalid local API request");
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "POST", path: "/sessions/ui-session-0/development-runs/development-run.fixture/interrupts/release.fixture/extra" }))).rejects.toThrow("invalid local API request");
  } finally {
    await app.close();
  }
});

test("development branch review approves only IDs supplied by the current interrupt", async ({}, testInfo) => {
  const runtimeDir = testInfo.outputPath("runtime-current-scope");
  installDevelopmentGraphFixtures(runtimeDir, "branch_review");
  const app = await electron.launch({ args: [path.resolve(".")], env: { ...process.env, HERMES_PYTHON: python, HERMES_RUNTIME_DIR: runtimeDir } });
  try {
    const page = await app.firstWindow();
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    const branchApproval = page.getByRole("button", { name: "批准分支审核并继续" });
    await expect(branchApproval).toBeVisible();
    await expect(branchApproval).toBeEnabled();
    await branchApproval.click();
    const archive = () => spawnSync(python, ["-c", String.raw`
import json, sys
from pathlib import Path
from workbench.workflow.store import WorkflowStore
with WorkflowStore(Path(sys.argv[1]) / "workbench.sqlite").connect() as connection:
    row = connection.execute("SELECT response_json FROM development_job_resolved_interrupts WHERE graph_run_id=? AND interrupt_id=?", ("development-run.fixture", "branch.fixture.current")).fetchone()
print(row["response_json"] if row else "")
`, runtimeDir], { encoding: "utf8" });
    await expect.poll(() => archive().stdout.trim()).toBe('{"decisions":{"frontend":"approved"}}');
  } finally {
    await app.close();
  }
});
