"""Strict Telegram tool-authority regression tests.

These use unittest so the focused suite remains runnable in production
environments that intentionally do not install pytest.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from hermes_cli.tools_config import _get_platform_tools
from toolsets import resolve_toolset


APPROVED_TOOLSETS = {"clarify", "memory", "todo", "vision", "web"}
APPROVED_TOOLS = {
    "clarify", "memory", "todo", "vision_analyze", "web_extract", "web_search",
}


def live_shape(*, known=...):
    config = {"platform_toolsets": {"telegram": sorted(APPROVED_TOOLSETS)}}
    if known is not ...:
        config["known_plugin_toolsets"] = {"telegram": known}
    return config


class TelegramStrictAllowlistTests(unittest.TestCase):
    def resolve(self, config):
        return _get_platform_tools(
            config, "telegram", include_default_mcp_servers=False
        )

    def test_null_plugin_ledger_does_not_raise_and_live_config_resolves(self):
        self.assertEqual(self.resolve(live_shape(known=None)), APPROVED_TOOLSETS)

    def test_effective_toolsets_and_tools_are_exact(self):
        toolsets = self.resolve(live_shape(known=None))
        tools = set()
        for name in toolsets:
            tools.update(resolve_toolset(name))
        self.assertEqual(toolsets, APPROVED_TOOLSETS)
        self.assertEqual(tools, APPROVED_TOOLS)
        self.assertFalse(any(name.startswith("kanban_") for name in tools))

    def test_new_plugin_is_absent_unless_explicitly_listed(self):
        with patch(
            "hermes_cli.tools_config._get_plugin_toolset_keys",
            return_value={"new_plugin"},
        ):
            self.assertNotIn("new_plugin", self.resolve(live_shape(known=None)))
            config = live_shape(known=None)
            config["platform_toolsets"]["telegram"].append("new_plugin")
            self.assertIn("new_plugin", self.resolve(config))

    def test_recovered_non_configurable_toolset_requires_explicit_listing(self):
        self.assertNotIn("kanban", self.resolve(live_shape(known=None)))
        config = live_shape(known=None)
        config["platform_toolsets"]["telegram"].append("kanban")
        self.assertIn("kanban", self.resolve(config))

    def test_other_platform_recovery_is_unchanged(self):
        config = {"platform_toolsets": {"cli": ["web", "terminal"]}}
        self.assertIn(
            "kanban",
            _get_platform_tools(config, "cli", include_default_mcp_servers=False),
        )

    def test_malformed_non_telegram_platform_map_preserves_legacy_failure(self):
        # The historical ``value or {}`` treated a falsy malformed value as
        # missing, but failed when the malformed value was truthy.
        self.assertEqual(
            _get_platform_tools(
                {"platform_toolsets": []},
                "cli",
                include_default_mcp_servers=False,
            ),
            _get_platform_tools({}, "cli", include_default_mcp_servers=False),
        )
        with self.assertRaises(AttributeError):
            _get_platform_tools(
                {"platform_toolsets": ["malformed"]},
                "cli",
                include_default_mcp_servers=False,
            )

    def test_malformed_platform_map_and_value_fail_closed(self):
        self.assertEqual(self.resolve({"platform_toolsets": []}), set())
        self.assertEqual(
            self.resolve({"platform_toolsets": {"telegram": None}}), set()
        )

    def test_missing_null_and_empty_have_distinct_documented_semantics(self):
        # Missing platform entry keeps the historical Telegram default.
        self.assertTrue(self.resolve({}))
        # Explicit null is malformed and explicit [] is a deliberate deny-all.
        self.assertEqual(
            self.resolve({"platform_toolsets": {"telegram": None}}), set()
        )
        self.assertEqual(
            self.resolve({"platform_toolsets": {"telegram": []}}), set()
        )

    def test_null_plugin_ledger_is_safe_on_legacy_missing_platform_path(self):
        with patch(
            "hermes_cli.tools_config._get_plugin_toolset_keys",
            return_value={"new_plugin"},
        ):
            resolved = self.resolve(
                {"known_plugin_toolsets": {"telegram": None}}
            )
        self.assertIn("new_plugin", resolved)

    def test_malformed_non_telegram_plugin_ledger_preserves_legacy_failures(self):
        with patch(
            "hermes_cli.tools_config._get_plugin_toolset_keys",
            return_value={"new_plugin"},
        ):
            with self.assertRaises(AttributeError):
                _get_platform_tools(
                    {"known_plugin_toolsets": ["malformed"]},
                    "cli",
                    include_default_mcp_servers=False,
                )
            with self.assertRaises(TypeError):
                _get_platform_tools(
                    {"known_plugin_toolsets": {"cli": None}},
                    "cli",
                    include_default_mcp_servers=False,
                )

    def test_explicit_global_mcp_is_not_inherited(self):
        config = live_shape(known=None)
        config["mcp_servers"] = {"extra-mcp": {"enabled": True}}
        self.assertNotIn("extra-mcp", self.resolve(config))
        config["platform_toolsets"]["telegram"].append("extra-mcp")
        self.assertIn("extra-mcp", self.resolve(config))

    def test_real_subprocess_exercises_production_registry_path(self):
        root = Path(__file__).resolve().parents[2]
        script = r'''import json
import sys
import yaml
sys.path.insert(0, sys.argv[1])
from hermes_cli.config import load_config
from hermes_cli.plugins import discover_plugins, get_plugin_manager
discover_plugins()
import model_tools
from hermes_cli.tools_config import _get_platform_tools
from toolsets import resolve_toolset
from tools.registry import registry
config = load_config()
toolsets = sorted(_get_platform_tools(config, "telegram"))
tools = set()
for toolset in toolsets:
    tools.update(resolve_toolset(toolset))
tools = sorted(name for name in tools if registry.get_entry(name) is not None)
manager = get_plugin_manager()
payload = {
    "toolsets": toolsets,
    "tools": tools,
    "kanban": sorted(name for name in tools if name.startswith("kanban_")),
    "plugin_tools": sorted(set(tools) & manager._plugin_tool_names),
    "mcp_tools": sorted(
        name for name in registry.get_all_tool_names()
        if (registry.get_toolset_for_tool(name) or "").startswith("mcp-")
    ),
}
print("TELEGRAM_REGISTRY=" + json.dumps(payload, sort_keys=True))
'''
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.yaml").write_text(
                yaml.safe_dump(live_shape(known=None)), encoding="utf-8"
            )
            env = {
                "HOME": directory,
                "HERMES_HOME": directory,
                "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            proc = subprocess.run(
                [sys.executable, "-I", "-c", script, str(root)],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        marker = "TELEGRAM_REGISTRY="
        lines = [line for line in proc.stdout.splitlines() if line.startswith(marker)]
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        import json

        payload = json.loads(lines[0][len(marker):])
        self.assertEqual(set(payload["toolsets"]), APPROVED_TOOLSETS)
        self.assertEqual(set(payload["tools"]), APPROVED_TOOLS)
        self.assertEqual(payload["kanban"], [])
        self.assertEqual(payload["plugin_tools"], [])
        self.assertEqual(payload["mcp_tools"], [])


if __name__ == "__main__":
    unittest.main()
