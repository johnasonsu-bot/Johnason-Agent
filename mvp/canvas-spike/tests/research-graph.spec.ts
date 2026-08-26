import { _electron as electron, expect, test } from "@playwright/test";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const python = path.resolve("../.venv/bin/python");

function installResearchFixtures(runtimeDir: string): void {
  fs.mkdirSync(runtimeDir, { recursive: true });
  const script = String.raw`
import sys
from pathlib import Path
from workbench.agents.models import AgentProfileWrite
from workbench.agents.repository import AgentProfileRepository
from workbench.conversations.repository import ConversationRepository
from workbench.models.profiles import ProviderProfileRecord
from workbench.protocol.events import DomainEvent
from workbench.providers.repository import ProviderRepository
from workbench.workflow.event_store import EventStore

db = Path(sys.argv[1]) / "workbench.sqlite"
providers = ProviderRepository(db)
providers.save(ProviderProfileRecord(id="lmstudio", name="LM Studio", protocol="openai", base_url="http://127.0.0.1:1234/v1"))
providers.save(ProviderProfileRecord.deepseek(id="deepseek-primary"))
agents = AgentProfileRepository(db)
for item in (
    ("product-manager", "产品经理", "worker", "lmstudio", "local-agent"),
    ("supervisor", "Supervisor", "supervisor", "deepseek-primary", "deepseek-v4-flash"),
    ("architect", "架构师", "worker", "deepseek-primary", "deepseek-v4-flash"),
    ("verifier", "Verifier", "verifier", "deepseek-primary", "deepseek-v4-flash"),
): agents.create(AgentProfileWrite(agent_id=item[0], display_name=item[1], role=item[2], provider_id=item[3], model=item[4]))
ConversationRepository(db).create_session("ui-session-0")
events = []
events.append(("research.branch.progress", {"graph_run_id":"research-run.old","node_id":"node.old","branch_id":"research","attempt":9,"stage":"worker","status":"completed","evidence_refs":["evidence:old"]}))
events.append(("research.plan.approved", {"graph_run_id":"research-run.fixture","plan_id":"plan.fixture","version":1,"status":"approved"}))
for branch in ("research", "compare", "fact_check", "gap_analysis"):
    events.append(("research.branch.progress", {"graph_run_id":"research-run.fixture","node_id":f"node.{branch}","branch_id":branch,"attempt":1,"stage":"worker","status":"completed","evidence_refs":[f"evidence:{branch}:1"]}))
events.extend([
    ("research.local_review.decided", {"graph_run_id":"research-run.fixture","node_id":"node.fact-check.verifier","branch_id":"fact_check","attempt":1,"stage":"local_verifier","decision":"rejected","findings":["证据不足"],"evidence_refs":["review:fact-check:1"]}),
    ("research.branch.progress", {"graph_run_id":"research-run.fixture","node_id":"node.fact-check","branch_id":"fact_check","attempt":2,"stage":"worker","status":"completed","evidence_refs":["evidence:fact-check:2"]}),
    ("research.local_review.decided", {"graph_run_id":"research-run.fixture","node_id":"node.fact-check.verifier","branch_id":"fact_check","attempt":2,"stage":"local_verifier","decision":"approved","evidence_refs":["review:fact-check:2"]}),
    ("research.supervisor.decided", {"graph_run_id":"research-run.fixture","decision":"continue_to_merge","conflicts":["claim-a-vs-b"],"evidence_refs":["evidence:supervisor"]}),
    ("research.arbitration.decided", {"graph_run_id":"research-run.fixture","decision":"resolved","resolution":"采用高等级证据","evidence_refs":["evidence:arbitration"]}),
    ("research.interrupt.required", {"graph_run_id":"research-run.fixture","interrupt_id":"interrupt.fixture","interrupt_kind":"arbitration","interrupt_digest":"digest-fixture","status":"needs_human"}),
    ("research.merge.completed", {"graph_run_id":"research-run.fixture","artifact_id":"artifact:report","claim_count":4,"evidence_refs":["evidence:report"]}),
    ("research.global_review.decided", {"graph_run_id":"research-run.fixture","decision":"approved","evidence_refs":["evidence:global"]}),
])
store = EventStore(db)
for index, (kind, payload) in enumerate(events): store.append(DomainEvent.new(kind, "fixture", payload, run_id="ui-session-0"), command_id=f"research-fixture-{index}")
`;
  const result = spawnSync(python, ["-c", script, runtimeDir], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || "research fixture setup failed");
}

test("approves a plan then shows parallel review and arbitration", async ({}, testInfo) => {
  const runtimeDir = testInfo.outputPath("runtime");
  installResearchFixtures(runtimeDir);
  const app = await electron.launch({ args: [path.resolve(".")], env: { ...process.env, HERMES_PYTHON: python, HERMES_RUNTIME_DIR: runtimeDir } });
  try {
    const page = await app.firstWindow();
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    await page.getByRole("button", { name: "生成研究计划" }).click();
    const plan = page.getByRole("region", { name: "执行计划" });
    await expect(plan).toContainText("4 个并行 Worker");
    await expect(plan).toContainText("待批准临时 Agent");
    await page.getByRole("button", { name: "批准并执行" }).click();
    await expect(plan).toContainText("已批准");
    const graph = page.getByRole("region", { name: "研究图运行" });
    await expect(graph.getByText(/局部审核未通过/)).toBeVisible();
    await expect(graph.getByText("Attempt 9", { exact: false })).toHaveCount(0);
    await expect(graph.getByText("冲突仲裁")).toBeVisible();
    await expect(graph.getByText("全局审核通过")).toBeVisible();
    await expect(graph.getByRole("button", { name: "批准仲裁并继续" })).toHaveCount(0);
    await page.evaluate(() => localStorage.setItem("hermes.v4.conversation-timelines", JSON.stringify({
      "ui-session-0": [{ id: "persisted", kind: "user", title: "你", content: "已保存会话", status: "刚刚" }],
    })));
    await page.reload();
    await expect(page.getByRole("region", { name: "研究图运行" })).toContainText("全局审核通过");
  } finally {
    await app.close();
  }
});
