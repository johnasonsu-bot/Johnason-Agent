import { _electron as electron, expect, test } from "@playwright/test";
import { chmod, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

// An Electron-owned HTTP boundary fixture, not a Runtime/model live-GO test.
async function launch(root: string, initialState: Record<string, unknown> = {}) {
  await mkdir(root, { recursive: true });
  const executable = path.join(root, "backend.mjs");
  const stateFile = path.join(root, "state.json");
  const logFile = path.join(root, "requests.jsonl");
  await writeFile(stateFile, JSON.stringify({ events: [], reject: false, ...initialState }));
  await writeFile(executable, `#!/usr/bin/env node
import http from 'node:http'; import fs from 'node:fs';
let input = ''; let started = false; let messageSequence = 0;
let profiles = [
 {id:'lmstudio',name:'Local',protocol:'lmstudio',credential_mode:'none'},
 {id:'custom-cloud',name:'Custom cloud',protocol:'deepseek',credential_mode:'reference'},
 {id:'deepseek',name:'Misleading ID',protocol:'anthropic',credential_mode:'reference'},
 {id:'legacy',name:'Legacy',protocol:'openai'},
 {id:'standalone',name:'Standalone',protocol:'openai_compatible',credential_mode:'none'}
].map(p=>({...p,base_url:'http://127.0.0.1:1234',headers:{},model_aliases:{default:p.id==='custom-cloud'?'deepseek-chat':'local-agent'},capabilities:['streaming'],enabled:true,thinking_enabled:false,reasoning_effort:'high',credential_status:'not_required'}));
process.stdin.on('data',chunk=>{input+=chunk;if(started||!input.includes('\\n'))return;started=true;const identity=JSON.parse(input.split('\\n')[0]);
const server=http.createServer((req,res)=>{let raw='';req.on('data',c=>raw+=c);req.on('end',()=>{
 const body=raw?JSON.parse(raw):null;const url=new URL(req.url,'http://localhost');const p=url.pathname;
 const state=JSON.parse(fs.readFileSync(${JSON.stringify(stateFile)},'utf8'));
 fs.appendFileSync(${JSON.stringify(logFile)},JSON.stringify({path:p,method:req.method,body,lastEventId:req.headers['last-event-id']??null})+'\\n');
 const send=(status,body,type='application/json')=>{res.writeHead(status,{'content-type':type});res.end(typeof body==='string'?body:JSON.stringify(body));};
 if(p==='/api/health')return send(200,{status:'ok',service:'hermes-workbench',instance_id:identity.instance_id,port:server.address().port});
 if(p==='/api/vault/status')return send(200,{status:'unlocked'});
 if(p==='/api/vault/lock')return send(200,{status:'locked'});
 if(p==='/api/providers') {if(req.method==='POST'){profiles=profiles.map(v=>v.id===body.id?{...v,...body}:v);if(!profiles.some(v=>v.id===body.id))profiles.push({...body,headers:{},credential_status:'not_required'});return send(201,profiles.find(v=>v.id===body.id));}return send(200,profiles);}
 if(p.endsWith('/models'))return send(200,{status:'online',models:['local-agent','discovered-model'],error_code:null});
 if(p==='/api/agents') {
   const reply=()=>{send(200,[{agent_id:'cloud',display_name:'Cloud',role:'worker',provider_id:'custom-cloud',model:'stale-model',enabled:true,tool_ids:[],skill_refs:[],version:1,created_at:1},{agent_id:'bad',display_name:'Bad',role:'worker',provider_id:'deepseek',model:'bad-model',enabled:true,tool_ids:[],skill_refs:[],version:1,created_at:1}]);fs.appendFileSync(${JSON.stringify(logFile)},JSON.stringify({path:'fixture-agents'})+'\\n');};
   if(state.holdAgents){const timer=setInterval(()=>{const current=JSON.parse(fs.readFileSync(${JSON.stringify(stateFile)},'utf8'));if(!current.holdAgents){clearInterval(timer);reply();}},20);return;}
   return reply();
 }
 if(p==='/api/v1/engine-host')return send(200,{v2:{enabled:true,protocol:'2.0',runtimes:['python-term','goose','dsh','unknown-runtime'].map(selector=>({runtime_id:selector,selector,build_id:'test',state:'ready',capabilities:[],selectable_for_new_commands:!state.reject,admission_state:state.reject?'blocked':'ready',trust_status:'DEV_UNTRUSTED',admission_reason:state.reject?'proof_revoked':null}))}});
 if(p.includes('/runtime-admissions/'))return send(200,{state:'ready',selector:'goose'});
 if(p.endsWith('/messages')) {
   const commandId = 'command-' + (++messageSequence);
   const reply = current => {send(current.reject?503:200,current.reject?{detail:'private internal exception /secret/path'}:{session_id:p.split('/')[3],command_id:commandId,status:current.postStatus??'queued',cursor:'ack'});fs.appendFileSync(${JSON.stringify(logFile)},JSON.stringify({path:'fixture-ack',commandId})+'\\n');};
   if(state.holdMessages) {const timer=setInterval(()=>{const current=JSON.parse(fs.readFileSync(${JSON.stringify(stateFile)},'utf8'));if(!current.holdMessages){clearInterval(timer);reply(current);}},20);return;}
   return reply(state);
 }
 if(p.endsWith('/events')) {const events=p.includes('ui-session-0')?state.events:[];const after=req.headers['last-event-id'];const i=events.findIndex(e=>e.cursor===after);return send(200,events.slice(after&&i>=0?i+1:0).map(e=>'id: '+e.cursor+'\\ndata: '+JSON.stringify(e)+'\\n\\n').join(''),'text/event-stream');}
 if(p==='/api/sessions')return send(200,{session_id:body.session_id});
 return send(404,{detail:'not found'});
});});server.listen(0,'127.0.0.1',()=>console.log(JSON.stringify({service:'hermes-workbench',instance_id:identity.instance_id,port:server.address().port})));});
`);
  await chmod(executable, 0o755);
  const app = await electron.launch({ args: [path.resolve(".")], env: { ...process.env, HERMES_PYTHON: executable, HERMES_RUNTIME_DIR: path.join(root, "runtime") } });
  const page = await app.firstWindow();
  return { app, page, state: async (value: object) => {
    // Publish atomically: the owned backend polls this file concurrently.
    const next = `${stateFile}.next`;
    await writeFile(next, JSON.stringify(value));
    await rename(next, stateFile);
  }, requests: async () => (await readFile(logFile, "utf8")).trim().split("\n").map(line => JSON.parse(line)) };
}

test("federated runtime four fixed modes use actual protocol and freeze exact bindings", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    const runtime = f.page.getByLabel("当前运行模式");
    await expect(runtime.locator("option")).toHaveCount(4);
    await expect(runtime).toContainText("聊天模式");
    await expect(runtime).toContainText("Agent-步进执行模式（Codex Harness）");
    await expect(runtime).toContainText("Agent-寻路模式（Claude Harness）");
    await expect(runtime).toContainText("Agent-事件驱动模式（DeepSeek Harness）");
    await runtime.selectOption("dsh");
    const model = f.page.getByLabel("当前模型");
    await expect(model).toHaveValue("custom-cloud/deepseek-chat");
    await expect(model.locator('option[value="deepseek/bad-model"]')).toHaveCount(0);
    await f.page.getByLabel("会话消息").fill("精确绑定");
    await f.page.getByLabel("发送", { exact: true }).click();
    await expect.poll(async () => (await f.requests()).filter(r => r.path.endsWith('/messages')).length).toBe(1);
    expect((await f.requests()).find(r => r.path.endsWith('/messages')).body).toEqual({ content: "精确绑定", runtime: "dsh", provider_id: "custom-cloud", model: "deepseek-chat" });
    await expect(runtime).toBeDisabled();
    await expect(model).toBeDisabled();
    await f.page.getByText("方案评审 · Architecture review", { exact: true }).click();
    await expect(runtime).toBeEnabled();
    await f.page.getByText("Jira 看板配置修复指引", { exact: true }).first().click();
    await expect(runtime).toBeDisabled();
    await expect(model).toHaveValue("custom-cloud/deepseek-chat");
  } finally { await f.app.close(); }
});

test("federated runtime replays server timeline without browser cache and refreshes failure diagnostics", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    const events = [
      { cursor: "1", eventId: "q", name: "turn_queued", value: { command_id: "command-1" } },
      { cursor: "2", eventId: "a", name: "runtime.status.changed", value: { status: "queued" } },
      { cursor: "3", eventId: "g", name: "runtime.status.changed", value: { status: "running" } },
      { cursor: "4", eventId: "s", type: "TEXT_MESSAGE_CONTENT", delta: "服务端流式证据" },
      { cursor: "5", eventId: "f", name: "turn_failed", value: { command_id: "command-1", reason: "runtime_cancelled" } },
    ];
    await f.state({ events, reject: false });
    await expect(f.page.getByText("服务端流式证据", { exact: true })).toBeVisible();
    await f.page.evaluate(() => { localStorage.removeItem("hermes.v4.conversation-timelines"); localStorage.setItem("hermes.v4.conversation-cursors", JSON.stringify({ "ui-session-0": "5" })); });
    await f.page.reload();
    await expect(f.page.getByText("服务端流式证据", { exact: true })).toHaveCount(1);
    await expect(f.page.getByTestId("conversation-status")).toContainText("已取消");
    await expect(f.page.getByText(/Runtime 状态/).first()).toBeVisible();
    await expect(f.page.getByText("准入已确认 · Admission ready")).toBeVisible();
    const runtime = f.page.getByLabel("当前运行模式");
    await runtime.selectOption("goose");
    await f.state({ events, reject: true });
    await f.page.getByLabel("会话消息").fill("拒绝执行");
    await f.page.getByLabel("发送", { exact: true }).click();
    await expect(f.page.getByTestId("conversation-status")).toContainText("runtime_unavailable");
    await expect(runtime.locator('option[value="goose"]')).toBeDisabled();
    await expect(f.page.locator("body")).not.toContainText("private internal exception");
  } finally { await f.app.close(); }
});

test("federated runtime provider save and model discovery preserve credential modes", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    await f.page.getByRole("link", { name: "模型供应商", exact: true }).click();
    await f.page.getByRole("button", { name: "保存供应商", exact: true }).click();
    await expect.poll(async () => (await f.requests()).filter(r => r.path === '/api/providers' && r.method === 'POST').length).toBe(1);
    expect((await f.requests()).filter(r => r.path === '/api/providers' && r.method === 'POST').at(-1).body.credential_mode).toBe("none");
    await f.page.getByRole("button", { name: "发现模型", exact: true }).click();
    await f.page.getByLabel("默认模型").selectOption("discovered-model");
    await expect.poll(async () => (await f.requests()).filter(r => r.path === '/api/providers' && r.method === 'POST').length).toBe(2);
    expect((await f.requests()).filter(r => r.path === '/api/providers' && r.method === 'POST').at(-1).body.credential_mode).toBe("none");
    await f.page.getByRole("button", { name: /Legacy/ }).click();
    await f.page.getByRole("button", { name: "保存供应商", exact: true }).click();
    await expect.poll(async () => (await f.requests()).filter(r => r.path === '/api/providers' && r.method === 'POST').length).toBe(3);
    expect((await f.requests()).filter(r => r.path === '/api/providers' && r.method === 'POST').at(-1).body.credential_mode).toBe("reference");
    await f.page.getByRole("button", { name: "使用 LM Studio", exact: true }).click();
    await f.page.getByRole("button", { name: "保存供应商", exact: true }).click();
    await expect.poll(async () => (await f.requests()).filter(r => r.path === '/api/providers' && r.method === 'POST').length).toBe(4);
    expect((await f.requests()).filter(r => r.path === '/api/providers' && r.method === 'POST').at(-1).body.credential_mode).toBe("none");
  } finally { await f.app.close(); }
});

test("federated runtime Goose selects configured provider models without an agent record", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    await f.page.getByLabel("当前运行模式").selectOption("goose");
    await expect(f.page.getByLabel("当前模型").locator('option[value="standalone/local-agent"]')).toHaveCount(1);
    await f.page.getByLabel("当前模型").selectOption("standalone/local-agent");
    await f.page.getByLabel("会话消息").fill("Goose standalone");
    await f.page.getByLabel("发送", { exact: true }).click();
    await expect.poll(async () => (await f.requests()).filter(r => r.path.endsWith('/messages')).length).toBe(1);
    expect((await f.requests()).find(r => r.path.endsWith('/messages')).body).toEqual({ content: "Goose standalone", runtime: "goose", provider_id: "standalone", model: "local-agent" });
    await f.state({ events: [{cursor:"1",eventId:"q",name:"turn_queued",value:{command_id:"command-1"}},{cursor:"2",eventId:"f",name:"runtime.status.changed",value:{status:"completed"}}] });
    await expect(f.page.getByLabel("当前运行模式")).toBeEnabled();
    await f.page.getByLabel("当前运行模式").selectOption("");
    await f.page.getByLabel("会话消息").fill("chat standalone");
    await f.page.getByLabel("发送", { exact: true }).click();
    await expect.poll(async () => (await f.requests()).filter(r => r.path.endsWith('/messages')).length).toBe(2);
    expect((await f.requests()).filter(r => r.path.endsWith('/messages'))[1].body).toEqual({ content: "chat standalone", provider_id: "standalone", model: "local-agent" });
  } finally { await f.app.close(); }
});

test("federated runtime pending response cursor does not skip queued evidence or keep terminal locked", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    await f.page.getByLabel("当前运行模式").selectOption("goose");
    await f.page.getByLabel("会话消息").fill("cursor ACK");
    await f.page.getByLabel("发送", { exact: true }).click();
    await expect.poll(async () => (await f.requests()).filter(r => r.path.endsWith('/messages')).length).toBe(1);
    await f.state({ events: [{cursor:"ack",eventId:"q",name:"turn_queued",value:{command_id:"command-1"}},{cursor:"done",eventId:"f",name:"runtime.status.changed",value:{status:"completed"}}] });
    await expect(f.page.getByText("已排队 · Turn queued")).toBeVisible();
    await expect(f.page.getByLabel("当前运行模式")).toBeEnabled();
    await expect(f.page.getByLabel("当前模型")).toBeEnabled();
  } finally { await f.app.close(); }
});

test("federated runtime pause is not a terminal and keeps all selection frozen", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    await f.page.getByLabel("当前运行模式").selectOption("goose");
    await f.page.getByLabel("会话消息").fill("pause pending");
    await f.page.getByLabel("发送", { exact: true }).click();
    await expect.poll(async () => (await f.requests()).filter(r => r.path.endsWith('/messages')).length).toBe(1);
    await f.state({ events: [{cursor:"ack",eventId:"q",name:"turn_queued",value:{command_id:"command-1"}},{cursor:"pause",eventId:"p",name:"runtime.status.changed",value:{status:"paused"}}] });
    await expect(f.page.getByTestId("conversation-status")).toContainText("已暂停");
    await expect(f.page.getByLabel("当前运行模式")).toBeDisabled();
    await expect(f.page.getByLabel("当前模型")).toBeDisabled();
  } finally { await f.app.close(); }
});

test("federated review delayed ACK cannot release a new command through old terminal replay", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  const history = [{cursor:"old-q",eventId:"old-q",name:"turn_queued",value:{command_id:"old-command"}},{cursor:"old-f",eventId:"old-f",name:"turn_finished",value:{command_id:"old-command",status:"completed"}}];
  try {
    await f.state({events:history,holdMessages:true});
    await expect(f.page.getByTestId("conversation-status")).toContainText("已完成");
    await f.page.getByLabel("当前运行模式").selectOption("goose");
    await f.page.getByLabel("当前模型").selectOption("standalone/local-agent");
    await f.page.getByLabel("会话消息").fill("pending replay");
    await f.page.getByLabel("发送", {exact:true}).click();
    await expect.poll(async () => (await f.requests()).filter(r=>r.path.endsWith('/messages')).length).toBe(1);
    const roundTrip = async () => {
      await f.page.getByText("方案评审 · Architecture review", {exact:true}).click();
      await f.page.getByText("Jira 看板配置修复指引", {exact:true}).first().click();
      await expect(f.page.getByText("执行完成 · Turn finished")).toBeVisible();
      await expect(f.page.getByLabel("当前运行模式")).toBeDisabled();
      await expect(f.page.getByLabel("当前运行模式")).toHaveValue("goose");
      await expect(f.page.getByLabel("当前模型")).toHaveValue("standalone/local-agent");
      await expect(f.page.getByLabel("当前模型")).toBeDisabled();
    };
    await roundTrip();
    await f.state({events:history});
    await expect.poll(async () => (await f.requests()).filter(r=>r.path==='fixture-ack').length).toBe(1);
    await roundTrip();
    await f.state({events:[...history,{cursor:"new-q",eventId:"new-q",name:"turn_queued",value:{command_id:"command-1"}},{cursor:"new-f",eventId:"new-f",name:"turn_finished",value:{command_id:"command-1",status:"completed"}}]});
    await expect(f.page.getByLabel("当前运行模式")).toBeEnabled();
  } finally { await f.app.close(); }
});

test("federated review POST paused ACK preserves the pending selection", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    await f.state({events:[],postStatus:"paused"});
    await f.page.getByLabel("当前运行模式").selectOption("goose");
    await f.page.getByLabel("会话消息").fill("paused ACK");
    await f.page.getByLabel("发送", {exact:true}).click();
    await expect(f.page.getByTestId("conversation-status")).toContainText("已暂停");
    await expect(f.page.getByLabel("当前运行模式")).toBeDisabled();
    await expect(f.page.getByLabel("当前模型")).toBeDisabled();
  } finally { await f.app.close(); }
});

test("federated review matching running supersedes POST paused ACK without unlocking", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    await f.state({events:[],postStatus:"paused"});
    await f.page.getByLabel("当前运行模式").selectOption("goose");
    await f.page.getByLabel("会话消息").fill("resume paused ACK");
    await f.page.getByLabel("发送", {exact:true}).click();
    await expect(f.page.getByTestId("conversation-status")).toContainText("已暂停");
    await f.state({events:[{cursor:"resume-q",eventId:"resume-q",name:"turn_queued",value:{command_id:"command-1"}},{cursor:"resume-r",eventId:"resume-r",name:"runtime.status.changed",value:{status:"running"}}]});
    await expect(f.page.getByTestId("conversation-status")).toContainText("执行中");
    await expect(f.page.getByLabel("当前运行模式")).toBeDisabled();
    await expect(f.page.getByLabel("当前运行模式")).toHaveValue("goose");
    await expect(f.page.getByLabel("当前模型")).toBeDisabled();
  } finally { await f.app.close(); }
});

test("federated review late Agent loading preserves explicit draft and frozen choices", async ({}, info) => {
  const f = await launch(info.outputPath("backend"), {holdAgents:true,postStatus:"paused"});
  try {
    await f.page.getByLabel("当前运行模式").selectOption("goose");
    await f.page.getByLabel("当前模型").selectOption("standalone/local-agent");
    await f.state({events:[],postStatus:"paused"});
    await expect.poll(async () => (await f.requests()).filter(r=>r.path==='fixture-agents').length).toBe(1);
    // Arrival of a new Agent record proves the late response has reached React.
    await f.page.getByLabel("当前角色 产品经理").click();
    await expect(f.page.getByRole("menuitem", {name:"Cloud",exact:true})).toBeVisible();
    await f.page.getByLabel("当前角色 产品经理").click();
    await expect(f.page.getByLabel("当前运行模式")).toHaveValue("goose");
    await expect(f.page.getByLabel("当前模型")).toHaveValue("standalone/local-agent");
    await f.page.getByLabel("会话消息").fill("late Agent metadata");
    await f.page.getByLabel("发送", {exact:true}).click();
    await expect(f.page.getByTestId("conversation-status")).toContainText("已暂停");
    expect((await f.requests()).find(r=>r.path.endsWith('/messages')).body).toEqual({content:"late Agent metadata",runtime:"goose",provider_id:"standalone",model:"local-agent"});
    await f.state({events:[{cursor:"late-q",eventId:"late-q",name:"turn_queued",value:{command_id:"command-1"}},{cursor:"late-r",eventId:"late-r",name:"runtime.status.changed",value:{status:"running"}}]});
    await expect(f.page.getByTestId("conversation-status")).toContainText("执行中");
    await expect(f.page.getByLabel("当前运行模式")).toHaveValue("goose");
    await expect(f.page.getByLabel("当前模型")).toHaveValue("standalone/local-agent");
    await expect(f.page.getByLabel("当前运行模式")).toBeDisabled();
    await expect(f.page.getByLabel("当前模型")).toBeDisabled();
  } finally { await f.app.close(); }
});

test("federated review buffers a terminal arriving before the matching ACK", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    await f.state({events:[],holdMessages:true});
    await f.page.getByLabel("当前运行模式").selectOption("goose");
    await f.page.getByLabel("会话消息").fill("terminal before ACK");
    await f.page.getByLabel("发送", {exact:true}).click();
    await expect.poll(async () => (await f.requests()).filter(r=>r.path.endsWith('/messages')).length).toBe(1);
    const events = [{cursor:"q",eventId:"q",name:"turn_queued",value:{command_id:"command-1"}},{cursor:"f",eventId:"f",name:"turn_finished",value:{command_id:"command-1",status:"completed"}}];
    await f.state({events,holdMessages:true});
    await expect(f.page.getByText("执行完成 · Turn finished")).toBeVisible();
    await expect(f.page.getByLabel("当前运行模式")).toBeDisabled();
    await f.state({events});
    await expect(f.page.getByLabel("当前运行模式")).toBeEnabled();
  } finally { await f.app.close(); }
});

test("federated review confirms optimistic messages by command without collapsing equal text", async ({}, info) => {
  const f = await launch(info.outputPath("backend"));
  try {
    await f.page.getByLabel("当前运行模式").selectOption("goose");
    const events: object[] = [];
    for (let turn=1;turn<=2;turn++) {
      await f.page.getByLabel("会话消息").fill("合法重复消息");
      await f.page.getByLabel("发送", {exact:true}).click();
      await expect.poll(async () => (await f.requests()).filter(r=>r.path.endsWith('/messages')).length).toBe(turn);
      events.push({cursor:`q${turn}`,eventId:`q${turn}`,name:"turn_queued",value:{command_id:`command-${turn}`}}, {cursor:`u${turn}`,eventId:`u${turn}`,name:"user.message.received",value:{content:"合法重复消息"}}, {cursor:`f${turn}`,eventId:`f${turn}`,name:"turn_finished",value:{command_id:`command-${turn}`,status:"completed"}});
      await f.state({events});
      await expect(f.page.getByLabel("当前运行模式")).toBeEnabled();
      await expect(f.page.getByText("合法重复消息", {exact:true})).toHaveCount(turn);
    }
    await f.page.getByText("方案评审 · Architecture review", {exact:true}).click();
    await f.page.getByText("Jira 看板配置修复指引", {exact:true}).first().click();
    await expect(f.page.getByText("合法重复消息", {exact:true})).toHaveCount(2);
    await f.page.evaluate(()=>localStorage.removeItem("hermes.v4.conversation-timelines"));
    await f.page.reload();
    await expect(f.page.getByText("合法重复消息", {exact:true})).toHaveCount(2);
  } finally { await f.app.close(); }
});
