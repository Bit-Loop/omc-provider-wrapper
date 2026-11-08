"""Provider configuration survives the HTTP subprocess environment boundary."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_chat_api.config import Settings
from agent_chat_api.environment import provider_subprocess_environment, wrapper_process_environment
from agent_chat_api.providers import ChatRunner


class OpenCodeEnvironmentTests(unittest.TestCase):
    def test_only_declared_api_keys_and_config_paths_reach_opencode(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "providers.json"
            config.write_text(json.dumps({"apis": {
                "lab": {"key_env": "LAB_KEY"},
                "invalid": {"key_env": "INVALID-NAME"},
                "wrapper": {"key_env": "CHAT_API_KEY"},
            }}))
            source = {"HOME": directory, "PATH": "/bin", "OMC_WRAPPER_CONFIG_DIR": directory,
                      "OPENCODE_CONFIG_DIR": str(Path(directory) / "opencode"),
                      "OPENCODE_CONFIG": str(Path(directory) / "extra.json"),
                      "LAB_KEY": "fixture-auth", "UNRELATED_SECRET": "fixture-other",
                      "CHAT_API_KEY": "fixture-wrapper", "INVALID-NAME": "fixture-invalid"}
            child = provider_subprocess_environment(source, provider="opencode")
            self.assertEqual(child.get("LAB_KEY"), source["LAB_KEY"])
            self.assertEqual(child.get("OPENCODE_CONFIG_DIR"), source["OPENCODE_CONFIG_DIR"])
            self.assertEqual(child.get("OPENCODE_CONFIG"), source["OPENCODE_CONFIG"])
            for name in ("CHAT_API_KEY", "UNRELATED_SECRET", "INVALID-NAME"):
                self.assertNotIn(name, child)
            self.assertNotIn("LAB_KEY", provider_subprocess_environment(source, provider="codex"))
            self.assertNotIn("OPENCODE_CONFIG_DIR", provider_subprocess_environment(source, provider="claude"))

    def test_config_override_and_wrapper_launch_preserve_declared_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "custom.json"
            config.write_text(json.dumps({"apis": {"lab": {"key_env": "LAB_KEY"}}}))
            source = {"HOME": directory, "OMC_WRAPPER_PROVIDER_CONFIG": str(config),
                      "LAB_KEY": "fixture-auth", "UNRELATED_SECRET": "fixture-other"}
            wrapper = wrapper_process_environment(source)
            child = provider_subprocess_environment(wrapper, provider="opencode")
            self.assertEqual(child.get("LAB_KEY"), source["LAB_KEY"])
            self.assertEqual(wrapper.get("OMC_WRAPPER_PROVIDER_CONFIG"), str(config))
            self.assertNotIn("UNRELATED_SECRET", wrapper)

    def test_xdg_default_config_is_loaded_without_changing_the_advisor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "omc-provider-wrapper"
            root.mkdir()
            (root / "providers.json").write_text(json.dumps({"apis": {"lab": {"key_env": "LAB_KEY"}}}))
            child = provider_subprocess_environment({"XDG_CONFIG_HOME": directory, "LAB_KEY": "fixture-auth"}, provider="opencode")
            self.assertIn("LAB_KEY", child)
            agent = json.loads(child["OPENCODE_CONFIG_CONTENT"])["agents"]["omc-advisor"]
            self.assertEqual(agent["permissions"][0]["effect"], "deny")

    def test_missing_or_invalid_config_does_not_inherit_other_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "providers.json"
            source = {"OMC_WRAPPER_PROVIDER_CONFIG": str(config), "UNRELATED_SECRET": "fixture-other"}
            self.assertNotIn("UNRELATED_SECRET", provider_subprocess_environment(source, provider="opencode"))
            config.write_text("invalid")
            self.assertNotIn("UNRELATED_SECRET", provider_subprocess_environment(source, provider="opencode"))

    def test_readiness_probes_use_the_same_opencode_config_and_autostart_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = str(Path(directory) / "opencode")
            runner = ChatRunner(Settings(workspace_root=Path(directory)))
            with patch.dict(os.environ, {"OPENCODE_CONFIG_DIR": config_dir}, clear=True), patch(
                "agent_chat_api.providers.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "2.0.24", ""),
            ) as run:
                status = runner._provider_status("opencode", "/fixture/opencode")
            self.assertTrue(status["available"])
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["env"].get("OPENCODE_CONFIG_DIR"), config_dir)
                self.assertEqual(call.kwargs["env"].get("OPENCODE_NO_AUTOLLAMA"), "1")
