import { expect, test } from "@playwright/test";
import { chmod, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { launchTestElectron } from "./support/electron-launch";

type ArtifactLink = {
  link_id: string;
  session_id: string;
  run_id: string;
  term_id: string;
  step_id: string;
  command_id: string;
  agent_id: string;
  attempt: number;
  artifact_id: string;
  filename: string;
  media_type: string;
};

type ArtifactFixture = {
  holdSession0?: boolean;
  holdArtifactId?: string;
  failArtifacts?: boolean;
  events?: Record<string, Array<Record<string, unknown> & { cursor: string; eventId: string }>>;
  links: Record<string, ArtifactLink[]>;
  contents: Record<string, string>;
};

const artifactId = (character: string) => `sha256:${character.repeat(64)}`;
const publicationId = (character: string) => character.repeat(64);

function link(
  sessionId: string,
  linkId: string,
  id: string,
  filename: string,
  mediaType: string,
  attempt: number,
): ArtifactLink {
  return {
    link_id: linkId,
    session_id: sessionId,
    run_id: `run-${sessionId}`,
    term_id: "term-1",
    step_id: "step-1",
    command_id: "command-1",
    agent_id: "agent-1",
    attempt,
    artifact_id: id,
    filename,
    media_type: mediaType,
  };
}

async function launchArtifactFixture(root: string, initial: ArtifactFixture) {
  await mkdir(root, { recursive: true });
  const executable = path.join(root, "artifact-backend.mjs");
  const stateFile = path.join(root, "state.json");
  const logFile = path.join(root, "requests.jsonl");
  await writeFile(stateFile, JSON.stringify(initial));
  await writeFile(executable, `#!/usr/bin/env node
import http from 'node:http'; import fs from 'node:fs';
let input=''; let started=false;
process.stdin.on('data',chunk=>{input+=chunk;if(started||!input.includes('\\n'))return;started=true;const identity=JSON.parse(input.split('\\n')[0]);
const server=http.createServer((req,res)=>{let raw='';req.on('data',c=>raw+=c);req.on('end',async()=>{
 const url=new URL(req.url,'http://localhost'); const p=url.pathname;
 fs.appendFileSync(${JSON.stringify(logFile)},JSON.stringify({path:p,method:req.method,search:url.search})+'\\n');
 const state=()=>JSON.parse(fs.readFileSync(${JSON.stringify(stateFile)},'utf8'));
 const send=(status,body,type='application/json',headers={})=>{res.writeHead(status,{'content-type':type,...headers});res.end(typeof body==='string'?body:JSON.stringify(body));};
 if(p==='/api/health')return send(200,{status:'ok',service:'hermes-workbench',instance_id:identity.instance_id,port:server.address().port});
 if(p==='/api/vault/status')return send(200,{status:'unlocked'});
 if(p==='/api/providers'||p==='/api/agents')return send(200,[]);
 if(p==='/api/v1/engine-host')return send(200,{v2:{enabled:false,protocol:'2.0',runtimes:[]}});
 if(p==='/api/sessions')return send(200,{session_id:raw?JSON.parse(raw).session_id:'unknown'});
 if(p.endsWith('/events')){
   const session=p.split('/')[3]; const events=state().events?.[session]??[]; const after=req.headers['last-event-id'];
   const index=events.findIndex(event=>event.cursor===after); const page=events.slice(after&&index>=0?index+1:0);
   return send(200,page.map(event=>'id: '+event.cursor+'\\ndata: '+JSON.stringify(event)+'\\n\\n').join(''),'text/event-stream');
 }
 if(p==='/api/artifacts'){
   const session=url.searchParams.get('session_id');
   if(session==='ui-session-0'&&state().holdSession0){await new Promise(resolve=>{const timer=setInterval(()=>{if(!state().holdSession0){clearInterval(timer);resolve();}},10);});}
   const current=state(); if(current.failArtifacts)return send(503,{detail:'artifact catalog unavailable'});
   return send(200,current.links[session]??[]);
 }
 if(p.startsWith('/api/artifacts/')){
   const parts=p.split('/'); const id=decodeURIComponent(parts[3]);
   if(state().holdArtifactId===id){await new Promise(resolve=>{const timer=setInterval(()=>{if(state().holdArtifactId!==id){clearInterval(timer);resolve();}},10);});}
   const current=state();
   const publication=Object.values(current.links).flat().find(item=>item.artifact_id===id);
   if(!publication)return send(404,{detail:'Artifact not found'});
   if(parts[4]==='download')return send(200,current.contents[id],publication.media_type,{'content-disposition':'attachment; filename="'+publication.filename+'"'});
   return send(200,{artifact_id:id,media_type:publication.media_type,content:current.contents[id],digest:id.slice(7)});
 }
 return send(404,{detail:'not found'});
});});server.listen(0,'127.0.0.1',()=>console.log(JSON.stringify({service:'hermes-workbench',instance_id:identity.instance_id,port:server.address().port})));});
`);
  await chmod(executable, 0o755);
  const app = await launchTestElectron({
    args: [path.resolve(".")],
    env: { HERMES_PYTHON: executable, HERMES_RUNTIME_DIR: path.join(root, "runtime") },
    isolationDirectory: root,
  });
  const page = await app.firstWindow();
  return {
    app,
    page,
    state: async (value: ArtifactFixture) => {
      const next = `${stateFile}.next`;
      await writeFile(next, JSON.stringify(value));
      await rename(next, stateFile);
    },
    requests: async () => (await readFile(logFile, "utf8")).trim().split("\n").filter(Boolean).map(line => JSON.parse(line)),
  };
}

test("late artifact response from the previous session cannot pollute the selected session", async ({}, info) => {
  const first = artifactId("a");
  const second = artifactId("b");
  const initial: ArtifactFixture = {
    holdSession0: true,
    links: {
      "ui-session-0": [link("ui-session-0", publicationId("1"), first, "alpha.md", "text/markdown", 1)],
      "ui-session-1": [link("ui-session-1", publicationId("2"), second, "beta.txt", "text/plain", 1)],
    },
    contents: { [first]: "alpha session", [second]: "beta session" },
  };
  const fixture = await launchArtifactFixture(info.outputPath("backend"), initial);
  try {
    await fixture.page.getByText("方案评审 · Architecture review", { exact: true }).click();
    await expect(fixture.page.getByRole("button", { name: /beta\.txt · Attempt 1/ })).toBeVisible();
    await fixture.state({ ...initial, holdSession0: false });
    await expect.poll(async () => (await fixture.requests()).filter(request => request.path === "/api/artifacts").length).toBeGreaterThanOrEqual(2);
    await expect(fixture.page.getByText("beta session", { exact: true })).toBeVisible();
    await expect(fixture.page.getByText("alpha.md", { exact: false })).toHaveCount(0);
  } finally {
    await fixture.app.close();
  }
});

test("selects published attempts and downloads the selected original filename", async ({}, info) => {
  const markdown = artifactId("c");
  const html = artifactId("d");
  const laterHtml = artifactId("f");
  const initial: ArtifactFixture = {
    links: {
      "ui-session-0": [
        link("ui-session-0", publicationId("3"), markdown, "report-v1.md", "text/markdown", 1),
        link("ui-session-0", publicationId("4"), html, "report-v2.html", "text/html", 2),
        link("ui-session-0", publicationId("6"), laterHtml, "report-v3.html", "text/html", 3),
      ],
    },
    contents: {
      [markdown]: "first published report",
      [html]: "<p>second published report</p>",
      [laterHtml]: "<p>third published report</p>",
    },
  };
  const fixture = await launchArtifactFixture(info.outputPath("backend"), initial);
  try {
    await expect(fixture.page.getByRole("heading", { name: "Runtime 发布产物" })).toBeVisible();
    await fixture.page.getByRole("button", { name: /report-v2\.html · Attempt 2/ }).click();
    const htmlPreview = fixture.page.locator('iframe[title="report-v2.html"]');
    await expect(htmlPreview).toBeVisible();
    await expect(htmlPreview.contentFrame().locator("body")).toContainText("second published report");

    await fixture.state({ ...initial, holdArtifactId: laterHtml });
    await fixture.page.getByRole("button", { name: /report-v3\.html · Attempt 3/ }).click();
    await expect(fixture.page.locator('iframe[title="report-v3.html"]')).toHaveCount(0);
    await expect(fixture.page.locator('iframe[title="report-v2.html"]')).toHaveCount(0);
    await fixture.state(initial);
    await expect(fixture.page.locator('iframe[title="report-v3.html"]').contentFrame().locator("body")).toContainText("third published report");

    await fixture.page.getByRole("button", { name: /report-v1\.md · Attempt 1/ }).click();
    await expect(fixture.page.getByText("first published report", { exact: true })).toBeVisible();
    const download = fixture.page.getByRole("link", { name: "下载 report-v1.md" });
    await expect(download).toHaveAttribute("download", "report-v1.md");
    await expect(download).toHaveAttribute("href", /^data:text\/markdown;charset=utf-8,/);
    await download.click();
  } finally {
    await fixture.app.close();
  }
});

test("manual refresh failure is explicit and never masquerades as fixture success", async ({}, info) => {
  const published = artifactId("e");
  const initial: ArtifactFixture = {
    links: {
      "ui-session-0": [link("ui-session-0", publicationId("5"), published, "live.txt", "text/plain", 1)],
    },
    contents: { [published]: "real runtime artifact" },
  };
  const fixture = await launchArtifactFixture(info.outputPath("backend"), initial);
  try {
    await expect(fixture.page.getByText("real runtime artifact", { exact: true })).toBeVisible();
    await fixture.state({ ...initial, failArtifacts: true });
    await fixture.page.getByRole("button", { name: "刷新 Runtime 产物" }).click();
    await expect(fixture.page.getByRole("alert")).toContainText("Runtime 产物暂不可用");
    await expect(fixture.page.getByRole("button", { name: "Run graph" })).toHaveCount(0);
    await expect(fixture.page.getByText("示例产物 · Fixture", { exact: true })).toHaveCount(0);
  } finally {
    await fixture.app.close();
  }
});

test("a new completion event refreshes the current session artifact publications", async ({}, info) => {
  const published = artifactId("9");
  const initial: ArtifactFixture = { links: { "ui-session-0": [] }, contents: {} };
  const fixture = await launchArtifactFixture(info.outputPath("backend"), initial);
  try {
    await expect(fixture.page.getByText("示例产物 · Fixture", { exact: true })).toBeVisible();
    await fixture.state({
      links: {
        "ui-session-0": [link("ui-session-0", publicationId("9"), published, "event-result.txt", "text/plain", 1)],
      },
      contents: { [published]: "published after completion" },
      events: {
        "ui-session-0": [{ cursor: "1", eventId: "finished-1", name: "turn_finished", value: { command_id: "command-1", status: "completed" } }],
      },
    });
    await expect(fixture.page.getByText("published after completion", { exact: true })).toBeVisible();
    await expect(fixture.page.getByText("示例产物 · Fixture", { exact: true })).toHaveCount(0);
  } finally {
    await fixture.app.close();
  }
});

test("streaming deltas do not refetch or clear artifacts before a terminal event", async ({}, info) => {
  const published = artifactId("8");
  const initial: ArtifactFixture = {
    links: {
      "ui-session-0": [link("ui-session-0", publicationId("8"), published, "stable.txt", "text/plain", 1)],
    },
    contents: { [published]: "stable artifact while streaming" },
  };
  const fixture = await launchArtifactFixture(info.outputPath("backend"), initial);
  try {
    await expect(fixture.page.getByText("stable artifact while streaming", { exact: true })).toBeVisible();
    await expect.poll(async () => (await fixture.requests()).filter(request => request.path === "/api/artifacts").length).toBe(1);

    await fixture.state({
      ...initial,
      events: {
        "ui-session-0": [
          { cursor: "delta-1", eventId: "delta-1", type: "TEXT_MESSAGE_CONTENT", delta: "first streaming delta" },
          { cursor: "delta-2", eventId: "delta-2", type: "TEXT_MESSAGE_CONTENT", delta: "second streaming delta" },
        ],
      },
    });
    await expect(fixture.page.getByText("first streaming delta", { exact: true })).toBeVisible();
    await expect(fixture.page.getByText("second streaming delta", { exact: true })).toBeVisible();
    await expect(fixture.page.getByText("stable artifact while streaming", { exact: true })).toBeVisible();
    expect((await fixture.requests()).filter(request => request.path === "/api/artifacts")).toHaveLength(1);

    const terminalState: ArtifactFixture = {
      ...initial,
      holdSession0: true,
      events: {
        "ui-session-0": [
          { cursor: "delta-1", eventId: "delta-1", type: "TEXT_MESSAGE_CONTENT", delta: "first streaming delta" },
          { cursor: "delta-2", eventId: "delta-2", type: "TEXT_MESSAGE_CONTENT", delta: "second streaming delta" },
          { cursor: "done", eventId: "done", name: "turn_finished", value: { command_id: "command-1", status: "completed" } },
        ],
      },
    };
    await fixture.state(terminalState);
    await expect.poll(async () => (await fixture.requests()).filter(request => request.path === "/api/artifacts").length).toBe(2);
    await expect(fixture.page.getByText("stable artifact while streaming", { exact: true })).toBeVisible();
    await fixture.state({ ...terminalState, holdSession0: false });
  } finally {
    await fixture.app.close();
  }
});
