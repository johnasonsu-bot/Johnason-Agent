import type { ResearchGraphState } from "./graphReducer";

const labels: Record<string, string> = { research: "研究", compare: "比较", fact_check: "事实核验", gap_analysis: "缺口分析" };

export function GraphRun({ state }: { state: ResearchGraphState }) {
  if (!state.graphRunId) return null;
  const records = Object.values(state.records);
  return <section className="research-run" aria-label="研究图运行">
    <header><div><small>Research Graph</small><h3>并行研究执行</h3></div><code>{state.graphRunId}</code></header>
    <div className="research-lanes">{Object.keys(labels).map((branch) => <article key={branch}><strong>{labels[branch]}</strong>{records.filter((item) => item.branchId === branch).sort((a,b) => a.attempt-b.attempt).map((item) => <p key={`${item.attempt}:${item.stage}`}><b>Attempt {item.attempt}</b> · {item.stage}{item.decision === "rejected" ? " · 局部审核未通过" : item.decision === "approved" ? " · 局部审核通过" : ""}</p>)}</article>)}</div>
    <div className="research-reduce-flow">
      {state.supervisor && <article><strong>整体 Supervisor</strong><p>{String(state.supervisor.decision ?? "已完成")}</p></article>}
      {state.arbitration && <article><strong>冲突仲裁</strong><p>{String(state.arbitration.decision ?? "resolved")}</p></article>}
      {state.merge && <article><strong>Merge · 证据合成</strong><p>{String(state.merge.claim_count ?? "已生成报告")}</p></article>}
      {state.globalReview && <article><strong>全局审核通过</strong><p>{String(state.globalReview.decision ?? "approved")}</p></article>}
    </div>
  </section>;
}
