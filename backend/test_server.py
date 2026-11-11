"""Regression tests for OpenCode service reuse and startup."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import server


class EnsureServiceTests(unittest.TestCase):
    def setUp(self):
        server._service_ready_until = 0.0

    def test_reuses_a_running_service(self):
        with (
            mock.patch.object(server, "service_health", return_value=True),
            mock.patch.object(server, "opencode_binary") as binary,
        ):
            server.ensure_service()
        binary.assert_not_called()

    def test_never_starts_for_an_explicit_remote_url(self):
        with (
            mock.patch.object(server, "CONFIGURED_BASE_URL", "http://127.0.0.1:9999"),
            mock.patch.object(server, "service_health") as health,
            mock.patch.object(server, "opencode_binary") as binary,
        ):
            server.ensure_service()
        health.assert_not_called()
        binary.assert_not_called()

    def test_starts_the_shared_service_when_needed(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(server, "service_health", side_effect=[False, False, True]),
            mock.patch.object(server, "opencode_binary", return_value="/usr/bin/opencode"),
            mock.patch.object(server.subprocess, "run", return_value=completed) as run,
            mock.patch.object(server.time, "sleep"),
        ):
            server.ensure_service()
        run.assert_called_once_with(
            ["/usr/bin/opencode", "service", "start"],
            capture_output=True,
            text=True,
            timeout=server.SERVICE_START_TIMEOUT,
        )

    def test_reports_a_missing_binary(self):
        with (
            mock.patch.object(server, "service_health", return_value=False),
            mock.patch.object(server, "opencode_binary", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                server.ensure_service()


if __name__ == "__main__":
    unittest.main()
