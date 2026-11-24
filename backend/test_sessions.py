"""Session manager, ACP delegation, and transcript tests."""
import os
import shutil
import sys
import tempfile
import time
import unittest
import json
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acp import AcpError
import sessions as sessions_module
from sessions import SessionManager

MOCK_AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "mock_acp_agent.py")


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class SessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="agent-harness-test-")
        self.workspace = os.path.join(self.root, "workspace")
        os.makedirs(self.workspace)
        self.manager = SessionManager(
            os.path.join(self.root, "data"),
            {
                "allow_adapter_install": False,
                "max_concurrent_sessions": 4,
                "transcript_retention_days": 30,
            },
        )
        self.manager.host_home_dir = self.root
        self.manager.set_registry(
            {
                "version": 1,
                "default": "mock",
                "harnesses": [
                    {
                        "id": "mock",
                        "label": "Mock agent",
                        "kind": "acp",
                        "command": sys.executable,
                        "args": [MOCK_AGENT],
                        "delegate_fs": True,
                        "delegate_terminal": True,
                        "enabled": True,
                    },
                    {
                        "id": "echo",
                        "label": "Echo command",
                        "kind": "exec",
                        "command": sys.executable,
                        "args": ["-c", "print('hello from exec')", "{prompt}"],
                        "enabled": True,
                    },
                ],
            }
        )

    def tearDown(self):
        self.manager.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def session_events(self, session_id, kind=None):
        events = self.manager.events.get(session_id, [])
        if kind is None:
            return events
        return [event for event in events if event.get("type") == kind]

    def wait_for_permission(self, session_id):
        def predicate():
            pending = [
                request_id
                for request_id, value in self.manager.pending_permissions.items()
                if value.get("session_id") == session_id
            ]
            return pending

        self.assertTrue(wait_for(predicate), "permission request never arrived")
        return [
            request_id
            for request_id, value in self.manager.pending_permissions.items()
            if value.get("session_id") == session_id
        ][0]

    def test_create_reports_capabilities_and_config(self):
        created = self.manager.create("mock", self.workspace)
        self.assertEqual(created["harness"], "mock")
        self.assertEqual(created["status"], "idle")
        self.assertEqual(created["cwd"], self.workspace)
        self.assertTrue(created["config_options"])
        self.assertTrue(created["harness_session_id"].startswith("sess_mock_"))
        self.assertEqual(self.manager.list("mock")[0]["id"], created["id"])

    def test_prompt_streams_updates_and_writes_files(self):
        created = self.manager.create("mock", self.workspace)
        session_id = created["id"]
        self.manager.prompt(session_id, "do the thing")
        request_id = self.wait_for_permission(session_id)
        self.manager.resolve_permission(request_id, "allow")
        self.assertTrue(
            wait_for(lambda: self.manager.sessions[session_id]["status"] == "idle"),
            "session never became idle",
        )
        with open(os.path.join(self.workspace, "mock-output.txt"), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello")
        messages = self.session_events(session_id, "message")
        agent_text = "".join(
            event["content"]["text"]
            for event in messages
            if event.get("role") == "agent" and event["content"].get("type") == "text"
        )
        self.assertIn("Hello from the mock agent.", agent_text)
        self.assertTrue(self.session_events(session_id, "tool_call"))
        self.assertTrue(self.session_events(session_id, "plan"))
        self.assertTrue(self.session_events(session_id, "permission_request"))
        self.assertTrue(self.session_events(session_id, "turn_end"))
        usage = self.session_events(session_id, "usage")
        self.assertTrue(usage)
        self.assertEqual(usage[-1]["usage"]["used"], 1234)
        self.assertEqual(self.manager.sessions[session_id]["usage"]["size"], 10000)
        self.assertEqual(self.manager.sessions[session_id]["title"], "Mock conversation")

        transcript = self.manager.transcript(session_id)
        self.assertIn("turn_end", [event["type"] for event in transcript])
        self.assertEqual(transcript[-1]["seq"], self.manager.sequences[session_id])

    def test_rejected_permission_skips_side_effects(self):
        created = self.manager.create("mock", self.workspace)
        session_id = created["id"]
        self.manager.prompt(session_id, "do not write")
        request_id = self.wait_for_permission(session_id)
        self.manager.resolve_permission(request_id, "reject")
        self.assertTrue(wait_for(lambda: self.manager.sessions[session_id]["status"] == "idle"))
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "mock-output.txt")))
        resolved = self.session_events(session_id, "permission_resolved")
        self.assertEqual(resolved[-1]["option_id"], "reject")

    def test_filesystem_delegation_is_confined_to_the_workspace(self):
        created = self.manager.create("mock", self.workspace)
        with self.assertRaises(AcpError):
            self.manager.read_text("/etc/passwd")
        with self.assertRaises(AcpError):
            self.manager.write_text("/tmp/agent-harness-escape.txt", "no")
        inside = os.path.join(self.workspace, "inside.txt")
        self.manager.write_text(inside, "ok")
        self.assertEqual(self.manager.read_text(inside), "ok")
        self.last_session = created

    def test_prompt_while_working_is_allowed(self):
        created = self.manager.create("mock", self.workspace)
        session_id = created["id"]

        class FakeConnection:
            capabilities = {"loadSession": True}
            sessions = {created["harness_session_id"]: {}}

            def session_capabilities(self):
                return {}

            def roots(self, _session_id):
                return [self_workspace]

            def prompt(self, _session_id, _blocks):
                return {"stopReason": "end_turn"}

        self_workspace = self.workspace
        fake = FakeConnection()
        self.manager.connection = lambda _harness: fake
        self.manager.sessions[session_id]["status"] = "working"
        self.manager.prompt(session_id, "steer")
        self.assertTrue(wait_for(lambda: self.manager.sessions[session_id]["status"] == "idle"))
        self.assertTrue(any(
            event.get("type") == "message"
            and event.get("role") == "user"
            and (event.get("content") or {}).get("text") == "steer"
            for event in self.manager.events[session_id]
        ))
        self.assertFalse(self.session_events(session_id, "error"))

    def use_service_registry(self):
        self.manager.registry = {
            "version": 1,
            "default": "opencode",
            "harnesses": [
                {
                    "id": "opencode",
                    "label": "OpenCode",
                    "kind": "acp",
                    "transport": "opencode",
                    "command": "opencode",
                    "args": ["acp"],
                    "web": {
                        "mount": "opencode",
                        "url": "http://127.0.0.1:49374",
                        "path": "/server/{server_key}/session/{session}",
                        "handoff": "opencode",
                    },
                    "delegate_fs": True,
                    "delegate_terminal": True,
                    "enabled": True,
                }
            ],
        }

    def test_service_transport_create_and_prompt(self):
        self.use_service_registry()
        with (
            mock.patch.object(self.manager, "_ensure_service_watch"),
            mock.patch.object(
                sessions_module.opencode_service,
                "create",
                return_value={"id": "ses_service", "title": "Service session"},
            ),
        ):
            created = self.manager.create("opencode", self.workspace)
        self.assertEqual(created["harness_session_id"], "ses_service")
        self.assertEqual(created["title"], "Service session")
        session_id = created["id"]

        with (
            mock.patch.object(self.manager, "_ensure_service_watch"),
            mock.patch.object(
                sessions_module.opencode_service,
                "prompt",
                return_value={"data": {"id": "msg_user"}},
            ) as prompt,
        ):
            self.manager.prompt(session_id, "hello service")
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.args[3], "hello service")
        self.assertEqual(self.manager.sessions[session_id]["status"], "working")
        users = [
            event
            for event in self.session_events(session_id, "message")
            if event.get("role") == "user"
        ]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["content"]["text"], "hello service")

    def test_service_transport_stream_mapping(self):
        self.use_service_registry()
        row = self.manager._new_row("opencode", "ses_stream", self.workspace, "Stream")
        session_id = row["id"]

        def event(kind, **data):
            self.manager.handle_service_event(
                "opencode", {"type": kind, "data": {"sessionID": "ses_stream", **data}}
            )

        event(
            "session.inbox.enqueued",
            inboxID="msg_user",
            item={"type": "user", "payload": {"text": "hello"}},
        )
        event("session.execution.started")
        event("session.reasoning.started", assistantMessageID="msg_a", ordinal=0)
        event("session.reasoning.delta", assistantMessageID="msg_a", ordinal=0, delta="thinking")
        event("session.reasoning.ended", assistantMessageID="msg_a", ordinal=0, text="thinking harder")
        event("session.text.started", assistantMessageID="msg_a", ordinal=0)
        event("session.text.delta", assistantMessageID="msg_a", ordinal=0, delta="Hi ")
        event("session.text.delta", assistantMessageID="msg_a", ordinal=0, delta="there")
        event("session.text.ended", assistantMessageID="msg_a", ordinal=0, text="Hi there")
        event(
            "session.tool.called",
            id="call_1",
            name="shell",
            input={"command": "pwd"},
        )
        event(
            "session.tool.success",
            id="call_1",
            name="shell",
            content=[{"type": "text", "text": "/tmp/workspace\n"}],
            metadata={"status": "completed", "exit": 0},
        )
        event("session.execution.succeeded")

        messages = self.session_events(session_id, "message")
        users = [message for message in messages if message.get("role") == "user"]
        agents = [message for message in messages if message.get("role") == "agent"]
        thoughts = [message for message in messages if message.get("role") == "thought"]
        self.assertEqual([message["content"]["text"] for message in users], ["hello"])
        self.assertEqual("".join(message["content"]["text"] for message in agents), "Hi there")
        self.assertEqual("".join(message["content"]["text"] for message in thoughts), "thinking")
        tools = self.session_events(session_id, "tool_call")
        self.assertEqual(tools[-1]["status"], "completed")
        self.assertEqual(tools[-1]["kind"], "execute")
        self.assertEqual(tools[-1]["title"], "pwd")
        self.assertIn("/tmp/workspace", tools[-1]["content"][0]["content"]["text"])
        self.assertEqual(self.manager.sessions[session_id]["status"], "idle")
        self.assertEqual(len(self.session_events(session_id, "turn_end")), 1)

    def test_service_transport_refresh_replays_messages(self):
        self.use_service_registry()
        row = self.manager._new_row("opencode", "ses_refresh", self.workspace, "Refresh")
        session_id = row["id"]
        rows = [
            {"id": "msg_1", "type": "user", "text": "first"},
            {
                "id": "msg_2",
                "type": "assistant",
                "content": [
                    {"type": "reasoning", "text": "hmm"},
                    {"type": "text", "text": "answer"},
                    {
                        "type": "tool",
                        "id": "call_2",
                        "name": "read",
                        "state": {
                            "status": "completed",
                            "input": {"filePath": "/tmp/a.txt"},
                            "content": [{"type": "text", "text": "contents"}],
                        },
                    },
                ],
            },
            {"id": "msg_3", "type": "idle", "outcome": "succeeded"},
        ]
        row["service_needs_sync"] = True
        with mock.patch.object(
            sessions_module.opencode_service,
            "message_page",
            return_value=(list(reversed(rows)), "older-cursor"),
        ):
            result = self.manager.refresh(session_id)
        self.assertGreater(result["events"], 1)
        self.assertFalse(result["session"]["needs_sync"])
        self.assertTrue(result["session"]["has_history"])
        messages = self.session_events(session_id, "message")
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"]["text"], "first")
        self.assertEqual(messages[1]["role"], "thought")
        self.assertEqual(messages[2]["role"], "agent")
        tools = self.session_events(session_id, "tool_call")
        self.assertEqual(tools[-1]["kind"], "read")
        self.assertEqual(tools[-1]["title"], "read /tmp/a.txt")
        self.assertTrue(self.session_events(session_id, "turn_end"))

    def test_acp_commands_are_listed_and_forwarded_as_prompts(self):
        created = self.manager.create("mock", self.workspace)
        session_id = created["id"]
        self.manager.update(
            created["harness_session_id"],
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [
                    {"name": "compact", "description": "Compact the context"},
                    {"name": "init", "description": "Initialize", "input": {"hint": "optional text"}},
                ],
            },
        )
        commands = self.manager.commands(session_id)["commands"]
        self.assertEqual([command["name"] for command in commands], ["compact", "init"])
        self.assertEqual(commands[1]["input"]["hint"], "optional text")
        with mock.patch.object(self.manager, "prompt") as prompt:
            self.manager.command(session_id, "init", "extra")
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.args[1], "/init extra")

    def test_acp_commands_offer_compact_when_not_advertised(self):
        created = self.manager.create("mock", self.workspace)
        commands = self.manager.commands(created["id"])["commands"]
        self.assertEqual(commands[0]["name"], "compact")
        self.assertTrue(commands[0]["suggested"])

    def test_service_commands_and_command_execution(self):
        self.use_service_registry()
        row = self.manager._new_row("opencode", "ses_cmd", self.workspace, "Commands")
        with (
            mock.patch.object(
                sessions_module.opencode_service,
                "commands",
                return_value=[{"name": "init", "description": "Initialize"}],
            ),
            mock.patch.object(
                sessions_module.opencode_service,
                "models",
                return_value=[{"value": "p/m", "label": "p/Model"}],
            ),
            mock.patch.object(
                sessions_module.opencode_service,
                "agents",
                return_value=[{"value": "build", "label": "Build"}],
            ),
        ):
            commands = self.manager.commands(row["id"])["commands"]
            self.assertEqual(
                [command["name"] for command in commands],
                ["compact", "model", "agent", "init"],
            )
            self.assertEqual(commands[1]["options"][0]["value"], "p/m")
            self.assertEqual(commands[2]["options"][0]["value"], "build")
            with mock.patch.object(
                sessions_module.opencode_service, "command", return_value={}
            ) as command:
                self.manager.command(row["id"], "init", "extra")
            command.assert_called_once()
            self.assertEqual(command.call_args.args[3], "init")
            self.assertEqual(command.call_args.args[4], "extra")
            with mock.patch.object(
                sessions_module.opencode_service,
                "compact",
                return_value={"data": {"id": "msg_compact"}},
            ) as compact:
                self.manager.command(row["id"], "compact")
            compact.assert_called_once()
            users = [
                event
                for event in self.session_events(row["id"], "message")
                if event.get("role") == "user"
            ]
            self.assertEqual(users[-1]["content"]["text"], "/compact")
            with mock.patch.object(
                sessions_module.opencode_service, "set_model", return_value={}
            ) as set_model:
                self.manager.command(row["id"], "model", "p/m#high")
            set_model.assert_called_once()
            self.assertEqual(set_model.call_args.args[3], "p")
            self.assertEqual(set_model.call_args.args[4], "m")
            self.assertEqual(set_model.call_args.args[5], "high")
            with mock.patch.object(
                sessions_module.opencode_service, "set_agent", return_value={}
            ) as set_agent:
                self.manager.command(row["id"], "agent", "plan")
            set_agent.assert_called_once()
            self.assertEqual(set_agent.call_args.args[3], "plan")
            with mock.patch.object(self.manager, "_prompt_service") as prompt:
                self.manager.command(row["id"], "unknown", "hello")
            prompt.assert_called_once()
            self.assertEqual(prompt.call_args.args[2], "/unknown hello")

    def test_service_compaction_is_rendered_as_a_command(self):
        self.use_service_registry()
        row = self.manager._new_row("opencode", "ses_compaction", self.workspace, "Compact")
        self.manager.handle_service_event(
            "opencode",
            {
                "type": "session.inbox.enqueued",
                "data": {
                    "sessionID": "ses_compaction",
                    "inboxID": "msg_compaction",
                    "item": {"type": "compaction", "payload": {}},
                },
            },
        )
        users = [
            event
            for event in self.session_events(row["id"], "message")
            if event.get("role") == "user"
        ]
        self.assertEqual(users[-1]["content"]["text"], "/compact")
        replayed = self.manager._service_events(
            row["id"],
            [{"id": "msg_compaction", "type": "compaction", "payload": {}}],
        )
        self.assertEqual(replayed[0]["content"]["text"], "/compact")

    def test_service_selection_events_are_notices(self):
        self.use_service_registry()
        row = self.manager._new_row("opencode", "ses_select", self.workspace, "Select")
        self.manager.handle_service_event(
            "opencode",
            {
                "type": "session.model.selected",
                "data": {
                    "sessionID": "ses_select",
                    "model": {"providerID": "p", "id": "m", "variant": "high"},
                },
            },
        )
        self.manager.handle_service_event(
            "opencode",
            {
                "type": "session.agent.selected",
                "data": {"sessionID": "ses_select", "agent": "plan"},
            },
        )
        notices = self.session_events(row["id"], "notice")
        self.assertEqual(notices[0]["update"]["title"], "Model: p/m#high")
        self.assertEqual(notices[1]["update"]["title"], "Agent: plan")

    def test_transcript_sequence_survives_a_restart(self):
        row = self.manager._new_row("mock", "ses_seq", self.workspace, "Seq")
        self.manager._emit(row["id"], {"type": "status", "status": "idle"})
        self.manager._emit(row["id"], {"type": "status", "status": "idle"})
        self.assertEqual(self.manager._transcript_sequence(row["transcript"]), 2)
        restarted = SessionManager(self.manager.data_dir, {})
        try:
            self.assertEqual(restarted.sequences.get(row["id"]), 2)
        finally:
            restarted.close()

    def test_service_transport_marks_missed_history(self):
        self.use_service_registry()
        row = self.manager._new_row("opencode", "ses_dirty", self.workspace, "Dirty")
        self.manager._mark_service_dirty("opencode")
        self.assertTrue(self.manager.public_session(row)["needs_sync"])

    def test_service_transport_pages_older_history(self):
        self.use_service_registry()
        row = self.manager._new_row("opencode", "ses_history", self.workspace, "History")
        row["history_cursor"] = "cursor-1"
        older = [
            {"id": "msg_old", "type": "user", "text": "older prompt"},
            {
                "id": "msg_old_a",
                "type": "assistant",
                "content": [{"type": "text", "text": "older answer"}],
            },
        ]
        with mock.patch.object(
            sessions_module.opencode_service,
            "message_page",
            return_value=(list(reversed(older)), "cursor-2"),
        ) as page:
            result = self.manager.history(row["id"])
        self.assertTrue(result["has_more"])
        self.assertEqual([event.get("role") for event in result["events"] if event["type"] == "message"], ["user", "agent"])
        self.assertEqual(result["events"][0]["content"]["text"], "older prompt")
        self.assertEqual(row["history_cursor"], "cursor-2")
        page.assert_called_once()
        self.assertEqual(page.call_args.kwargs["cursor"], "cursor-1")
        batches = [event for event in self.manager.events[row["id"]] if event["type"] == "history"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]["events"]), 2)
        self.assertTrue(self.manager.public_session(row)["has_history"])

    def test_service_transport_adopt_discovery_and_delete(self):
        self.use_service_registry()
        info = {
            "id": "ses_remote",
            "title": "Remote session",
            "location": {"directory": self.workspace},
        }
        messages = [{"id": "msg_1", "type": "user", "text": "remote"}]
        with (
            mock.patch.object(self.manager, "_ensure_service_watch"),
            mock.patch.object(sessions_module.opencode_service, "info", return_value=info),
            mock.patch.object(
                sessions_module.opencode_service,
                "message_page",
                return_value=(messages, ""),
            ),
        ):
            adopted = self.manager.adopt("opencode", "ses_remote", self.workspace)
        self.assertEqual(adopted["title"], "Remote session")
        self.assertEqual(adopted["harness_session_id"], "ses_remote")

        discovered_rows = [
            {
                "id": "ses_remote",
                "title": "Remote session",
                "location": {"directory": self.workspace},
                "time": {"updated": 1794860000000, "busy": 2, "idle": 1},
            }
        ]
        with mock.patch.object(
            sessions_module.opencode_service, "list_sessions", return_value=discovered_rows
        ):
            discovered = self.manager.discover("opencode")
        self.assertTrue(discovered["supported"])
        self.assertEqual(discovered["sessions"][0]["session_id"], "ses_remote")
        self.assertTrue(discovered["sessions"][0]["native_active"])
        self.assertEqual(discovered["sessions"][0]["status"], "working")

        with mock.patch.object(
            sessions_module.opencode_service, "remove", return_value=True
        ) as remove:
            deleted = self.manager.delete(adopted["id"])
        remove.assert_called_once()
        self.assertTrue(deleted["native_deleted"])
        self.assertNotIn(adopted["id"], self.manager.sessions)

    def test_config_updates_are_forwarded(self):
        created = self.manager.create("mock", self.workspace)
        result = self.manager.config(created["id"], "model", "mock-2")
        model = next(option for option in result["config_options"] if option["id"] == "model")
        self.assertEqual(model["currentValue"], "mock-2")

    def test_prompt_reconnects_a_restarted_agent(self):
        created = self.manager.create("mock", self.workspace)
        session_id = created["id"]
        connection = self.manager.connections.pop("mock")
        connection.close()
        self.manager.prompt(session_id, "after restart")
        request_id = self.wait_for_permission(session_id)
        self.manager.resolve_permission(request_id, "allow")
        self.assertTrue(wait_for(lambda: self.manager.sessions[session_id]["status"] == "idle"))
        messages = self.session_events(session_id, "message")
        self.assertTrue(any(
            (event.get("content") or {}).get("text", "") == "after restart"
            for event in messages
            if event.get("role") == "user"
        ))
        self.assertFalse(self.session_events(session_id, "error"))

    def test_resume_after_detach(self):
        created = self.manager.create("mock", self.workspace)
        session_id = created["id"]
        self.manager.sessions[session_id]["status"] = "detached"
        resumed = self.manager.resume(session_id)
        self.assertEqual(resumed["session"]["status"], "idle")

    def test_delete_removes_the_transcript(self):
        created = self.manager.create("mock", self.workspace)
        session_id = created["id"]
        transcript = os.path.join(self.manager.data_dir, self.manager.sessions[session_id]["transcript"])
        self.manager.delete(session_id)
        self.assertFalse(os.path.exists(transcript))
        self.assertIsNone(self.manager.sessions.get(session_id))

    def test_exec_driver_captures_a_session_id(self):
        created = self.manager.create("echo", self.workspace)
        session_id = created["id"]
        self.manager.prompt(session_id, "ignored")
        self.assertTrue(wait_for(lambda: self.manager.sessions[session_id]["status"] == "idle"))
        messages = self.session_events(session_id, "message")
        self.assertTrue(any("hello from exec" in event["content"]["text"] for event in messages))
        self.assertEqual(self.manager.sessions[session_id]["harness_session_id"], "")
        # A second prompt must run the command again, not try to use an ACP
        # connection for the exec driver.
        self.manager.prompt(session_id, "again")
        self.assertTrue(wait_for(lambda: self.manager.sessions[session_id]["status"] == "idle"))
        self.assertEqual(len(self.session_events(session_id, "turn_end")), 2)

    def test_terminal_delegation_captures_output(self):
        created = self.manager.create("mock", self.workspace)
        terminal = self.manager.create_terminal(
            created["harness_session_id"],
            "sh",
            ["-c", "printf terminal-ok"],
            [],
            self.workspace,
            None,
        )
        self.assertTrue(wait_for(lambda: terminal.done.is_set(), timeout=5))
        output = self.manager.terminal_output(terminal.id)
        self.assertIn("terminal-ok", output["output"])
        terminal.release()

    def test_terminal_rejects_a_cwd_outside_the_workspace(self):
        created = self.manager.create("mock", self.workspace)
        with self.assertRaisesRegex(AcpError, "outside"):
            self.manager.create_terminal(
                created["harness_session_id"],
                "sh",
                ["-c", "true"],
                [],
                "/tmp",
                None,
            )

    def test_adopt_indexes_an_external_session(self):
        adopted = self.manager.adopt("mock", "sess_from_build", self.workspace, "Build session")
        self.assertEqual(adopted["harness"], "mock")
        self.assertEqual(adopted["harness_session_id"], "sess_from_build")
        self.assertEqual(adopted["cwd"], self.workspace)
        self.assertEqual(adopted["status"], "idle")
        self.assertTrue(adopted["title"])
        replayed = [
            event for event in self.manager.events.get(adopted["id"], [])
            if event.get("type") == "message"
            and "replayed history" in (event.get("content") or {}).get("text", "")
        ]
        self.assertTrue(replayed, "session/load replay was not recorded")
        again = self.manager.adopt("mock", "sess_from_build", self.workspace)
        self.assertEqual(again["id"], adopted["id"])

    def test_discover_lists_harness_history(self):
        created = self.manager.create("mock", self.workspace)
        result = self.manager.discover("mock")
        self.assertTrue(result["supported"])
        self.assertTrue(
            any(session["session_id"] == created["harness_session_id"] for session in result["sessions"])
        )
        self.assertFalse(self.manager.discover("echo")["supported"])

    def test_adopt_rejects_non_acp_harnesses(self):
        with self.assertRaisesRegex(ValueError, "not an ACP harness"):
            self.manager.adopt("echo", "sess_x", self.workspace)

    def test_legacy_native_registry_is_migrated(self):
        data = os.path.join(self.root, "legacy-data")
        os.makedirs(data)
        with open(os.path.join(data, "harnesses.json"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "version": 1,
                "default": "opencode",
                "harnesses": [{"id": "opencode", "kind": "native-service", "command": "opencode"}],
            }))
        migrated = SessionManager(data, {})
        try:
            entry = migrated.entry("opencode")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["kind"], "acp")
            self.assertEqual(entry["args"], ["acp"])
        finally:
            migrated.close()

    def test_native_activity_marks_opencode_sessions(self):
        row = self.manager._new_row("opencode", "ses_native", self.workspace, "Native")
        registry = {"harnesses": [{"id": "opencode", "web": {"handoff": "opencode"}, "enabled": True}]}
        with (
            mock.patch.object(self.manager, "registry", registry),
            mock.patch.object(
                sessions_module,
                "opencode_activity",
                return_value={"ses_native": {"active": True, "updated": 123456}},
            ),
        ):
            self.manager._activity_cache = {"at": 0.0, "value": {}, "available": False}
            public = self.manager.public_session(row)
        self.assertTrue(public["native_active"])
        self.assertEqual(public["native_updated"], 123456)
        self.assertEqual(public["status"], "working")

        row["status"] = "working"
        with (
            mock.patch.object(self.manager, "registry", registry),
            mock.patch.object(
                sessions_module,
                "opencode_activity",
                return_value={"ses_native": {"active": False, "updated": 123466}},
            ),
        ):
            self.manager._activity_cache = {"at": 0.0, "value": {}, "available": False}
            public = self.manager.public_session(row)
        self.assertFalse(public["native_active"])
        self.assertEqual(public["status"], "idle")

        row["status"] = "waiting"
        with (
            mock.patch.object(self.manager, "registry", registry),
            mock.patch.object(
                sessions_module,
                "opencode_activity",
                return_value={"ses_native": {"active": True, "updated": 123476}},
            ),
        ):
            self.manager._activity_cache = {"at": 0.0, "value": {}, "available": False}
            public = self.manager.public_session(row)
        self.assertEqual(public["status"], "waiting")

    def test_refresh_replaces_history_from_the_harness(self):
        created = self.manager.create("mock", self.workspace)
        session_id = created["id"]
        self.manager.prompt(session_id, "old turn")
        request_id = self.wait_for_permission(session_id)
        self.manager.resolve_permission(request_id, "allow")
        self.assertTrue(wait_for(lambda: self.manager.sessions[session_id]["status"] == "idle"))
        result = self.manager.refresh(session_id)
        self.assertGreater(result["events"], 1)
        events = self.manager.events[session_id]
        self.assertEqual(events[0]["type"], "reset")
        self.assertTrue(any(
            event.get("type") == "message"
            and "replayed history" in (event.get("content") or {}).get("text", "")
            for event in events
        ))
        self.assertEqual(result["session"]["status"], "idle")

    def test_public_status_hides_detached(self):
        row = self.manager._new_row("mock", "ses_detached", self.workspace, "Detached")
        row["status"] = "detached"
        public = self.manager.public_session(row)
        self.assertEqual(public["status"], "idle")

    def test_delete_removes_local_and_native_session(self):
        created = self.manager.create("mock", self.workspace)
        transcript = os.path.join(self.manager.data_dir, self.manager.sessions[created["id"]]["transcript"])
        result = self.manager.delete(created["id"], native=True)
        self.assertTrue(result["native_deleted"])
        self.assertIsNone(self.manager.sessions.get(created["id"]))
        self.assertFalse(os.path.exists(transcript))
        discovered = self.manager.discover("mock")["sessions"]
        self.assertFalse(any(session["session_id"] == created["harness_session_id"] for session in discovered))

    def test_forget_removes_a_history_session(self):
        created = self.manager.create("mock", self.workspace)
        harness_session_id = created["harness_session_id"]
        self.manager._remove_local(created["id"])
        result = self.manager.forget("mock", harness_session_id, native=True)
        self.assertTrue(result["native_deleted"])
        discovered = self.manager.discover("mock")["sessions"]
        self.assertFalse(any(session["session_id"] == harness_session_id for session in discovered))

    def test_registry_rejects_unknown_harness(self):
        with self.assertRaisesRegex(ValueError, "unknown harness"):
            self.manager.create("nope", self.workspace)

    def test_retention_prunes_old_transcripts(self):
        created = self.manager.create("mock", self.workspace)
        self.manager._emit(created["id"], {"type": "status", "status": "idle"})
        transcript = os.path.join(self.manager.data_dir, self.manager.sessions[created["id"]]["transcript"])
        with open(transcript, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        old = time.time() - 90 * 86400
        os.utime(transcript, (old, old))
        self.manager._prune_transcripts()
        self.assertFalse(os.path.exists(transcript))


if __name__ == "__main__":
    unittest.main()
