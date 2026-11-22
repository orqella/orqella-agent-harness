"""Registry validation and driver resolution tests."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry


class RegistryTests(unittest.TestCase):
    def test_default_registry_is_normalized(self):
        value = registry.normalize(registry.default_registry())
        ids = [entry["id"] for entry in value["harnesses"]]
        self.assertEqual(ids, ["opencode", "claude", "codex", "gemini", "custom"])
        self.assertEqual(value["default"], "opencode")
        self.assertEqual(value["harnesses"][0]["kind"], "acp")

    def test_opencode_is_a_normal_acp_harness(self):
        value = registry.normalize(registry.default_registry())
        opencode = next(entry for entry in value["harnesses"] if entry["id"] == "opencode")
        self.assertEqual(opencode["kind"], "acp")
        self.assertEqual(opencode["command"], "opencode")
        self.assertEqual(opencode["args"], ["acp"])
        self.assertEqual(opencode["web"]["mount"], "opencode")
        self.assertEqual(opencode["web"]["handoff"], "opencode")
        self.assertEqual(opencode["transport"], "opencode")

    def test_builtin_web_default_is_filled_for_older_registries(self):
        value = registry.normalize(
            {
                "harnesses": [
                    {"id": "opencode", "kind": "acp", "command": "opencode", "args": ["acp"]},
                ],
                "default": "opencode",
            }
        )
        self.assertEqual(value["harnesses"][0]["web"]["mount"], "opencode")

    def test_builtin_transport_default_is_filled_for_older_registries(self):
        value = registry.normalize(
            {
                "harnesses": [
                    {"id": "opencode", "kind": "acp", "command": "opencode", "args": ["acp"]},
                ],
                "default": "opencode",
            }
        )
        self.assertEqual(value["harnesses"][0]["transport"], "opencode")

    def test_unknown_transport_is_rejected(self):
        with self.assertRaisesRegex(registry.RegistryError, "unknown transport"):
            registry.normalize(
                {"harnesses": [{"id": "x", "command": "x", "transport": "telepathy"}]}
            )

    def test_web_requires_a_mount(self):
        with self.assertRaisesRegex(registry.RegistryError, "web requires a mount"):
            registry.normalize(
                {"harnesses": [{"id": "x", "command": "x", "web": {"url": "http://127.0.0.1:1"}}]}
            )

    def test_builtin_kind_cannot_be_changed(self):
        value = registry.normalize(
            {
                "harnesses": [
                    {"id": "opencode", "kind": "exec", "command": "python3"},
                ],
                "default": "opencode",
            }
        )
        self.assertEqual(value["harnesses"][0]["kind"], "acp")

    def test_rejects_duplicate_ids(self):
        with self.assertRaisesRegex(registry.RegistryError, "duplicate"):
            registry.normalize(
                {
                    "harnesses": [
                        {"id": "one", "command": "x"},
                        {"id": "one", "command": "y"},
                    ]
                }
            )

    def test_rejects_unknown_kind_and_missing_command(self):
        with self.assertRaisesRegex(registry.RegistryError, "unknown kind"):
            registry.normalize({"harnesses": [{"id": "x", "kind": "tmux", "command": "x"}]})
        with self.assertRaisesRegex(registry.RegistryError, "needs a command"):
            registry.normalize({"harnesses": [{"id": "x", "command": "  "}]})

    def test_rejects_bad_ids(self):
        for bad in ("", " one", "UPPER!", "-leading"):
            with self.assertRaises(registry.RegistryError):
                registry.normalize({"harnesses": [{"id": bad, "command": "x"}]})

    def test_fallback_only_runs_when_allowed(self):
        entry = registry.normalize(
            {
                "harnesses": [
                    {
                        "id": "probe",
                        "command": "definitely-not-installed-adapter",
                        "fallback": {"command": "python3", "args": ["-c", "pass"]},
                    }
                ]
            }
        )["harnesses"][0]
        command, _args, reason = registry.resolve_command(entry, allow_fallback=False)
        self.assertIsNone(command)
        self.assertIn("not found", reason)
        command, args, source = registry.resolve_command(entry, allow_fallback=True)
        self.assertTrue(command)
        self.assertEqual(source, "fallback")
        self.assertEqual(args, ["-c", "pass"])

    def test_env_references_are_preserved_but_resolved_at_spawn(self):
        from acp import resolve_env_value

        os.environ["ORQELLA_TEST_ENV"] = "value"
        self.assertEqual(resolve_env_value("${ORQELLA_TEST_ENV}"), "value")
        self.assertEqual(resolve_env_value("$ORQELLA_TEST_ENV"), "value")
        self.assertIsNone(resolve_env_value("${ORQELLA_TEST_MISSING}"))

    def test_public_registry_includes_missing_fallbacks(self):
        value = registry.public_registry(registry.default_registry())
        opencode = next(entry for entry in value["harnesses"] if entry["id"] == "opencode")
        self.assertIsNone(opencode["fallback"])
        self.assertTrue(opencode["builtin"])
        self.assertTrue(opencode["enabled"])

    def test_probe_reports_missing_binary(self):
        result = registry.probe(
            {
                "id": "missing",
                "command": "definitely-not-installed-adapter",
                "kind": "acp",
                "args": [],
            }
        )
        self.assertFalse(result["installed"])
        self.assertIn("not found", result["error"])


if __name__ == "__main__":
    unittest.main()
