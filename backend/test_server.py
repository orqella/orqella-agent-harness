"""Regression tests for OpenCode service reuse and startup."""
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
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
            env=mock.ANY,
        )

    def test_reports_a_missing_binary(self):
        with (
            mock.patch.object(server, "service_health", return_value=False),
            mock.patch.object(server, "opencode_binary", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                server.ensure_service()

    def test_invalid_credential_is_surfaced(self):
        with tempfile.TemporaryDirectory() as temp:
            old = server.HOST_HOME
            server.HOST_HOME = temp
            try:
                path = Path(temp) / ".config" / "opencode" / "service.json"
                path.parent.mkdir(parents=True)
                path.write_text("{not json", encoding="utf-8")
                with self.assertRaises(server.ServiceCredentialInvalid):
                    server.service_health()
                status = server.service_status()
                self.assertFalse(status["ok"])
                self.assertIn("could not read", status["error"])
            finally:
                server.HOST_HOME = old

    def test_service_status_reports_a_missing_credential(self):
        with tempfile.TemporaryDirectory() as temp:
            old = server.HOST_HOME
            server.HOST_HOME = temp
            try:
                status = server.service_status()
                self.assertTrue(status["ok"])
                self.assertFalse(status["running"])
                self.assertIn("credential not found", status["error"])
            finally:
                server.HOST_HOME = old

    def test_sanitized_env_removes_module_credentials(self):
        with mock.patch.dict(
            os.environ,
            {"ORQELLA_MODULE_TOKEN": "secret", "ORQELLA_MODULE_CONFIG": "{}", "KEEP": "yes"},
            clear=False,
        ):
            env = server.sanitized_env()
        self.assertNotIn("ORQELLA_MODULE_TOKEN", env)
        self.assertNotIn("ORQELLA_MODULE_CONFIG", env)
        self.assertEqual(env.get("KEEP"), "yes")


class ApiMappingTests(unittest.TestCase):
    def test_service_config_and_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            old_home = server.HOST_HOME
            server.HOST_HOME = temp
            try:
                path = Path(temp) / ".config" / "opencode" / "service.json"
                path.parent.mkdir(parents=True)
                path.write_text('{"port": 51234, "password": "secret"}', encoding="utf-8")
                service, found = server.service_config()
                self.assertEqual(service["port"], 51234)
                self.assertEqual(found, str(path))
                self.assertEqual(server.basic_auth(), "b3BlbmNvZGU6c2VjcmV0")
                with mock.patch.object(server, "CONFIGURED_BASE_URL", ""):
                    self.assertEqual(server.service_base_url(), "http://127.0.0.1:51234")
                with mock.patch.object(server, "CONFIGURED_BASE_URL", "http://example.com"):
                    self.assertEqual(server.service_base_url(), "http://example.com")
            finally:
                server.HOST_HOME = old_home

        with tempfile.TemporaryDirectory() as temp:
            old_home = server.HOST_HOME
            server.HOST_HOME = temp
            try:
                with self.assertRaises(RuntimeError):
                    server.service_config()
                bad = Path(temp) / ".config" / "opencode" / "service.json"
                bad.parent.mkdir(parents=True)
                bad.write_text("{not json", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "could not read"):
                    server.service_config()
            finally:
                server.HOST_HOME = old_home

    def test_models_filter_disabled_entries(self) -> None:
        responses = {
            "/api/model": {
                "data": [
                    {"id": "one", "modelID": "m1", "providerID": "p", "enabled": True},
                    {"id": "two", "modelID": "m2", "providerID": "p", "enabled": False},
                ]
            },
            "/api/model/default": {"data": {"id": "one", "providerID": "p"}},
            "/api/provider": {"data": [{"id": "p", "name": "Provider"}, {"name": "No id"}]},
        }

        def fake_api(method, path, body=None, accept=None):
            return responses[path]

        with mock.patch.object(server, "opencode_api", side_effect=fake_api):
            result = server.models()
        self.assertEqual([model["id"] for model in result["models"]], ["one"])
        self.assertEqual(result["default"]["id"], "one")
        self.assertEqual(result["providers"], [{"id": "p", "name": "Provider"}])

    def test_sessions_clamps_limit_and_keeps_cursor(self) -> None:
        calls = []

        def fake_api(method, path, body=None, accept=None):
            calls.append((method, path))
            return {
                "data": [
                    {
                        "id": "s1",
                        "title": "Session",
                        "parentID": None,
                        "agent": "build",
                        "model": {"id": "m"},
                        "time": {"created": 1},
                    }
                ],
                "cursor": "next",
            }

        with mock.patch.object(server, "opencode_api", side_effect=fake_api):
            result = server.sessions({"limit": ["500"], "cursor": ["abc"]})
        self.assertEqual(result["data"][0]["id"], "s1")
        self.assertEqual(result["cursor"], "next")
        self.assertIn("limit=100", calls[0][1])
        self.assertIn("cursor=abc", calls[0][1])
        with mock.patch.object(server, "opencode_api", side_effect=fake_api):
            server.sessions({"limit": ["0"]})
        self.assertIn("limit=1", calls[1][1])
        with mock.patch.object(server, "opencode_api", side_effect=fake_api):
            server.sessions({"limit": ["not-a-number"]})
        self.assertIn("limit=25", calls[2][1])

    def test_pair_returns_urlsafe_auth_token(self) -> None:
        calls = []

        def fake_api(method, path, body=None, accept=None):
            calls.append((method, path, body, accept))
            if path == "/api/pair":
                return {"code": "pair code"}
            return {"token": "token-value"}

        with mock.patch.object(server, "opencode_api", side_effect=fake_api):
            result = server.pair()
        self.assertEqual(result["code"], "pair code")
        self.assertEqual(
            result["auth_token"],
            base64.urlsafe_b64encode(b"opencode:token-value").decode().rstrip("="),
        )
        self.assertEqual(calls[1][1], "/auth/connect/pair%20code")

        with mock.patch.object(server, "opencode_api", return_value={}):
            with self.assertRaises(RuntimeError):
                server.pair()

    def test_launch_validates_and_creates_a_session(self) -> None:
        calls = []

        def fake_api(method, path, body=None, accept=None):
            calls.append((method, path, body))
            if path == "/api/session":
                return {"data": {"id": "s1"}}
            return {"data": {"id": "message-1"}}

        with mock.patch.object(server, "opencode_api", side_effect=fake_api):
            result = server.launch(
                {
                    "prompt": "build it",
                    "model": {"id": "m1", "providerID": "p1", "variant": "fast"},
                }
            )
        self.assertEqual(result["session"]["id"], "s1")
        self.assertEqual(calls[0][2], {"model": {"id": "m1", "providerID": "p1", "variant": "fast"}})
        self.assertEqual(calls[1][1], "/api/session/s1/prompt")
        self.assertEqual(calls[1][2], {"text": "build it"})

        for payload in [
            {},
            {"prompt": " "},
            {"prompt": "x", "model": {"id": "m1"}},
        ]:
            with self.assertRaises(ValueError):
                server.launch(payload)

    def test_public_model_defaults_and_aliases(self) -> None:
        model = server.public_model({"id": "one", "modelID": "m1", "providerID": "p1"})
        self.assertEqual(model["id"], "one")
        self.assertEqual(model["modelID"], "m1")
        self.assertEqual(model["name"], "one")
        self.assertEqual(model["providerID"], "p1")
        self.assertTrue(model["enabled"])
        self.assertIsNone(model["family"])


if __name__ == "__main__":
    unittest.main()
