import { expect, test } from "@playwright/test";
import { conversationApi } from "../src/renderer/api";

function installBridge(request: (value: any) => Promise<any>) {
  (globalThis as any).window = { workbenchBridge: { apiRequest: request } };
}

test("event pages restore over one MiB without exceeding the IPC response bound", async () => {
  const cursors = Array.from({length:12}, (_,i)=>`${Math.floor(i/3)+1}:${i%3}`);
  const frames = cursors.map((cursor,i)=>`id: ${cursor}\ndata: ${JSON.stringify({eventId:`event-${i}`,delta:"x".repeat(100000)})}\n\n`);
  const requested: string[] = [];
  installBridge(async request => {
    const after = request.headers?.["Last-Event-ID"];
    const limit = Number(request.headers?.["X-Event-Page-Bytes"] ?? Number.MAX_SAFE_INTEGER);
    if (after) requested.push(after);
    let text="";
    for (const frame of frames.slice(after ? cursors.indexOf(after)+1 : 0)) {
      if (Buffer.byteLength(text+frame)>limit) break;
      text+=frame;
    }
    if (text.length>1048576) throw new Error("local API response is too large");
    return {status:200,text,body:null};
  });
  const events = await conversationApi.events("isolated-session");
  expect(events.map(event=>event.cursor)).toEqual(cursors);
  expect(requested).toEqual(["1:1","2:0","2:2","3:1","4:0","4:2"]);
});

test("event pages stop after session cancellation instead of requesting another page", async () => {
  let active=true;
  let calls=0;
  installBridge(async () => {calls++;active=false;return {status:200,text:'id: 1:0\ndata: {"eventId":"one"}\n\n',body:null};});
  await expect(conversationApi.events("isolated-session",undefined,{shouldContinue:()=>active})).rejects.toMatchObject({name:"AbortError"});
  expect(calls).toBe(1);
});

test("event pages surface an oversized frame with already fetched history and no retry", async () => {
  let calls=0;
  installBridge(async () => ++calls===1
    ? {status:200,text:'id: 1:0\ndata: {"eventId":"first","delta":"preserved"}\n\n',body:null}
    : {status:413,text:'{"detail":"conversation_event_frame_too_large"}',body:{detail:"conversation_event_frame_too_large"}});
  await expect(conversationApi.events("isolated-session")).rejects.toMatchObject({message:"conversation_event_frame_too_large",events:[expect.objectContaining({eventId:"first"})]});
  expect(calls).toBe(2);
});

test("event pages reject a repeated cursor instead of looping forever", async () => {
  let calls=0;
  installBridge(async () => {calls++;return {status:200,text:'id: 1:0\ndata: {"eventId":"first"}\n\n',body:null};});
  await expect(conversationApi.events("isolated-session")).rejects.toMatchObject({message:"conversation_event_cursor_invalid"});
  expect(calls).toBe(2);
});
