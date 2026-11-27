"""Tests for injecting the Orqella MCP gateway into ACP sessions."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessions import orqella_mcp_servers


class OrqellaMcpServersTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agent-harness-mcp-")

    def test_read_profile_is_enabled_by_default(self):
        servers = orqella_mcp_servers(
            {},
            self.home,
            binary=sys.executable,
            environ={"PATH": ""},
        )
        self.assertEqual(len(servers), 1)
        server = servers[0]
        self.assertEqual(server["name"], "orqella-harness")
        self.assertEqual(server["command"], os.path.abspath(sys.executable))
        self.assertEqual(server["args"], ["mcp", "--profile", "read"])
        self.assertIn({"name": "HOME", "value": self.home}, server["env"])

    def test_dev_profile_and_portable_home_are_forwarded(self):
        servers = orqella_mcp_servers(
            {"orqella_mcp": True, "orqella_mcp_profile": "dev"},
            self.home,
            binary=sys.executable,
            environ={"PATH": "", "ORQELLA_HOME": "/tmp/orqella-portable"},
        )
        self.assertEqual(servers[0]["args"], ["mcp", "--profile", "dev"])
        self.assertIn(
            {"name": "ORQELLA_HOME", "value": "/tmp/orqella-portable"},
            servers[0]["env"],
        )

    def test_invalid_profile_falls_back_to_read(self):
        servers = orqella_mcp_servers(
            {"orqella_mcp": True, "orqella_mcp_profile": "admin"},
            self.home,
            binary=sys.executable,
            environ={"PATH": ""},
        )
        self.assertEqual(servers[0]["args"], ["mcp", "--profile", "read"])

    def test_disabled_or_missing_binary_returns_nothing(self):
        self.assertEqual(
            orqella_mcp_servers(
                {"orqella_mcp": False},
                self.home,
                binary=sys.executable,
                environ={"PATH": ""},
            ),
            [],
        )
        self.assertEqual(
            orqella_mcp_servers(
                {"orqella_mcp": True},
                self.home,
                binary="/no/such/orqella",
                environ={"PATH": ""},
            ),
            [],
        )
        self.assertEqual(
            orqella_mcp_servers(
                {"orqella_mcp": True},
                self.home,
                binary=None,
                environ={"PATH": "/no/such"},
            ),
            [],
        )

    def test_orqella_bin_environment_is_used(self):
        servers = orqella_mcp_servers(
            {"orqella_mcp": True},
            self.home,
            binary=None,
            environ={"PATH": "", "ORQELLA_BIN": sys.executable},
        )
        self.assertEqual(servers[0]["command"], os.path.abspath(sys.executable))


if __name__ == "__main__":
    unittest.main()
