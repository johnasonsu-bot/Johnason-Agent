import { useState } from "react";
import { conversationApi } from "../api";
import type { DevelopmentBranchRecord, ResearchGraphState } from "./graphReducer";

const labels: Record<string, string> = { research: "研究", compare: "比较", fact_check: "事实核验", gap_analysis: "缺口分析" };

export function GraphRun({ state, onResume, sessionId }: { state: ResearchGraphState; onResume(preference?: string): void; sessionId: string }) {
  const [preference, setPreference] = useState("");
  const [releasePending, setReleasePending] = useState(false);
  const development = state.development;
  if (!state.graphRunId && !development?.graphRunId) return null;
  const records = Object.values(state.records);
  const shortSha = (sha: string) => sha ? sha.slice(0, 12) : "—";
  const testStatus = (record: DevelopmentBranchRecord) => record.testResult === "passed" ? "通过" : record.testResult === "failed" ? "未通过" : "待验证";
  const approveRelease = () => {
    if (!development?.graphRunId || !development.interruptId || development.interruptKind !== "release_approval") return;
    setReleasePending(true);
    void conversationApi.resumeDevelopmentInterrupt(sessionId, development.graphRunId, development.interruptId, `development-release-${sessionId}-${Date.now()}`).catch(() => setReleasePending(false));
  };
  return <>
    {state.graphRunId && <section className="research-run" aria-label="研究图运行">
    <header><div><small>Research Graph</small><h3>并行研究执行</h3></div><code>{state.graphRunId}</code></header>
    <div className="research-lanes">{Object.keys(labels).map((branch) => <article key={branch}><strong>{labels[branch]}</strong>{records.filter((item) => item.branchId === branch).sort((a,b) => a.attempt-b.attempt).map((item) => <p key={`${item.attempt}:${item.stage}`}><b>Attempt {item.attempt}</b> · {item.stage}{item.decision === "rejected" ? " · 局部审核未通过" : item.decision === "approved" ? " · 局部审核通过" : ""}</p>)}</article>)}</div>
    <div className="research-reduce-flow">
      {state.supervisor && <article><strong>整体 Supervisor</strong><p>{String(state.supervisor.decision ?? "已完成")}</p></article>}
      {state.arbitration && <article><strong>冲突仲裁</strong><p>{String(state.arbitration.decision ?? "resolved")}</p>{state.interruptKind === "arbitration" && state.interruptId && state.arbitration.decision === "requires_preference" && <><label>仲裁偏好<input aria-label="仲裁偏好" value={preference} onChange={(event) => setPreference(event.target.value)} /></label><button type="button" disabled={!preference.trim()} onClick={() => onResume(preference.trim())}>提交偏好并继续</button></>}{state.interruptKind === "arbitration" && state.arbitration.decision === "insufficient_evidence" && <p>证据不足，必须重新规划并补充证据。</p>}</article>}
      {state.interruptKind === "branch_review" && state.interruptId && <article><strong>分支审核等待人工决定</strong><button type="button" onClick={() => onResume()}>批准分支审核并继续</button></article>}
      {state.interruptKind === "replan" && <article><strong>需要重新规划</strong><p>请在执行计划中创建并批准新版本。</p></article>}
      {state.merge && <article><strong>Merge · 证据合成</strong><p>{String(state.merge.claim_count ?? "已生成报告")}</p></article>}
      {state.globalReview && <article><strong>全局审核通过</strong><p>{String(state.globalReview.decision ?? "approved")}</p></article>}
    </div>
    </section>}
    {development?.graphRunId && <section className="development-run" aria-label="开发图运行">
      <header><div><small>Development Graph</small><h3>隔离开发证据</h3></div><code>{development.graphRunId}</code></header>
      <div className="development-lanes">{Object.values(development.branches).map((record) => <article key={`${record.branchId}:${record.attempt}`}><strong>{record.branchId} · 独立 Worktree</strong><p>Worktree · {record.worktreeName || "—"}</p><p>分支 · {record.workerBranch || "—"}</p><p>Base · <code>{shortSha(record.baseSha)}</code>　Commit · <code>{shortSha(record.commitSha)}</code></p><p>负责文件 · {record.ownedPathSummary.length} 项</p><p>{record.testLabel || "声明测试"}{testStatus(record)}</p>{record.review && <p>{record.review === "approved" ? "局部审核通过" : record.review === "rejected" ? "局部审核未通过" : "等待人工局部审核"}</p>}{record.findings.length > 0 && <p>发现 · {record.findings.join("；")}</p>}</article>)}</div>
      <div className="development-reduce-flow">
        {development.merge && <article><strong>临时集成分支</strong><p>{String(development.merge.status) === "conflict" ? "合并冲突，需仲裁" : "合并证据已记录"}</p><p>{String(development.merge.integration_branch ?? "—")}</p><p>Integration · <code>{shortSha(String(development.merge.integration_sha ?? ""))}</code></p>{Array.isArray(development.merge.conflict_paths) && development.merge.conflict_paths.length > 0 && <p>冲突文件 · {(development.merge.conflict_paths as string[]).join("、")}</p>}</article>}
        {development.regression && <article><strong>全局验证</strong><p>{String(development.regression.test_label ?? "临时集成分支测试")}{String(development.regression.test_result) === "passed" ? "通过" : "未通过"}</p><p>Global Verifier · {String(development.regression.global_verifier ?? development.regression.decision ?? "待验证") === "approved" ? "通过" : "待处理"}</p></article>}
        {development.interruptKind === "release_approval" && development.interruptId && <article><strong>等待发布审批</strong><p>不会自动合并到目标分支。</p><button type="button" disabled={releasePending} onClick={approveRelease}>{releasePending ? "审批已提交" : "批准进入目标分支"}</button></article>}
      </div>
    </section>}
  </>;
}
