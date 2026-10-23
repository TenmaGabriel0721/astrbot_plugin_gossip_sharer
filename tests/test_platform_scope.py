import ast
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
METHOD_NAMES = {
    "_build_session_id",
    "_adapter_kind",
    "_platform_kind",
    "_event_kind",
    "_get_platform_by_id",
    "_effective_target_platform_id",
    "_target_policy",
    "_validate_target",
    "_official_session",
    "_attach_official_raw_message",
    "_track_official_delivery",
}
source_tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
plugin_class = next(
    node
    for node in source_tree.body
    if isinstance(node, ast.ClassDef) and node.name == "GossipSharer"
)
test_class = ast.ClassDef(
    name="TestableGossipSharer",
    bases=[],
    keywords=[],
    decorator_list=[],
    body=[
        node
        for node in plugin_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in METHOD_NAMES
    ],
)
constants = [
    node
    for node in source_tree.body
    if isinstance(node, ast.Assign)
    and any(
        isinstance(target, ast.Name)
        and target.id
        in {"ONEBOT_ADAPTERS", "OFFICIAL_ADAPTERS"}
        for target in node.targets
    )
]
module = ast.fix_missing_locations(
    ast.Module(body=[*constants, test_class], type_ignores=[])
)


class _FakeBotpyMessage:
    def __init__(self, api, event_id, data):
        self.api = api
        self.id = data.get("id")
        self.content = data.get("content")
        self.group_openid = data.get("group_openid")
        self.author = SimpleNamespace(**data.get("author", {}))


class _FakeGroupMessage(_FakeBotpyMessage):
    pass


class _FakeC2CMessage(_FakeBotpyMessage):
    pass


_fake_botpy_message = ModuleType("botpy.message")
_fake_botpy_message.GroupMessage = _FakeGroupMessage
_fake_botpy_message.C2CMessage = _FakeC2CMessage
_fake_botpy = ModuleType("botpy")
_fake_botpy.message = _fake_botpy_message
sys.modules["botpy"] = _fake_botpy
sys.modules["botpy.message"] = _fake_botpy_message

namespace = {
    "asyncio": asyncio,
    "time": time,
    "AstrMessageEvent": object,
    "AstrBotMessage": object,
}
exec(compile(module, str(PLUGIN_PATH), "exec"), namespace)
GossipSharer = namespace["TestableGossipSharer"]


class _FakePlatform:
    def __init__(self, platform_id: str, adapter: str):
        self._meta = SimpleNamespace(id=platform_id, name=adapter)
        self.client = SimpleNamespace(api="fake-api")

    def meta(self):
        return self._meta


class _FakeEvent:
    def __init__(self, platform_id: str, adapter: str):
        self._platform_id = platform_id
        self._adapter = adapter

    def get_platform_id(self):
        return self._platform_id

    def get_platform_name(self):
        return self._adapter


def _make_plugin(default_platform: str = "") -> GossipSharer:
    plugin = GossipSharer.__new__(GossipSharer)
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(
            platform_insts=[
                _FakePlatform("napcat", "aiocqhttp"),
                _FakePlatform("napcat2", "aiocqhttp"),
                _FakePlatform("official", "qq_official"),
                _FakePlatform("tg", "telegram"),
            ]
        )
    )
    plugin.default_platform = default_platform
    plugin.group_whitelist = ["123456"]
    plugin.sister_qq = "10001"
    plugin.official_group_whitelist = ["GROUPOPENID"]
    plugin.official_sister_openid = "SISTEROPENID"
    plugin.enable_arbitrary_friend_targets = False
    plugin._official_sessions = {}
    plugin._load_group_whitelist = lambda: None
    return plugin


class ResolveTargetPlatformTests(unittest.TestCase):
    def test_default_platform_of_other_adapter_is_ignored(self):
        plugin = _make_plugin(default_platform="napcat")
        self.assertEqual(
            plugin._effective_target_platform_id(_FakeEvent("official", "qq_official")),
            "official",
        )

        plugin = _make_plugin(default_platform="official")
        self.assertEqual(
            plugin._effective_target_platform_id(_FakeEvent("napcat", "aiocqhttp")),
            "napcat",
        )

    def test_default_platform_of_same_adapter_is_kept(self):
        plugin = _make_plugin(default_platform="napcat2")
        self.assertEqual(
            plugin._effective_target_platform_id(_FakeEvent("napcat", "aiocqhttp")),
            "napcat2",
        )

    def test_unsupported_source_resolves_nothing(self):
        plugin = _make_plugin(default_platform="napcat")
        self.assertEqual(
            plugin._effective_target_platform_id(_FakeEvent("tg", "telegram")), ""
        )


class TargetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def test_group_whitelists_do_not_leak_between_adapters(self):
        self.assertIsNone(
            self.plugin._validate_target("GroupMessage", "GROUPOPENID", "official")
        )
        self.assertIsNotNone(
            self.plugin._validate_target("GroupMessage", "GROUPOPENID", "napcat")
        )
        self.assertIsNone(self.plugin._validate_target("GroupMessage", "123456", "napcat"))
        self.assertIsNotNone(
            self.plugin._validate_target("GroupMessage", "123456", "official")
        )

    def test_private_targets_follow_adapter_sister(self):
        self.assertIsNone(
            self.plugin._validate_target("FriendMessage", "SISTEROPENID", "official")
        )
        error = self.plugin._validate_target("FriendMessage", "10001", "official")
        self.assertIn("official_sister_openid", error)
        self.assertIsNone(self.plugin._validate_target("FriendMessage", "10001", "napcat"))

    def test_arbitrary_private_targets_do_not_need_sister(self):
        self.plugin.official_sister_openid = ""
        self.assertIsNotNone(
            self.plugin._validate_target("FriendMessage", "U1", "official")
        )
        self.plugin.enable_arbitrary_friend_targets = True
        self.assertIsNone(self.plugin._validate_target("FriendMessage", "U1", "official"))


class OfficialRawMessageTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()
        self.platform = _FakePlatform("official", "qq_official")

    def _message(self):
        return SimpleNamespace(message_id="gossip-task-x", message_str="任务", raw_message=None)

    def test_group_target_keeps_synthetic_id_for_proactive_fallback(self):
        message = self._message()
        self.plugin._attach_official_raw_message(
            self.platform, message, "GroupMessage", "G1", "REQ"
        )

        self.assertEqual(message.message_id, "gossip-task-x")
        self.assertIsInstance(message.raw_message, _FakeGroupMessage)
        self.assertEqual(message.raw_message.group_openid, "G1")
        self.assertEqual(message.raw_message.author.member_openid, "REQ")

    def test_private_target_is_sent_proactively(self):
        message = self._message()
        self.plugin._attach_official_raw_message(
            self.platform, message, "FriendMessage", "U1", "REQ"
        )

        self.assertIsNone(message.message_id)
        self.assertIsInstance(message.raw_message, _FakeC2CMessage)
        self.assertEqual(message.raw_message.author.user_openid, "U1")


class OfficialDeliveryTests(unittest.TestCase):
    @staticmethod
    def _deliver(post_send):
        async def run():
            event = SimpleNamespace(_post_send=post_send)
            delivery = GossipSharer._track_official_delivery(event)
            try:
                await event._post_send()
            except Exception:
                pass
            return await delivery

        return asyncio.run(run())

    def test_successful_send_resolves_without_error(self):
        async def post_send():
            return "ok"

        self.assertIsNone(self._deliver(post_send))

    def test_send_error_is_reported(self):
        async def post_send():
            raise RuntimeError("用户已关闭主动消息")

        self.assertEqual(str(self._deliver(post_send)), "用户已关闭主动消息")

if __name__ == "__main__":
    unittest.main()
