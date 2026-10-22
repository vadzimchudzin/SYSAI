#!/usr/bin/env python3
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import bootstrap


def env(**extra):
    base = {
        "SYSAI_BASE_URL": "https://sysai.onrender.com",
        "SYSAI_MCP_TOKEN": "test-only-mcp-secret-0000",
        "SYSAI_MODEL_PROXY_TOKEN": "test-only-proxy-secret-0000",
        "ZEROCLAW_GATEWAY_TOKEN": "test-only-gateway-token-0000",
        "SYSAI_MODEL_ID": "sysai-agent",
        "PORT": "10000",
    }
    base.update(extra)
    return base


class BootstrapTests(unittest.TestCase):
    def test_mcp_http_and_model_proxy(self):
        text = bootstrap.build_config(env())
        self.assertIn('transport = "http"', text)
        self.assertIn("https://sysai.onrender.com/execution/mcp", text)
        self.assertIn("https://sysai.onrender.com/execution/v1", text)
        self.assertIn('wire_api = "chat_completions"', text)
        self.assertIn('model = "sysai-agent"', text)

    def test_exact_approved_tools_only(self):
        text = bootstrap.build_config(env())
        self.assertIn("sysai__list_approved_tasks", text)
        self.assertIn("dispatch_task_action", text)
        self.assertNotIn("*", text)
        self.assertIn('excluded_tools = ["exec"', text)
        self.assertIn("http_request = false", text)

    def test_rejects_http_base_and_unknown_tools(self):
        with self.assertRaises(SystemExit):
            bootstrap.build_config(env(SYSAI_BASE_URL="http://sysai.onrender.com"))
        with self.assertRaises(SystemExit):
            bootstrap.build_config(
                env(SYSAI_MCP_TOOLS_JSON='[{"name":"exec"}]')
            )

    def test_config_file_not_world_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "config.toml"
            bootstrap.write_config(bootstrap.build_config(env()), dest)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

    def test_start_script_runs_official_gateway(self):
        source = pathlib.Path(__file__).with_name("start.sh").read_text()
        self.assertIn("exec zeroclaw gateway --port", source)
        self.assertNotIn("telegram", source.lower())


if __name__ == "__main__":
    unittest.main()
