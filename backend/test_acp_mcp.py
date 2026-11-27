"""ACP session lifecycle includes the configured MCP servers."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acp import AcpConnection


class FakeHost:
    def mcp_servers(self):
        return [
            {
                "name": "orqella-harness",
                "command": "/usr/bin/orqella",
                "args": ["mcp", "--profile", "read"],
                "env": [{"name": "HOME", "value": "/home/user"}],
            }
        ]


class EmptyHost:
    pass


def connection_with(host):
    connection = AcpConnection.__new__(AcpConnection)
    connection.host = host
    connection.capabilities = {}
    connection.sessions = {}
    connection.requests = []

    def request(method, params, timeout=None):
        connection.requests.append((method, params))
        if method == "session/new":
            return {"sessionId": "sess_test"}
        return {}

    connection.request = request
    return connection


class AcpMcpInjectionTests(unittest.TestCase):
    def test_new_load_and_resume_carry_the_server(self):
        connection = connection_with(FakeHost())
        connection.new_session("/tmp/workspace")
        connection.load_session("sess_test", "/tmp/workspace")
        connection.resume_session("sess_test", "/tmp/workspace")
        self.assertEqual([method for method, _ in connection.requests], [
            "session/new",
            "session/load",
            "session/resume",
        ])
        for method, params in connection.requests:
            self.assertEqual(params["mcpServers"][0]["name"], "orqella-harness", method)
            self.assertEqual(
                params["mcpServers"][0]["args"],
                ["mcp", "--profile", "read"],
                method,
            )

    def test_empty_host_keeps_the_mcp_list_empty(self):
        connection = connection_with(EmptyHost())
        connection.new_session("/tmp/workspace")
        self.assertEqual(connection.requests[0][1]["mcpServers"], [])


if __name__ == "__main__":
    unittest.main()
