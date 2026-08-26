import { _electron as electron, expect, test } from "@playwright/test";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const python = path.resolve("../.venv/bin/python");

function installSequentialFixtures(runtimeDir: string): void {
  fs.mkdirSync(runtimeDir, { recursive: true });
  const script = String.raw`
import sys
from pathlib import Path
from workbench.agents.models import AgentProfileWrite
from workbench.agents.repository import AgentProfileRepository
from workbench.artifacts.store import ArtifactStore
from workbench.conversations.repository import ConversationRepository
from workbench.models.profiles import ProviderProfileRecord
from workbench.protocol.events import DomainEvent
from workbench.providers.repository import ProviderRepository
from workbench.workflow.event_store import EventStore

runtime = Path(sys.argv[1])
db = runtime / "workbench.sqlite"
providers = ProviderRepository(db)
providers.save(ProviderProfileRecord(id="lmstudio", name="LM Studio", protocol="openai", base_url="http://127.0.0.1:1234/v1"))
providers.save(ProviderProfileRecord.deepseek(id="deepseek-primary"))
agents = AgentProfileRepository(db)
for item in (
    ("product-manager", "产品经理", "worker", "lmstudio", "local-agent"),
    ("supervisor", "Supervisor", "supervisor", "deepseek-primary", "deepseek-v4-flash"),
    ("architect", "架构师", "worker", "deepseek-primary", "deepseek-v4-flash"),
    ("verifier", "Verifier", "verifier", "deepseek-primary", "deepseek-v4-flash"),
):
    agents.create(AgentProfileWrite(agent_id=item[0], display_name=item[1], role=item[2], provider_id=item[3], model=item[4]))
ConversationRepository(db).create_session("ui-session-0")
artifact = ArtifactStore(db, runtime / "artifacts").put_bytes(b"<html><style>@keyframes fly{to{transform:translateX(30px)}}</style><body><div style='animation:fly 1s infinite'>flight</div></body></html>", "text/html", {"artifact_kind":"html_animation"})
store = EventStore(db)
events = [
 ("orchestration.graph.queued", {"command_id":"seed-cmd","plan_id":"plan.seed","graph_run_id":"graph-run.seed","status":"queued"}),
]
sequence = 0
for node, agent, attempt in (("node.pm","product-manager",1),("node.sup","supervisor",1),("node.pm","product-manager",2),("node.sup","supervisor",2),("node.arch","architect",1),("node.ver","verifier",1),("node.arch","architect",2),("node.ver","verifier",2)):
    sequence += 1
    events.append(("orchestration.node.progress", {"graph_run_id":"graph-run.seed","node_id":node,"agent_id":agent,"attempt":attempt,"stage":"completed","status":"completed","label":"completed","sequence":sequence,"percentage":100}))
for reviewer, target, attempt, decision in (("node.sup","node.pm",1,"rejected"),("node.sup","node.pm",2,"approved"),("node.ver","node.arch",1,"rejected"),("node.ver","node.arch",2,"approved")):
    events.append(("orchestration.review.decided", {"graph_run_id":"graph-run.seed","reviewer_node_id":reviewer,"reviewed_node_id":target,"reviewed_attempt":attempt,"decision":decision,"findings":["需要返工"] if decision == "rejected" else [],"evidence_refs":[f"evidence.{reviewer}.{attempt}"]}))
events.append(("orchestration.artifact.published", {"graph_run_id":"graph-run.seed","node_id":"node.arch","agent_id":"architect","attempt":2,"artifact_id":artifact.artifact_id,"media_type":"text/html"}))
events.append(("conversation.turn.finished", {"command_id":"seed-cmd","status":"completed"}))
for index, (kind, payload) in enumerate(events):
    store.append(DomainEvent.new(kind, "fixture", payload, run_id="ui-session-0"), command_id=f"fixture-{index}")
`;
  const result = spawnSync(python, ["-c", script, runtimeDir], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || "fixture setup failed");
}

function installProviderFixtures(runtimeDir: string): void {
  fs.mkdirSync(runtimeDir, { recursive: true });
  const script = "from pathlib import Path; import sys; from workbench.models.profiles import ProviderProfileRecord; from workbench.providers.repository import ProviderRepository; db=Path(sys.argv[1])/'workbench.sqlite'; repo=ProviderRepository(db); repo.save(ProviderProfileRecord(id='lmstudio',name='LM Studio',protocol='openai',base_url='http://127.0.0.1:1234/v1')); repo.save(ProviderProfileRecord.deepseek(id='deepseek-primary'))";
  const result = spawnSync(python, ["-c", script, runtimeDir], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || "provider fixture setup failed");
}

test("shows sequential reviews, rework, recovery and html preview", async ({}, testInfo) => {
  const runtimeDir = testInfo.outputPath("runtime");
  installSequentialFixtures(runtimeDir);
  const app = await electron.launch({ args: [path.resolve(".")], env: { ...process.env, HERMES_PYTHON: python, HERMES_RUNTIME_DIR: runtimeDir } });
  try {
    const page = await app.firstWindow();
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    const graph = page.getByRole("region", { name: "多 Agent 执行图" });
    await expect(graph).toBeVisible();
    await expect(graph.getByText("产品经理 · Attempt 2")).toBeVisible();
    await expect(graph.getByText("Supervisor · 第 2 轮审核通过")).toBeVisible();
    await expect(graph.getByText("架构师 · Attempt 2")).toBeVisible();
    await expect(graph.getByText("Verifier · 第 2 轮审核通过")).toBeVisible();
    const preview = page.locator('iframe[title="animation.html"]');
    await expect(preview).toBeVisible();
    await expect(preview).toHaveAttribute("sandbox", "allow-scripts");
    await expect(page.getByRole("link", { name: "下载" })).toBeVisible();
  } finally {
    await app.close();
  }
});

test("persists credential-free Agent versions through the backend API", async ({}, testInfo) => {
  const runtimeDir = testInfo.outputPath("agent-runtime");
  installProviderFixtures(runtimeDir);
  const app = await electron.launch({ args: [path.resolve(".")], env: { ...process.env, HERMES_PYTHON: python, HERMES_RUNTIME_DIR: runtimeDir } });
  try {
    const page = await app.firstWindow();
    await page.getByRole("button", { name: "Agent 配置" }).click();
    await page.getByRole("button", { name: "保存 Agent 配置" }).click();
    await expect(page.getByRole("status")).toContainText("已保存到本地运行时");
    await page.reload();
    await page.getByRole("button", { name: "Agent 配置" }).click();
    await expect(page.getByText("需求拆解与内容创作 · v1")).toBeVisible();
    await expect(page.getByText("审核与返工决策 · v1")).toBeVisible();
    await page.getByLabel("产品经理 Model").fill("local-agent-v2");
    await page.getByRole("button", { name: "保存 Agent 配置" }).click();
    await expect(page.getByText("需求拆解与内容创作 · v2")).toBeVisible();
    await page.getByRole("button", { name: "◌ 会话" }).click();
    const prompt = "@产品经理 写一篇200字小说 @Supervisor 审核小说 @架构师 改写成一个动画html @Verifier 验证HTML";
    await page.getByRole("textbox", { name: "会话消息" }).fill(prompt);
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByText(prompt, { exact: true }).last()).toBeVisible();
    await expect(page.getByTestId("conversation-status")).toContainText(/多 Agent|执行中|等待重试/);
  } finally {
    await app.close();
  }
});
