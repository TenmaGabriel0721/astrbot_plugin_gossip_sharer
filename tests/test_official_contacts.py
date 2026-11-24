import ast
import asyncio
import enum
import re
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
GROUP_A = "A1" * 16
GROUP_B = "A2" * 16
FRIEND_A = "B1" * 16
FRIEND_B = "B2" * 16


class _MessageType(enum.Enum):
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"
    OTHER_MESSAGE = "OtherMessage"


class _MessageSession:
    @staticmethod
    def from_str(umo: str):
        platform_id, message_type, session_id = umo.split(":", 2)
        return SimpleNamespace(
            platform_id=platform_id,
            message_type=_MessageType(message_type),
            session_id=session_id,
        )


method_names = {
    "_build_session_id",
    "_adapter_kind",
    "_platform_kind",
    "_event_kind",
    "_get_platform_by_id",
    "_target_policy",
    "_validate_target",
    "_official_session",
    "_sync_official_sessions",
    "_format_elapsed",
    "_format_official_groups",
    "_format_official_friends",
    "_unwrap_list_data",
    "_format_group_list",
    "get_groups",
    "get_friend_list",
}
source_tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
plugin_class = next(
    node
    for node in source_tree.body
    if isinstance(node, ast.ClassDef) and node.name == "GossipSharer"
)
methods = []
for node in plugin_class.body:
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in method_names
    ):
        node.decorator_list = [
            decorator
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Name)
            and decorator.id in {"staticmethod", "classmethod"}
        ]
        methods.append(node)
constants = [
    node
    for node in source_tree.body
    if isinstance(node, ast.Assign)
    and any(
        isinstance(target, ast.Name)
        and target.id in {"ONEBOT_ADAPTERS", "OFFICIAL_ADAPTERS"}
        for target in node.targets
    )
]
module = ast.fix_missing_locations(
    ast.Module(
        body=[
            *constants,
            ast.ClassDef(
                name="TestableGossipSharer",
                bases=[],
                keywords=[],
                decorator_list=[],
                body=methods,
            ),
        ],
        type_ignores=[],
    )
)
namespace = {
    "asyncio": asyncio,
    "re": re,
    "time": time,
    "AstrMessageEvent": object,
    "MessageType": _MessageType,
    "MessageSession": _MessageSession,
    "logger": Mock(),
}
exec(compile(module, str(PLUGIN_PATH), "exec"), namespace)
GossipSharer = namespace["TestableGossipSharer"]


def _conversation(
    target_id: str,
    message_type: str = "GroupMessage",
    *,
    platform_id: str = "official",
    umo_platform: str | None = None,
    updated_at: int = 100,
):
    return SimpleNamespace(
        platform_id=platform_id,
        user_id=f"{umo_platform or platform_id}:{message_type}:{target_id}",
        updated_at=updated_at,
        created_at=1,
        title="This dialogue title is not a contact name",
    )


class OfficialContactTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.filtered = AsyncMock(return_value=([], 0))
        self.aliases = AsyncMock(return_value=[])
        self.manager = SimpleNamespace(
            get_filtered_conversations=self.filtered,
            db=SimpleNamespace(get_umo_aliases=self.aliases),
        )
        self.plugin = GossipSharer.__new__(GossipSharer)
        self.plugin.context = SimpleNamespace(
            conversation_manager=self.manager,
            platform_manager=SimpleNamespace(
                platform_insts=[
                    SimpleNamespace(
                        meta=lambda: SimpleNamespace(id="official", name="qq_official")
                    ),
                    SimpleNamespace(
                        meta=lambda: SimpleNamespace(
                            id="official2", name="qq_official_webhook"
                        )
                    ),
                    SimpleNamespace(
                        meta=lambda: SimpleNamespace(id="napcat", name="aiocqhttp")
                    ),
                ]
            ),
        )
        self.plugin.default_platform = "napcat"
        self.plugin.group_whitelist = []
        self.plugin.sister_qq = "10001"
        self.plugin.official_group_whitelist = []
        self.plugin.official_sister_openid = ""
        self.plugin.sister_title = "姐姐"
        self.plugin.enable_arbitrary_friend_targets = False
        self.plugin._official_sessions = {}
        self.plugin._load_group_whitelist = Mock()
        self.plugin._unwrap_message_event = lambda event: event
        self.plugin._ensure_official_group_name = AsyncMock()
        self.plugin._try_get_group_list = AsyncMock(
            side_effect=AssertionError("The official bot must use known sessions.")
        )
        self.plugin._try_get_friend_list = AsyncMock(
            side_effect=AssertionError("The official bot must use known sessions.")
        )
        self.event = SimpleNamespace(
            get_platform_id=lambda: "official",
            get_platform_name=lambda: "qq_official",
            bot=object(),
        )

    async def test_saved_dialogues_restore_group_and_private_lists(self):
        group = _conversation(GROUP_A)
        friend = _conversation(FRIEND_A, "FriendMessage")
        self.filtered.return_value = ([group, friend], 2)
        self.aliases.return_value = [
            SimpleNamespace(
                umo=group.user_id, user_alias="群备注", auto_name="保存群名"
            ),
            SimpleNamespace(umo=friend.user_id, user_alias="", auto_name="保存昵称"),
        ]
        self.plugin.official_group_whitelist = [GROUP_A]
        self.plugin.official_sister_openid = FRIEND_A

        groups = await self.plugin.get_groups(self.event)
        friends = await self.plugin.get_friend_list(self.event)

        self.assertIsInstance(friends, str)
        self.assertIn(f"群备注 ({GROUP_A})", groups)
        self.assertIn("白名单可转发", groups)
        self.assertIn(f"保存昵称 ({FRIEND_A})", friends)
        self.assertIn("默认可转发", friends)
        self.assertIn("历史会话", groups)
        self.assertIn("历史会话", friends)
        self.assertEqual(groups.count(GROUP_A), 1)
        self.assertEqual(friends.count(FRIEND_A), 1)
        self.assertNotIn(group.title, groups + friends)
        for record in self.plugin._official_sessions.values():
            self.assertEqual(record["last_active"], 0)
        self.plugin._try_get_group_list.assert_not_awaited()
        self.plugin._try_get_friend_list.assert_not_awaited()

    async def test_member_isolation_and_multiple_dialogues_share_one_group_target(self):
        first_umo = f"{FRIEND_A}_{GROUP_A}"
        second_umo = f"{FRIEND_B}_{GROUP_A}"
        self.filtered.return_value = (
            [
                _conversation(first_umo, updated_at=10),
                _conversation(first_umo, updated_at=90),
                _conversation(second_umo, updated_at=20),
                _conversation(GROUP_A, updated_at=30),
            ],
            3,
        )

        result = await self.plugin.get_groups(self.event)

        self.assertEqual(result.count(GROUP_A), 1)
        self.assertEqual(len(self.plugin._official_sessions), 1)
        self.assertNotIn(first_umo, result)
        self.assertNotIn(second_umo, result)
        record = self.plugin._official_sessions[f"official:GroupMessage:{GROUP_A}"]
        self.assertEqual(record["history_updated"], 90)
        self.assertEqual(record["last_active"], 0)

    async def test_other_bot_instances_and_guild_scenes_are_excluded(self):
        rows = [
            _conversation(GROUP_A),
            _conversation(FRIEND_A, "FriendMessage"),
            _conversation(GROUP_B, platform_id="official2"),
            _conversation(GROUP_B, umo_platform="official2"),
            _conversation(GROUP_B, platform_id="napcat", umo_platform="official"),
            _conversation(GROUP_B, platform_id="napcat"),
            _conversation("123456789012345678"),
            _conversation("123456789012345678", "FriendMessage"),
            _conversation(GROUP_B, "OtherMessage"),
            _conversation(""),
            _conversation(f"{GROUP_B}:extra"),
        ]
        rows.append(SimpleNamespace(platform_id="official", user_id="malformed"))
        self.filtered.return_value = (rows, len(rows))

        await self.plugin._sync_official_sessions(self.event)

        self.assertEqual(
            set(self.plugin._official_sessions),
            {
                f"official:GroupMessage:{GROUP_A}",
                f"official:FriendMessage:{FRIEND_A}",
            },
        )

    async def test_pagination_uses_session_total_despite_duplicate_dialogues(self):
        group_ids = [f"A{index:031X}" for index in range(101)]
        first_page = [
            _conversation(group_id, updated_at=updated_at)
            for group_id in group_ids[:100]
            for updated_at in (10, 20)
        ]
        self.filtered.side_effect = [
            (first_page, 101),
            ([_conversation(group_ids[100], updated_at=30)], 101),
        ]

        await self.plugin._sync_official_sessions(self.event)

        self.assertEqual(len(self.plugin._official_sessions), 101)
        self.assertIn(
            f"official:GroupMessage:{group_ids[100]}", self.plugin._official_sessions
        )
        self.assertEqual(self.filtered.await_count, 2)
        for page, call in enumerate(self.filtered.await_args_list, start=1):
            self.assertEqual(call.kwargs["page"], page)
            self.assertEqual(call.kwargs["page_size"], 100)
            self.assertEqual(call.kwargs["platform_ids"], ["official"])
            self.assertFalse(call.kwargs["include_history"])
            self.assertTrue(call.kwargs["group_by_session"])

    async def test_unavailable_history_keeps_configured_and_runtime_targets(self):
        self.plugin.official_group_whitelist = [GROUP_A]
        self.plugin.official_sister_openid = FRIEND_A
        group = self.plugin._official_session("official", "GroupMessage", GROUP_B)
        group.update(name="运行期群", last_active=time.time())
        friend = self.plugin._official_session("official", "FriendMessage", FRIEND_B)
        friend.update(name="运行期用户", last_active=time.time())
        self.filtered.side_effect = RuntimeError("History database is unavailable.")

        for manager in (self.manager, None):
            with self.subTest(manager_available=manager is not None):
                self.plugin.context.conversation_manager = manager
                groups = await self.plugin.get_groups(self.event)
                friends = await self.plugin.get_friend_list(self.event)
                self.assertIn(GROUP_A, groups)
                self.assertIn(f"运行期群 ({GROUP_B})", groups)
                self.assertIn(FRIEND_A, friends)
                self.assertIn(f"运行期用户 ({FRIEND_B})", friends)
                self.assertNotIn("获取群列表失败", groups)
                self.assertNotIn("获取好友列表失败", friends)
        self.aliases.assert_not_awaited()

    async def test_legacy_manager_without_alias_api_still_restores_openids(self):
        rows = [_conversation(GROUP_A), _conversation(FRIEND_A, "FriendMessage")]
        legacy = AsyncMock(return_value=rows)
        self.plugin.context.conversation_manager = SimpleNamespace(
            get_conversations=legacy, db=SimpleNamespace()
        )

        groups = await self.plugin.get_groups(self.event)
        friends = await self.plugin.get_friend_list(self.event)

        self.assertIn(GROUP_A, groups)
        self.assertIn(FRIEND_A, friends)
        self.assertNotIn(rows[0].title, groups + friends)
        legacy.assert_awaited_with(platform_id="official")

    async def test_history_never_replaces_runtime_name_or_receive_timestamp(self):
        group = self.plugin._official_session("official", "GroupMessage", GROUP_A)
        group.update(name="当前群名", last_active=1234)
        friend = self.plugin._official_session("official", "FriendMessage", FRIEND_A)
        friend.update(name="当前昵称", last_active=1234)
        rows = [
            _conversation(GROUP_A, updated_at=9999),
            _conversation(FRIEND_A, "FriendMessage", updated_at=9999),
        ]
        self.filtered.return_value = (rows, 2)
        self.aliases.return_value = [
            SimpleNamespace(umo=row.user_id, user_alias="旧备注", auto_name="旧名称")
            for row in rows
        ]

        groups = await self.plugin.get_groups(self.event)
        friends = await self.plugin.get_friend_list(self.event)

        self.assertIn(f"当前群名 ({GROUP_A})", groups)
        self.assertIn(f"当前昵称 ({FRIEND_A})", friends)
        self.assertNotIn("旧备注", groups + friends)
        self.assertEqual(group["last_active"], 1234)
        self.assertEqual(friend["last_active"], 1234)
        self.assertEqual(group["history_updated"], 9999)
        self.assertEqual(friend["history_updated"], 9999)

    async def test_discovery_does_not_expand_sending_permissions(self):
        self.plugin.official_group_whitelist = [GROUP_A]
        self.plugin.official_sister_openid = FRIEND_A
        rows = [
            _conversation(GROUP_A),
            _conversation(GROUP_B),
            _conversation(FRIEND_A, "FriendMessage"),
            _conversation(FRIEND_B, "FriendMessage"),
        ]
        self.filtered.return_value = (rows, 4)

        await self.plugin._sync_official_sessions(self.event)

        self.assertEqual(len(self.plugin._official_sessions), 4)
        self.assertIsNone(
            self.plugin._validate_target("GroupMessage", GROUP_A, "official")
        )
        self.assertIsNotNone(
            self.plugin._validate_target("GroupMessage", GROUP_B, "official")
        )
        self.assertIsNone(
            self.plugin._validate_target("FriendMessage", FRIEND_A, "official")
        )
        self.assertIsNotNone(
            self.plugin._validate_target("FriendMessage", FRIEND_B, "official")
        )
        self.assertEqual(self.plugin.official_group_whitelist, [GROUP_A])
        self.assertEqual(self.plugin.official_sister_openid, FRIEND_A)
        self.plugin.enable_arbitrary_friend_targets = True
        self.assertIsNone(
            self.plugin._validate_target("FriendMessage", FRIEND_B, "official")
        )

    async def test_empty_official_group_list_marks_all_known_groups_allowed(self):
        self.filtered.return_value = (
            [_conversation(GROUP_A), _conversation(GROUP_B)],
            2,
        )

        result = await self.plugin.get_groups(self.event)

        self.assertIn("群白名单为空，允许所有群", result)
        for group_id in (GROUP_A, GROUP_B):
            self.assertIn(f"({group_id}) [可转发]", result)
            self.assertIsNone(
                self.plugin._validate_target("GroupMessage", group_id, "official")
            )
        self.assertEqual(self.plugin.official_group_whitelist, [])

    async def test_nonempty_official_group_list_only_marks_members_allowed(self):
        self.plugin.official_group_whitelist = [GROUP_A]
        self.filtered.return_value = (
            [_conversation(GROUP_A), _conversation(GROUP_B)],
            2,
        )

        result = await self.plugin.get_groups(self.event)

        self.assertIn(f"({GROUP_A}) [白名单可转发]", result)
        blocked_line = next(
            line for line in result.splitlines() if f"({GROUP_B})" in line
        )
        self.assertNotIn("[可转发]", blocked_line)
        self.assertNotIn("[白名单可转发]", blocked_line)
        self.assertNotIn("群白名单为空，允许所有群", result)

    async def test_onebot_list_marks_groups_according_to_its_whitelist(self):
        self.plugin._try_get_group_list = AsyncMock(
            return_value=[
                {"group_id": "123456", "group_name": "群甲"},
                {"group_id": "789012", "group_name": "群乙"},
            ]
        )
        event = SimpleNamespace(
            get_platform_id=lambda: "napcat",
            get_platform_name=lambda: "aiocqhttp",
        )

        unrestricted = await self.plugin.get_groups(event)

        self.assertIn("群白名单为空，允许所有群", unrestricted)
        self.assertIn("群甲 (123456) [可转发]", unrestricted)
        self.assertIn("群乙 (789012) [可转发]", unrestricted)

        self.plugin.group_whitelist = ["123456"]
        restricted = await self.plugin.get_groups(event)

        self.assertIn("群甲 (123456) [白名单可转发]", restricted)
        blocked_line = next(
            line for line in restricted.splitlines() if "(789012)" in line
        )
        self.assertNotIn("[可转发]", blocked_line)
        self.assertNotIn("[白名单可转发]", blocked_line)
        self.assertNotIn("群白名单为空，允许所有群", restricted)
        self.filtered.assert_not_awaited()

    async def test_onebot_missing_list_interface_still_reports_unrestricted_policy(
        self,
    ):
        self.plugin._try_get_group_list = AsyncMock(return_value=None)
        event = SimpleNamespace(
            get_platform_id=lambda: "napcat",
            get_platform_name=lambda: "aiocqhttp",
        )

        result = await self.plugin.get_groups(event)

        self.assertIn("群白名单为空，允许所有群", result)
        self.assertNotIn("获取群列表失败", result)

    async def test_alias_lookup_failure_keeps_discovered_targets(self):
        self.filtered.return_value = (
            [_conversation(GROUP_A), _conversation(FRIEND_A, "FriendMessage")],
            2,
        )
        self.aliases.side_effect = RuntimeError("Alias database is unavailable.")

        groups = await self.plugin.get_groups(self.event)
        friends = await self.plugin.get_friend_list(self.event)

        self.assertIn(GROUP_A, groups)
        self.assertIn(FRIEND_A, friends)
        self.assertNotIn("获取群列表失败", groups)
        self.assertNotIn("获取好友列表失败", friends)


if __name__ == "__main__":
    unittest.main()
