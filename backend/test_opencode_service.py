"""Managed-service client limits and pagination."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import opencode_service

ENTRY = {
    "id": "opencode",
    "kind": "acp",
    "transport": "opencode",
    "web": {"url": "http://127.0.0.1:49374", "handoff": "opencode"},
}


class ServiceClientTests(unittest.TestCase):
    def test_session_list_limit_is_clamped_to_the_service_maximum(self):
        with mock.patch.object(
            opencode_service, "request", return_value={"data": []}
        ) as request:
            opencode_service.list_sessions(ENTRY, "/home/test", limit=500)
        self.assertIn("limit=200", request.call_args.args[3])

    def test_message_limit_is_clamped_and_cursors_are_followed(self):
        pages = [
            {"data": [{"id": "msg_1"}], "cursor": {"next": "next-page"}},
            {"data": [{"id": "msg_2"}], "cursor": {}},
        ]
        with mock.patch.object(
            opencode_service, "request", side_effect=pages
        ) as request:
            rows = opencode_service.messages(ENTRY, "/home/test", "ses_x", limit=1000)
        self.assertEqual([row["id"] for row in rows], ["msg_1", "msg_2"])
        self.assertIn("limit=200", request.call_args_list[0].args[3])
        self.assertIn("cursor=next-page", request.call_args_list[1].args[3])


class ServiceMcpTests(unittest.TestCase):
    def test_orqella_server_is_put_with_host_home_and_profile(self):
        with mock.patch.object(
            opencode_service, "request", return_value={}
        ) as request:
            installed = opencode_service.ensure_mcp(
                ENTRY,
                "/home/test",
                [
                    {
                        "name": "orqella-harness",
                        "command": "/usr/bin/orqella",
                        "args": ["mcp", "--profile", "read"],
                        "env": [{"name": "HOME", "value": "/home/test"}],
                    }
                ],
            )
        self.assertEqual(installed, ["orqella-harness"])
        self.assertEqual(request.call_args.args[2], "PUT")
        self.assertEqual(
            request.call_args.args[3], "/api/experimental/mcp/orqella-harness"
        )
        config = request.call_args.args[4]["config"]
        self.assertEqual(config["type"], "local")
        self.assertEqual(
            config["command"],
            ["/usr/bin/orqella", "mcp", "--profile", "read"],
        )
        self.assertEqual(config["environment"], {"HOME": "/home/test"})
        self.assertFalse(config["codemode"])


if __name__ == "__main__":
    unittest.main()


class ServiceCommandTests(unittest.TestCase):
    def test_command_list_is_scoped_to_the_session_directory(self):
        directory = "/tmp/command-cache-test"
        with (
            mock.patch.object(
                opencode_service, "base_url", return_value="http://127.0.0.1:49374"
            ),
            mock.patch.object(
                opencode_service,
                "request",
                return_value={"data": [{"name": "init", "description": "Init"}]},
            ) as request,
        ):
            rows = opencode_service.commands(ENTRY, "/home/test", directory)
        self.assertEqual(rows, [{"name": "init", "description": "Init"}])
        self.assertIn("location%5Bdirectory%5D", request.call_args.args[3])

    def test_compact_and_command_use_the_service_endpoints(self):
        with mock.patch.object(opencode_service, "request", return_value={}) as request:
            opencode_service.compact(ENTRY, "/home/test", "ses_x")
            self.assertEqual(request.call_args.args[2], "POST")
            self.assertTrue(request.call_args.args[3].endswith("/compact"))
            opencode_service.command(ENTRY, "/home/test", "ses_x", "init", "extra")
            self.assertEqual(request.call_args.args[2], "POST")
            self.assertTrue(request.call_args.args[3].endswith("/command"))
            self.assertEqual(request.call_args.args[4], {"name": "init", "text": "extra"})


class ServiceSelectionTests(unittest.TestCase):
    def test_models_and_agents_are_filtered_for_sessions(self):
        directory = "/tmp/selection-cache-test"
        models = [
            {"providerID": "p", "id": "m", "name": "Model", "enabled": True, "type": "chat"},
            {"providerID": "p", "id": "off", "name": "Off", "enabled": False, "type": "chat"},
            {"providerID": "p", "id": "img", "name": "Image", "enabled": True, "type": "image"},
        ]
        agents = [
            {"id": "build", "name": "Build", "mode": "primary", "hidden": False},
            {"id": "explore", "name": "Explore", "mode": "subagent", "hidden": False},
            {"id": "hidden", "name": "Hidden", "mode": "primary", "hidden": True},
        ]
        with (
            mock.patch.object(
                opencode_service, "base_url", return_value="http://127.0.0.1:49374"
            ),
            mock.patch.object(
                opencode_service, "request", side_effect=[{"data": models}, {"data": agents}]
            ) as request,
        ):
            self.assertEqual(
                opencode_service.models(ENTRY, "/home/test", directory),
                [{"value": "p/m", "label": "Model", "description": "p"}],
            )
            self.assertEqual(
                opencode_service.agents(ENTRY, "/home/test", directory),
                [{"value": "build", "label": "Build", "description": ""}],
            )
        self.assertIn("location%5Bdirectory%5D", request.call_args_list[0].args[3])
        self.assertIn("location%5Bdirectory%5D", request.call_args_list[1].args[3])

    def test_set_model_and_agent_use_session_endpoints(self):
        with mock.patch.object(opencode_service, "request", return_value={}) as request:
            opencode_service.set_model(ENTRY, "/home/test", "ses_x", "p", "m", "high")
            self.assertTrue(request.call_args.args[3].endswith("/model"))
            self.assertEqual(
                request.call_args.args[4],
                {"model": {"providerID": "p", "id": "m", "variant": "high"}},
            )
            opencode_service.set_agent(ENTRY, "/home/test", "ses_x", "plan")
            self.assertTrue(request.call_args.args[3].endswith("/agent"))
            self.assertEqual(request.call_args.args[4], {"agent": "plan"})
