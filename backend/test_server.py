"""Sidecar HTTP, WebSocket, OpenCode driver, and directory listing tests."""
import base64
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AGENT_HARNESS_DATA_DIR", tempfile.mkdtemp(prefix="agent-harness-server-"))

import opencode
import server
from sessions import SessionManager

MOCK_AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "mock_acp_agent.py")


class OpenCodeServiceTests(unittest.TestCase):
    def setUp(self):
        opencode._service_ready_until.clear()

    def test_reuses_a_running_service(self):
        with (
            mock.patch.object(opencode, "service_health", return_value=True),
            mock.patch.object(opencode, "opencode_binary") as binary,
        ):
            opencode.ensure_service({}, "/home/test")
        binary.assert_not_called()

    def test_never_starts_for_an_explicit_remote_url(self):
        with (
            mock.patch.object(opencode, "service_health") as health,
            mock.patch.object(opencode, "opencode_binary") as binary,
        ):
            opencode.ensure_service({"base_url": "http://127.0.0.1:9999"}, "/home/test")
        health.assert_not_called()
        binary.assert_not_called()

    def test_starts_the_shared_service_when_needed(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(opencode, "service_health", side_effect=[False, False, True]),
            mock.patch.object(opencode, "opencode_binary", return_value="/usr/bin/opencode"),
            mock.patch.object(opencode.subprocess, "run", return_value=completed) as run,
            mock.patch.object(opencode.time, "sleep"),
            mock.patch.object(opencode, "service_base_url", return_value="http://127.0.0.1:49374"),
        ):
            opencode.ensure_service({}, "/home/test")
        run.assert_called_once_with(
            ["/usr/bin/opencode", "service", "start"],
            capture_output=True,
            text=True,
            timeout=opencode.SERVICE_START_TIMEOUT,
            env=mock.ANY,
        )

    def test_reports_a_missing_binary(self):
        with (
            mock.patch.object(opencode, "service_health", return_value=False),
            mock.patch.object(opencode, "opencode_binary", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                opencode.ensure_service({}, "/home/test")

    def test_invalid_credential_is_surfaced(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".config" / "opencode" / "service.json"
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(opencode.ServiceCredentialInvalid):
                opencode.service_health({}, temp)
            status = opencode.service_status({}, temp)
            self.assertFalse(status["ok"])
            self.assertIn("could not read", status["error"])

    def test_service_status_reports_a_missing_credential(self):
        with tempfile.TemporaryDirectory() as temp:
            status = opencode.service_status({}, temp)
            self.assertTrue(status["ok"])
            self.assertFalse(status["running"])
            self.assertIn("service credential not found", status["error"])

    def test_non_loopback_base_url_is_refused(self):
        status = opencode.service_status({"base_url": "http://example.com:49374"}, "/home/test")
        self.assertFalse(status["ok"])
        self.assertIn("loopback", status["error"])

    def test_sanitized_env_removes_module_credentials(self):
        os.environ["ORQELLA_MODULE_TOKEN"] = "secret"
        os.environ["ORQELLA_MODULE_CONFIG"] = "{}"
        try:
            env = opencode.sanitized_env("/home/test")
            self.assertNotIn("ORQELLA_MODULE_TOKEN", env)
            self.assertNotIn("ORQELLA_MODULE_CONFIG", env)
            self.assertEqual(env["HOME"], "/home/test")
        finally:
            os.environ.pop("ORQELLA_MODULE_TOKEN", None)
            os.environ.pop("ORQELLA_MODULE_CONFIG", None)

    def test_service_config_and_base_url(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".config" / "opencode" / "service.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"port": 50123, "password": "pw"}), encoding="utf-8")
            self.assertEqual(
                opencode.service_base_url({}, temp), "http://127.0.0.1:50123"
            )
            self.assertEqual(
                opencode.service_base_url({"base_url": "http://127.0.0.1:4444"}, temp),
                "http://127.0.0.1:4444",
            )

    def test_pair_returns_urlsafe_auth_token(self):
        def fake_api(config, home, method, path, body=None, accept=None):
            if path == "/api/pair":
                return {"code": "code/one"}
            return {"token": "tok"}

        with mock.patch.object(opencode, "opencode_api", side_effect=fake_api):
            result = opencode.pair({}, "/home/test")
        decoded = base64.urlsafe_b64decode(result["auth_token"] + "==").decode()
        self.assertEqual(decoded, "opencode:tok")

def ws_client_frame(payload, opcode=1):
    payload = payload.encode() if isinstance(payload, str) else payload
    mask = b"\x01\x02\x03\x04"
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + mask + masked


class WebSocketFrameTests(unittest.TestCase):
    def test_round_trip_short_and_long_frames(self):
        left, right = socket.socketpair()
        try:
            for payload in ("hello", "x" * 300, "y" * 70000):
                threading.Thread(
                    target=server.ws_send, args=(left, payload), daemon=True
                ).start()
                opcode, value = server.ws_read(right)
                self.assertEqual(opcode, 1)
                self.assertEqual(value.decode(), payload)
        finally:
            left.close()
            right.close()

    def test_client_frame_is_unmasked(self):
        left, right = socket.socketpair()
        try:
            left.sendall(ws_client_frame("masked"))
            opcode, value = server.ws_read(right)
            self.assertEqual(opcode, 1)
            self.assertEqual(value, b"masked")
        finally:
            left.close()
            right.close()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="agent-harness-http-")
        self.workspace = os.path.join(self.root, "workspace")
        os.makedirs(self.workspace)
        self.old_token = server.TOKEN
        self.old_manager = server.manager
        server.TOKEN = "test-token"
        server.manager = SessionManager(
            os.path.join(self.root, "data"),
            {"allow_adapter_install": False, "max_concurrent_sessions": 4},
        )
        server.manager.host_home_dir = self.root
        server.manager.set_registry(
            {
                "version": 1,
                "default": "mock",
                "harnesses": [
                    {
                        "id": "mock",
                        "label": "Mock",
                        "kind": "acp",
                        "command": sys.executable,
                        "args": [MOCK_AGENT],
                        "delegate_fs": True,
                        "delegate_terminal": True,
                        "enabled": True,
                    }
                ],
            }
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.manager.close()
        server.TOKEN = self.old_token
        server.manager = self.old_manager
        shutil.rmtree(self.root, ignore_errors=True)

    def request(self, method, path, body=None, token="test-token"):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                **({"X-Orqella-Module-Token": token} if token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_requires_the_module_token(self):
        status, _body = self.request("GET", "/api/registry", token=None)
        self.assertEqual(status, 401)
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_registry_and_harnesses(self):
        status, body = self.request("GET", "/api/registry")
        self.assertEqual(status, 200)
        self.assertEqual(body["default"], "mock")
        status, body = self.request("GET", "/api/harnesses")
        self.assertEqual(status, 200)
        self.assertEqual(body["harnesses"][0]["id"], "mock")

    def test_create_list_prompt_and_delete(self):
        status, created = self.request(
            "POST", "/api/sessions", {"harness": "mock", "cwd": self.workspace}
        )
        self.assertEqual(status, 200, created)
        session_id = created["id"]
        status, listing = self.request("GET", "/api/sessions?harness=mock")
        self.assertEqual(status, 200)
        self.assertEqual(listing["sessions"][0]["id"], session_id)
        status, prompt = self.request("POST", f"/api/sessions/{session_id}/prompt", {"text": "hi"})
        self.assertEqual(status, 200, prompt)

        deadline = time.monotonic() + 15
        request_id = None
        while time.monotonic() < deadline and not request_id:
            for key, value in server.manager.pending_permissions.items():
                if value.get("session_id") == session_id:
                    request_id = key
                    break
            time.sleep(0.02)
        self.assertIsNotNone(request_id)
        status, _ = self.request(
            "POST",
            f"/api/sessions/{session_id}/permission",
            {"request_id": request_id, "option_id": "allow"},
        )
        self.assertEqual(status, 200)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and server.manager.sessions[session_id]["status"] != "idle":
            time.sleep(0.02)
        status, transcript = self.request("GET", f"/api/sessions/{session_id}/transcript")
        self.assertEqual(status, 200)
        self.assertTrue(any(event["type"] == "turn_end" for event in transcript["events"]))
        status, deleted = self.request("DELETE", f"/api/sessions/{session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(deleted["deleted"], session_id)

    def test_discover_lists_harness_history(self):
        status, created = self.request(
            "POST", "/api/sessions", {"harness": "mock", "cwd": self.workspace}
        )
        self.assertEqual(status, 200, created)
        status, body = self.request("GET", "/api/sessions/discover?harness=mock")
        self.assertEqual(status, 200)
        self.assertTrue(body["supported"])
        self.assertTrue(
            any(session["session_id"] == created["harness_session_id"] for session in body["sessions"])
        )

    def test_unknown_session_is_404(self):
        status, _ = self.request("GET", "/api/sessions/missing")
        self.assertEqual(status, 404)

    def test_directory_listing_only_returns_directories(self):
        os.makedirs(os.path.join(self.workspace, "src"))
        os.makedirs(os.path.join(self.workspace, ".hidden"))
        with open(os.path.join(self.workspace, "file.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        status, body = self.request("GET", f"/api/fs/dirs?path={self.workspace}")
        self.assertEqual(status, 200)
        self.assertEqual([entry["name"] for entry in body["entries"]], ["src"])
        status, _ = self.request("GET", f"/api/fs/dirs?path={os.path.join(self.workspace, 'file.txt')}")
        self.assertEqual(status, 400)

    def test_websocket_sends_ready_and_events(self):
        status, created = self.request(
            "POST", "/api/sessions", {"harness": "mock", "cwd": self.workspace}
        )
        self.assertEqual(status, 200, created)
        session_id = created["id"]
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                f"GET /ws/sessions/{session_id} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "X-Orqella-Module-Token: test-token\r\n\r\n"
            )
            connection.sendall(request.encode())
            response = connection.recv(4096)
            self.assertIn(b"101 Switching Protocols", response)
            kind = ""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and kind != "ready":
                opcode, payload = server.ws_read(connection)
                self.assertEqual(opcode, 1)
                kind = json.loads(payload)["type"]
            self.assertEqual(kind, "ready")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
