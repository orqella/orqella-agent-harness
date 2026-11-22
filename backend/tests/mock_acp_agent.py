#!/usr/bin/env python3
"""Deterministic ACP agent used by the backend tests.

Speaks just enough ACP v1 to exercise streaming, permissions, filesystem
delegation, terminals, session load/resume, and configuration updates.
"""
import json
import os
import sys


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


_next_id = 1000


def request(method, params):
    global _next_id
    _next_id += 1
    send({"jsonrpc": "2.0", "id": _next_id, "method": method, "params": params})
    while True:
        line = sys.stdin.readline()
        if not line:
            raise SystemExit(1)
        message = json.loads(line)
        if message.get("id") == _next_id and ("result" in message or "error" in message):
            return message


def update(session_id, payload):
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": payload},
        }
    )


sessions = {}
counter = 0


def config_options(current="mock-1"):
    return [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": current,
            "options": [
                {"value": "mock-1", "name": "Mock One"},
                {"value": "mock-2", "name": "Mock Two"},
            ],
        },
        {
            "id": "mode",
            "name": "Mode",
            "category": "mode",
            "type": "select",
            "currentValue": "ask",
            "options": [
                {"value": "ask", "name": "Ask"},
                {"value": "code", "name": "Code"},
            ],
        },
    ]


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "method" not in message:
        continue
    method = message["method"]
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "sessionCapabilities": {"list": {}, "resume": {}, "close": {}, "delete": {}},
                    },
                    "agentInfo": {"name": "Mock Agent", "version": "0.0.0"},
                },
            }
        )
    elif method == "session/new":
        counter += 1
        session_id = f"sess_mock_{counter}"
        sessions[session_id] = params.get("cwd") or os.getcwd()
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"sessionId": session_id, "configOptions": config_options()},
            }
        )
    elif method == "session/load":
        session_id = params.get("sessionId") or ""
        sessions[session_id] = params.get("cwd") or os.getcwd()
        update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "replay-1",
                "content": {"type": "text", "text": "replayed history"},
            },
        )
        send({"jsonrpc": "2.0", "id": request_id, "result": {"configOptions": config_options()}})
    elif method == "session/resume":
        session_id = params.get("sessionId") or ""
        sessions[session_id] = params.get("cwd") or os.getcwd()
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "session/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "sessions": [
                        {
                            "sessionId": session_id,
                            "cwd": cwd,
                            "title": "Mock session",
                            "updatedAt": "2026-01-01T00:00:00Z",
                        }
                        for session_id, cwd in sessions.items()
                    ]
                },
            }
        )
    elif method == "session/close":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "session/delete":
        sessions.pop(params.get("sessionId") or "", None)
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "session/cancel":
        pass
    elif method == "session/set_config_option":
        value = params.get("value")
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "configOptions": config_options(
                        current=str(value) if not isinstance(value, bool) else "mock-1"
                    )
                },
            }
        )
    elif method == "session/prompt":
        session_id = params.get("sessionId") or ""
        cwd = sessions.get(session_id) or os.getcwd()
        message_id = f"msg-{counter}-{os.getpid()}"
        update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": message_id,
                "content": {"type": "text", "text": "Hello "},
            },
        )
        update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": message_id,
                "content": {"type": "text", "text": "from the mock agent."},
            },
        )
        update(
            session_id,
            {
                "sessionUpdate": "agent_thought_chunk",
                "messageId": f"thought-{counter}-{os.getpid()}",
                "content": {"type": "text", "text": "considering"},
            },
        )
        update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-1",
                "title": "Write a file",
                "kind": "edit",
                "status": "pending",
            },
        )
        decision = request(
            "session/request_permission",
            {
                "sessionId": session_id,
                "toolCall": {"toolCallId": "call-1", "title": "Write a file", "kind": "edit"},
                "options": [
                    {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )
        outcome = (decision.get("result") or {}).get("outcome") or {}
        allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow"
        if allowed:
            request(
                "fs/write_text_file",
                {"sessionId": session_id, "path": os.path.join(cwd, "mock-output.txt"), "content": "hello"},
            )
            read = request(
                "fs/read_text_file",
                {"sessionId": session_id, "path": os.path.join(cwd, "mock-output.txt")},
            )
            text = (read.get("result") or {}).get("content") or ""
            terminal = request(
                "terminal/create",
                {
                    "sessionId": session_id,
                    "command": "sh",
                    "args": ["-c", "printf terminal-ok"],
                    "cwd": cwd,
                },
            )
            terminal_id = (terminal.get("result") or {}).get("terminalId")
            request("terminal/wait_for_exit", {"sessionId": session_id, "terminalId": terminal_id})
            output = request("terminal/output", {"sessionId": session_id, "terminalId": terminal_id})
            request("terminal/release", {"sessionId": session_id, "terminalId": terminal_id})
            terminal_text = (output.get("result") or {}).get("output") or ""
            update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-1",
                    "status": "completed",
                    "content": [
                        {"type": "content", "content": {"type": "text", "text": f"read={text}"}},
                        {
                            "type": "diff",
                            "path": os.path.join(cwd, "mock-output.txt"),
                            "oldText": None,
                            "newText": "hello",
                        },
                    ],
                    "rawOutput": {"terminal": terminal_text},
                },
            )
        else:
            update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-1",
                    "status": "failed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "rejected"}}],
                },
            )
        update(
            session_id,
            {
                "sessionUpdate": "plan",
                "entries": [{"content": "Do the thing", "status": "completed", "priority": "high"}],
            },
        )
        update(
            session_id,
            {"sessionUpdate": "session_info_update", "title": "Mock conversation"},
        )
        update(
            session_id,
            {
                "sessionUpdate": "usage_update",
                "used": 1234,
                "size": 10000,
                "cost": {"amount": 0.001, "currency": "USD"},
            },
        )
        send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})
    else:
        if request_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
            )
