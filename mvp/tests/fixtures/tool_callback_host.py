"""Local NDJSON peer for exercising the real platform client tool transport."""
import json
import sys

envelope = None
mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
cursor = 0


def send(frame):
    print(json.dumps(frame), flush=True)


def event(kind, payload):
    global cursor
    cursor += 1
    send({"kind": "event", "payload": {
        "event_id": f"event-{cursor}", "run_id": envelope["run_id"],
        "term_id": envelope["term_id"], "step_id": envelope["step_id"],
        "cursor": cursor, "type": kind, "payload": payload, "required": False,
    }})


for line in sys.stdin:
    frame = json.loads(line)
    kind = frame["type"]
    payload = frame["payload"]
    response = {"kind": "response", "type": kind, "command_id": frame["command_id"]}
    if kind == "runtime.capabilities":
        send({**response, "payload": {
            "runtime_id": "fake-v2", "build_id": "python:test-build",
            "query": True, "model": True, "tools": True, "streaming": True,
            "event_cursor": True,
            "pause_resume": mode == "bad_control_pending_write",
        }})
    elif kind == "query.start":
        envelope = payload["envelope"]
        send({**response, "payload": {"accepted": True}})
        event("runtime.status", {"status": "running"})
        identity = {key: envelope[key] for key in (
            "session_id", "run_id", "term_id", "step_id", "command_id", "agent_id", "agent_role")}
        send({"kind": "request", "type": "tool.execute", "request_id": "req-1",
              "payload": {"identity": identity, "tool_call_id": "call-1",
                          "tool_id": "file_tool", "arguments": {"text": "actual file"}}})
        if mode == "early_terminal":
            event("runtime.status", {"status": "completed"})
        elif mode == "invalid_tool_event_pending_write":
            event("tool.call", {
                "tool_call_id": "runtime-call-1", "tool_id": "not-granted",
                "read_only": False, "effect_id": "runtime-effect-1",
            })
    elif kind == "tool.result":
        event("assistant.delta", {"text": payload["output"]["text"]})
        event("runtime.status", {"status": "completed"})
    elif kind == "query.status":
        send({**response, "payload": {
            "state": "terminal", "sealed": True, "run_id": envelope["run_id"],
            "term_id": envelope["term_id"], "step_id": envelope["step_id"],
            "terminal_cursor": cursor,
        }})
    elif kind == "query.cancel":
        send({**response, "payload": {"accepted": True}})
        event("runtime.status", {"status": "cancelled"})
    elif kind == "query.pause" and mode == "bad_control_pending_write":
        send({**response, "payload": {"state": "not-paused"}})
