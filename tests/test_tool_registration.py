import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"

REGISTRATION_SCRIPT = """
import importlib.util
import sys

from astrbot.core.provider.register import llm_tools

spec = importlib.util.spec_from_file_location("gossip_registration_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

registered = {
    tool.name: tool
    for tool in llm_tools.func_list
    if tool.handler.__module__ == module.__name__
}
expected = {
    "wake_qq_session_task",
    "get_qq_session_messages",
    "reply_qq_session_task",
    "get_available_groups",
    "get_friend_list",
    "get_target_group_members",
}
assert set(registered) == expected, sorted(registered)
for name, tool in registered.items():
    properties = tool.parameters.get("properties", {})
    assert not {"self", "event"}.intersection(properties), (name, properties)

groups = registered["get_available_groups"]
assert groups.parameters["properties"] == {}, groups.parameters
description = " ".join(groups.description.lower().split())
assert "empty group whitelist allows all group destinations" in description, description
"""


class ToolRegistrationTests(unittest.TestCase):
    def test_all_public_tools_register_with_real_astrbot_schema_parser(self):
        # Real API imports initialize globals; isolate them from live AstrBot
        # data and from the other tests' lightweight adapter stubs.
        with tempfile.TemporaryDirectory(
            prefix="codex.gossip-registration."
        ) as temp_dir:
            env = os.environ.copy()
            env.update(
                ASTRBOT_ROOT=temp_dir,
                TMPDIR=temp_dir,
                TEMP=temp_dir,
                TMP=temp_dir,
                PYTHONDONTWRITEBYTECODE="1",
                ASTRBOT_DASHBOARD_INITIAL_PASSWORD="Registration-only-test-123!",
            )
            # Preserve the caller's SDK paths when the child changes cwd.
            env["PYTHONPATH"] = os.pathsep.join(
                str(Path(path or ".").resolve()) for path in sys.path
            )
            result = subprocess.run(
                [sys.executable, "-c", REGISTRATION_SCRIPT, str(PLUGIN_PATH)],
                cwd=temp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(
            result.returncode,
            0,
            f"Real AstrBot tool registration failed:\n{result.stdout}\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
