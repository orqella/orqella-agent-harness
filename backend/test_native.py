"""Generic web workspace status and handoff tests."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import native
import opencode


class NativeTests(unittest.TestCase):
    def test_generic_web_workspace(self):
        entry = {"id": "webby", "web": {"mount": "webby", "url": "http://127.0.0.1:9000", "path": "/s/{session}"}}
        status = native.status(entry, "/home/test")
        self.assertTrue(status["available"])
        self.assertTrue(status["running"])
        self.assertEqual(status["mount"], "webby")
        self.assertEqual(native.pair(entry, "/home/test")["auth_token"], "")

    def test_no_web_workspace(self):
        status = native.status({"id": "plain"}, "/home/test")
        self.assertFalse(status["available"])

    def test_service_url_falls_back_to_the_state_file(self):
        with tempfile.TemporaryDirectory() as home:
            config_dir = os.path.join(home, ".config", "opencode")
            state_dir = os.path.join(home, ".local", "state", "opencode")
            os.makedirs(config_dir)
            os.makedirs(state_dir)
            with open(os.path.join(config_dir, "service.json"), "w", encoding="utf-8") as handle:
                json.dump({"password": "secret"}, handle)
            with open(os.path.join(state_dir, "service.json"), "w", encoding="utf-8") as handle:
                json.dump(
                    {"url": "http://127.0.0.1:49999", "password": "secret"},
                    handle,
                )
            self.assertEqual(
                opencode.service_base_url({}, home),
                "http://127.0.0.1:49999",
            )

    def test_opencode_handoff_uses_the_service_helper(self):
        entry = {"id": "opencode", "web": {"mount": "opencode", "url": "http://127.0.0.1:49374", "handoff": "opencode"}}
        with (
            mock.patch.object(native.opencode, "service_status", return_value={"ok": True, "running": True, "url": "http://127.0.0.1:49374", "error": ""}),
            mock.patch.object(native.opencode, "ensure_service") as ensure,
            mock.patch.object(native.opencode, "pair", return_value={"auth_token": "token"}),
        ):
            self.assertTrue(native.status(entry, "/home/test")["running"])
            native.start(entry, "/home/test")
            ensure.assert_called_once()
            self.assertEqual(native.pair(entry, "/home/test")["auth_token"], "token")


if __name__ == "__main__":
    unittest.main()
