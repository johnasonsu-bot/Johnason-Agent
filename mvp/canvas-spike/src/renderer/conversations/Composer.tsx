import { FormEvent, useState } from "react";

export function Composer({ onSend, pending = false }: { onSend: (prompt: string) => void; pending?: boolean }) {
  const [prompt, setPrompt] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const value = prompt.trim();
    if (!value) return;
    onSend(value);
    setPrompt("");
  };

  return <form className="conversation-composer" onSubmit={submit} aria-label="人工介入 composer" style={{ borderTop: "1px solid #e6e5e2", padding: "14px 18px", background: "#fff" }}>
    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
      <textarea aria-label="输入消息或介入要求" placeholder="输入消息或介入要求" value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={2} style={{ flex: 1, resize: "vertical", border: "1px solid #d9dce1", borderRadius: 10, padding: 10, font: "inherit" }} />
      <button type="submit" disabled={pending}>{pending ? "发送中…" : "发送"}</button>
    </div>
    <small style={{ color: "#6b7280" }}>Human intervention · 补充、纠正或约束当前执行</small>
  </form>;
}
