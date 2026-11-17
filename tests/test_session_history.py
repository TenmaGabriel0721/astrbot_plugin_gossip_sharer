import ast
import asyncio
import base64
import copy
import io
import json
import mimetypes
import re
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

from PIL import Image as PILImage

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
METHOD_NAMES = {
    "_unwrap_message_event",
    "_build_session_id",
    "_adapter_kind",
    "_platform_kind",
    "_event_kind",
    "_target_policy",
    "_validate_target",
    "_normalize_target_type_name",
    "_normalize_string_list",
    "_get_platform_by_id",
    "_call_platform_action",
    "_unwrap_onebot_action_payload",
    "_normalize_onebot_content",
    "_onebot_content_preview",
    "_ensure_attachment_registry",
    "_normalize_attachment_refs",
    "_prepare_image_for_llm",
    "_allowed_attachment_paths",
    "_validate_attachment_path",
    "_find_attachment_entry",
    "_resolve_wake_image_ref",
    "_wake_prepare_issue",
    "_prepare_wake_attachments",
    "_cleanup_wake_snapshots",
    "get_qq_session_messages",
}


class _Logger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class _Image:
    def __init__(self, file: str, **kwargs):
        self.file = file
        self.url = kwargs.get("url", "")
        self.path = kwargs.get("path", "")

    @classmethod
    def fromURL(cls, url: str):
        return cls(file=url)

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls(file=Path(path).as_uri(), path=path)

    async def convert_to_base64(self):
        if self.file.startswith("base64://"):
            return self.file.removeprefix("base64://")
        raise ValueError("Remote downloads are disabled in this test")


class _File:
    def __init__(self, name: str, file: str = "", url: str = ""):
        self.name = name
        self.file_ = file
        self.url = url

    async def get_file(self):
        return self.file_


class _Reply:
    chain = []


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
        and node.name in METHOD_NAMES
    ):
        node = copy.deepcopy(node)
        if node.name == "get_qq_session_messages":
            node.decorator_list = []
        methods.append(node)
test_class = ast.ClassDef(
    name="TestableGossipSharer",
    bases=[],
    keywords=[],
    decorator_list=[],
    body=methods,
)
module = ast.fix_missing_locations(ast.Module(body=[test_class], type_ignores=[]))
namespace = {
    "AstrMessageEvent": object,
    "Path": Path,
    "Image": _Image,
    "File": _File,
    "Reply": _Reply,
    "PILImage": PILImage,
    "base64": base64,
    "io": io,
    "json": json,
    "copy": copy,
    "mimetypes": mimetypes,
    "re": re,
    "shutil": shutil,
    "uuid": uuid,
    "urlparse": urlparse,
    "logger": _Logger(),
    "ONEBOT_ADAPTERS": {"aiocqhttp"},
    "OFFICIAL_ADAPTERS": {"qq_official", "qq_official_webhook"},
    "ATTACHMENT_REGISTRY_EXTRA": "gossip_sharer_attachment_registry",
    "is_file_uri": lambda value: value.startswith("file://"),
    "file_uri_to_path": lambda value: value.removeprefix("file://"),
}
exec(compile(module, str(PLUGIN_PATH), "exec"), namespace)
GossipSharer = namespace["TestableGossipSharer"]


class _Event:
    def __init__(
        self,
        group_id="860354493",
        sender_id="10001",
        platform_id="napcat",
        adapter="aiocqhttp",
    ):
        self.group_id = group_id
        self.sender_id = sender_id
        self.platform_id = platform_id
        self.adapter = adapter
        self.message_obj = SimpleNamespace(self_id="99999")
        self.extras = {}
        self.messages = []
        self.unified_msg_origin = f"{platform_id}:{'GroupMessage' if group_id else 'FriendMessage'}:{group_id or sender_id}"

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id

    def get_platform_id(self):
        return self.platform_id

    def get_platform_name(self):
        return self.adapter

    def get_messages(self):
        return self.messages

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class SessionHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="codex.gossip_history.")
        self.addCleanup(self.temp_dir.cleanup)
        namespace["get_astrbot_temp_path"] = lambda: self.temp_dir.name
        namespace["get_astrbot_workspaces_path"] = lambda: self.temp_dir.name
        self.responses = {
            "get_group_msg_history": {"messages": []},
            "get_friend_msg_history": {"messages": []},
        }
        self.bot = SimpleNamespace(call_action=AsyncMock(side_effect=self._respond))
        self.platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="napcat", name="aiocqhttp"), bot=self.bot
        )
        self.other_platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="napcat2", name="aiocqhttp"), bot=self.bot
        )
        self.official_platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="official", name="qq_official"),
            bot=self.bot,
        )
        self.plugin = GossipSharer.__new__(GossipSharer)
        self.plugin.context = SimpleNamespace(
            platform_manager=SimpleNamespace(
                platform_insts=[
                    self.platform,
                    self.other_platform,
                    self.official_platform,
                ]
            )
        )
        self.plugin.default_platform = "napcat2"
        self.plugin.sister_qq = "10001"
        self.plugin.group_whitelist = ["860354493", "123456"]
        self.plugin.official_group_whitelist = []
        self.plugin.official_sister_openid = ""
        self.plugin.enable_arbitrary_friend_targets = False
        self.plugin._load_group_whitelist = lambda: None
        self.plugin.enable_wake_images = True
        self.plugin.enable_wake_files = True
        self.plugin.max_wake_images = 4
        self.plugin.max_wake_files = 3
        self.plugin.max_wake_image_mb = 15
        self.plugin.max_wake_file_mb = 50
        self.plugin.max_wake_total_mb = 100
        self.plugin.allow_remote_attachment_urls = True
        self.plugin.attachment_allowed_roots = []
        self.event = _Event()

    async def _respond(self, action, **_kwargs):
        value = self.responses.get(action)
        if isinstance(value, Exception):
            raise value
        return value

    @staticmethod
    def _message(
        message_id="101",
        *,
        group_id="860354493",
        sender_id="20002",
        time=1794900000,
        segments=None,
    ):
        message = {
            "message_id": message_id,
            "time": time,
            "sender": {"user_id": sender_id, "nickname": "锦亚澄"},
            "message": segments
            or [
                {
                    "type": "image",
                    "data": {
                        "file": "original.image",
                        "url": "https://qq.example/original.jpg",
                    },
                }
            ],
        }
        if group_id:
            message["group_id"] = group_id
        return message

    def _registry(self, event=None):
        return (event or self.event).get_extra(
            namespace["ATTACHMENT_REGISTRY_EXTRA"], {}
        )

    async def test_current_group_reads_actual_platform_and_original_image(self):
        self.responses["get_group_msg_history"] = {"messages": [self._message()]}

        result = await self.plugin.get_qq_session_messages(self.event)

        self.bot.call_action.assert_awaited_once_with(
            "get_group_msg_history",
            group_id=860354493,
            count=20,
            reverseOrder=True,
            self_id="99999",
        )
        entry = self._registry()["image_1"]
        self.assertEqual(entry["component"].file, "https://qq.example/original.jpg")
        self.assertEqual(entry["source_session"], "napcat:GroupMessage:860354493")
        self.assertEqual(entry["source_message_id"], "101")
        self.assertIn("锦亚澄(20002)", result)
        self.assertIn("image_1", result)
        self.assertNotIn("media_image_", result)

    async def test_private_current_target_is_session_peer_not_original_requester(self):
        event = _Event(group_id="", sender_id="30003")
        self.responses["get_friend_msg_history"] = {
            "messages": [self._message(group_id="", sender_id="30003")]
        }

        result = await self.plugin.get_qq_session_messages(event)

        self.bot.call_action.assert_awaited_once_with(
            "get_friend_msg_history",
            user_id=30003,
            count=20,
            reverseOrder=True,
            self_id="99999",
        )
        self.assertIn("napcat:FriendMessage:30003", result)

    async def test_explicit_private_to_group_switch_and_count_clamp(self):
        event = _Event(group_id="")
        self.responses["get_group_msg_history"] = {
            "messages": [self._message(group_id="123456")]
        }

        result = await self.plugin.get_qq_session_messages(
            event, target_id="123456", target_type="GroupMessage", limit=200
        )

        self.assertIn("napcat:GroupMessage:123456", result)
        self.assertEqual(self.bot.call_action.await_args.kwargs["count"], 50)

    async def test_explicit_targets_obey_whitelist_and_adapter_scope(self):
        result = await self.plugin.get_qq_session_messages(
            self.event, target_id="456789", target_type="GroupMessage"
        )
        self.assertIn("白名单", result)
        result = await self.plugin.get_qq_session_messages(
            self.event, target_platform="official"
        )
        self.assertIn("目标平台必须", result)
        self.bot.call_action.assert_not_awaited()

    async def test_official_platform_reports_no_history_support(self):
        result = await self.plugin.get_qq_session_messages(
            _Event(platform_id="official", adapter="qq_official")
        )
        self.assertIn("QQ 官方机器人不支持", result)
        self.bot.call_action.assert_not_awaited()

    async def test_duplicate_reads_reuse_refs_and_preserve_current_attachment(self):
        self.event.messages = [_Image("base64://Y3VycmVudA==")]
        self.responses["get_group_msg_history"] = {"messages": [self._message()]}

        await self.plugin.get_qq_session_messages(self.event)
        result = await self.plugin.get_qq_session_messages(self.event)

        self.assertEqual(set(self._registry()), {"image_1", "image_2"})
        self.assertEqual(self._registry()["image_2"]["source_message_id"], "101")
        self.assertIn("附件: image_2", result)

    async def test_same_message_id_from_different_session_gets_separate_ref(self):
        self.responses["get_group_msg_history"] = {"messages": [self._message()]}
        await self.plugin.get_qq_session_messages(self.event)
        self.responses["get_group_msg_history"] = {
            "messages": [self._message(group_id="123456")]
        }

        await self.plugin.get_qq_session_messages(
            self.event, target_id="123456", target_type="group"
        )

        self.assertEqual(set(self._registry()), {"image_1", "image_2"})

    async def test_group_file_url_is_resolved_from_file_id(self):
        segments = [
            {
                "type": "file",
                "data": {"file_id": "FILE", "file": "report.pdf", "name": "report.pdf"},
            }
        ]
        self.responses["get_group_msg_history"] = {
            "messages": [self._message(segments=segments)]
        }
        self.responses["get_group_file_url"] = {"url": "https://qq.example/report.pdf"}

        result = await self.plugin.get_qq_session_messages(self.event)

        self.bot.call_action.assert_any_await(
            "get_group_file_url", file_id="FILE", group_id=860354493, self_id="99999"
        )
        self.assertEqual(
            self._registry()["file_1"]["component"].url, "https://qq.example/report.pdf"
        )
        self.assertIn("file_1", result)

    async def test_private_file_url_is_resolved_without_group_route(self):
        event = _Event(group_id="")
        segments = [{"type": "file", "data": {"file_id": "FILE", "name": "report.pdf"}}]
        self.responses["get_friend_msg_history"] = {
            "messages": [self._message(group_id="", segments=segments)]
        }
        self.responses["get_private_file_url"] = {
            "data": {"url": "https://qq.example/report.pdf"}
        }

        await self.plugin.get_qq_session_messages(event)

        self.bot.call_action.assert_any_await(
            "get_private_file_url", file_id="FILE", self_id="99999"
        )
        self.assertEqual(self._registry(event)["file_1"]["name"], "report.pdf")

    async def test_anchor_reads_exact_old_message_and_filters_newer_images(self):
        anchor = self._message(
            "80",
            time=100,
            segments=[
                {"type": "image", "data": {"url": "https://qq.example/wanted.jpg"}}
            ],
        )
        anchor["message_seq"] = 8000
        self.responses["get_msg"] = anchor
        self.responses["get_group_msg_history"] = {
            "messages": [self._message("81", time=200)]
        }

        result = await self.plugin.get_qq_session_messages(
            self.event, before_message_id="80"
        )

        self.bot.call_action.assert_any_await(
            "get_group_msg_history",
            group_id=860354493,
            count=20,
            message_seq=8000,
            reverseOrder=True,
            self_id="99999",
        )
        self.assertEqual(
            self._registry()["image_1"]["component"].file,
            "https://qq.example/wanted.jpg",
        )
        self.assertNotIn("消息 81", result)

    async def test_anchor_failure_never_substitutes_recent_image(self):
        self.responses["get_msg"] = None
        self.responses["get_group_msg_history"] = {"messages": [self._message()]}

        result = await self.plugin.get_qq_session_messages(
            self.event, before_message_id="80"
        )

        self.assertIn("未改用无关", result)
        self.assertFalse(self._registry())
        self.assertEqual(self.bot.call_action.await_count, 1)

    async def test_private_anchor_retrieves_earlier_messages(self):
        event = _Event(group_id="")
        anchor = self._message("80", group_id="", sender_id="10001", time=200)
        self.responses["get_msg"] = anchor
        self.responses["get_friend_msg_history"] = {
            "messages": [
                self._message(
                    "70",
                    group_id="",
                    sender_id="10001",
                    time=100,
                    segments=[
                        {
                            "type": "image",
                            "data": {"url": "https://qq.example/older.jpg"},
                        }
                    ],
                )
            ]
        }

        result = await self.plugin.get_qq_session_messages(
            event, before_message_id="80"
        )

        self.bot.call_action.assert_any_await(
            "get_friend_msg_history",
            user_id=10001,
            count=20,
            message_seq=80,
            reverseOrder=True,
            self_id="99999",
        )
        self.assertIn("消息 70", result)
        self.assertIn("消息 80", result)
        self.assertEqual(
            self._registry(event)["image_1"]["component"].file,
            "https://qq.example/older.jpg",
        )

    async def test_anchor_from_another_session_is_rejected(self):
        self.responses["get_msg"] = self._message("80", group_id="123456")

        result = await self.plugin.get_qq_session_messages(
            self.event, before_message_id="80"
        )

        self.assertIn("不属于目标会话", result)
        self.assertFalse(self._registry())

    async def test_anchor_still_returns_original_when_history_pagination_fails(self):
        self.responses["get_msg"] = self._message("80")
        self.responses["get_group_msg_history"] = RuntimeError("unsupported")

        result = await self.plugin.get_qq_session_messages(
            self.event, before_message_id="80"
        )

        self.assertIn("仅返回已验证", result)
        self.assertEqual(self._registry()["image_1"]["source_message_id"], "80")

    async def test_json_message_and_string_segment_data_preserve_original_url(self):
        message = self._message()
        message["message"] = json.dumps(
            [
                {
                    "type": "image",
                    "data": json.dumps(
                        {
                            "url": "https://qq.example/full.gif",
                            "thumb_url": "https://qq.example/thumb.png",
                        }
                    ),
                }
            ]
        )
        self.responses["get_group_msg_history"] = {"messages": [message]}

        await self.plugin.get_qq_session_messages(self.event)

        self.assertEqual(
            self._registry()["image_1"]["component"].file, "https://qq.example/full.gif"
        )

    async def test_missing_file_url_reports_failure_without_fabricating_attachment(
        self,
    ):
        segments = [
            {"type": "file", "data": {"file_id": "FILE", "file": "missing.pdf"}}
        ]
        self.responses["get_group_msg_history"] = {
            "messages": [self._message(segments=segments)]
        }

        result = await self.plugin.get_qq_session_messages(self.event)

        self.assertIn("附件读取失败", result)
        self.assertFalse(self._registry())

    async def test_history_api_failure_reports_no_original_attachment(self):
        self.responses["get_group_msg_history"] = RuntimeError("disconnected")

        result = await self.plugin.get_qq_session_messages(self.event)

        self.assertIn("没有可用原图或文件引用", result)
        self.assertFalse(self._registry())

    async def test_serialized_single_message_segment_is_read_as_attachment(self):
        message = self._message()
        message["message"] = json.dumps(
            {"type": "image", "data": {"url": "https://qq.example/full.jpg"}}
        )
        self.responses["get_group_msg_history"] = {"messages": [message]}

        await self.plugin.get_qq_session_messages(self.event)

        self.assertEqual(
            self._registry()["image_1"]["component"].file,
            "https://qq.example/full.jpg",
        )

    async def test_bot_internal_task_is_not_exposed_as_real_history(self):
        internal = self._message(
            "102",
            sender_id="99999",
            segments=[{"type": "text", "data": {"text": "[跨会话行动] 内部任务"}}],
        )
        self.responses["get_group_msg_history"] = {
            "messages": [internal, self._message()]
        }

        result = await self.plugin.get_qq_session_messages(self.event)

        self.assertNotIn("内部任务", result)
        self.assertEqual(set(self._registry()), {"image_1"})

    async def test_history_gif_ref_can_be_prepared_as_original_and_preview(self):
        raw = io.BytesIO()
        PILImage.new("RGB", (2, 2), color="red").save(raw, format="GIF")
        encoded = base64.b64encode(raw.getvalue()).decode()
        segments = [
            {
                "type": "image",
                "data": {"file": f"base64://{encoded}", "name": "original.gif"},
            }
        ]
        self.responses["get_group_msg_history"] = {
            "messages": [self._message(segments=segments)]
        }
        await self.plugin.get_qq_session_messages(self.event)

        prepared = await self.plugin._prepare_wake_attachments(
            self.event, ["image_1"], []
        )

        self.assertFalse(prepared["fatal_failures"])
        self.assertEqual(
            base64.b64decode(prepared["images"][0]["base64"]), raw.getvalue()
        )
        self.assertTrue(
            base64.b64decode(prepared["images"][0]["llm_base64"]).startswith(b"\x89PNG")
        )

    async def test_remote_napcat_image_path_uses_original_base64(self):
        original = io.BytesIO()
        PILImage.new("RGB", (2, 2), color="blue").save(original, format="GIF")
        encoded = base64.b64encode(original.getvalue()).decode()
        remote_path = "/remote-napcat-only/original.gif"
        self.responses["get_group_msg_history"] = {
            "messages": [
                self._message(
                    segments=[
                        {
                            "type": "image",
                            "data": {"file": "original.gif", "url": remote_path},
                        }
                    ]
                )
            ]
        }
        self.responses["get_image"] = {
            "file": remote_path,
            "url": remote_path,
            "base64": encoded,
        }

        result = await self.plugin.get_qq_session_messages(self.event)
        prepared = await self.plugin._prepare_wake_attachments(
            self.event, ["image_1"], []
        )

        self.assertEqual(
            self._registry()["image_1"]["component"].file, f"base64://{encoded}"
        )
        self.assertEqual(
            self._registry()["image_1"]["source_session"],
            "napcat:GroupMessage:860354493",
        )
        self.assertFalse(prepared["fatal_failures"])
        self.assertEqual(
            base64.b64decode(prepared["images"][0]["base64"]), original.getvalue()
        )
        self.assertTrue(prepared["images"][0]["is_gif"])
        self.assertNotIn(remote_path, result)

    async def test_concurrent_different_origins_never_reuse_a_file_ref(self):
        gate = asyncio.Event()
        pending_calls = 0

        async def response(action, **kwargs):
            nonlocal pending_calls
            if action == "get_group_msg_history":
                group_id = str(kwargs["group_id"])
                return {
                    "messages": [
                        self._message(
                            message_id=group_id,
                            group_id=group_id,
                            segments=[
                                {
                                    "type": "file",
                                    "data": {
                                        "file_id": group_id,
                                        "file": f"{group_id}.pdf",
                                    },
                                }
                            ],
                        )
                    ]
                }
            pending_calls += 1
            if pending_calls == 2:
                gate.set()
            await gate.wait()
            return {"url": f"https://qq.example/{kwargs['file_id']}.pdf"}

        self.bot.call_action = AsyncMock(side_effect=response)
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(
                self.plugin.get_qq_session_messages(self.event),
                self.plugin.get_qq_session_messages(
                    self.event, target_id="123456", target_type="GroupMessage"
                ),
            ),
            timeout=2,
        )

        refs_by_session = {
            entry["source_session"]: ref for ref, entry in self._registry().items()
        }
        first_ref = refs_by_session["napcat:GroupMessage:860354493"]
        second_ref = refs_by_session["napcat:GroupMessage:123456"]
        self.assertNotEqual(first_ref, second_ref)
        self.assertIn(f"附件: {first_ref}", first_result)
        self.assertIn(f"附件: {second_ref}", second_result)
        self.assertEqual(len(self._registry()), 2)

    async def test_concurrent_duplicate_reads_keep_one_stable_file_ref(self):
        gate = asyncio.Event()
        pending_calls = 0
        file_message = self._message(
            segments=[
                {
                    "type": "file",
                    "data": {"file_id": "FILE", "file": "report.pdf"},
                }
            ]
        )

        async def response(action, **_kwargs):
            nonlocal pending_calls
            if action == "get_group_msg_history":
                return {"messages": [file_message]}
            pending_calls += 1
            if pending_calls == 2:
                gate.set()
            await gate.wait()
            return {"url": "https://qq.example/download"}

        self.bot.call_action = AsyncMock(side_effect=response)
        results = await asyncio.wait_for(
            asyncio.gather(
                self.plugin.get_qq_session_messages(self.event),
                self.plugin.get_qq_session_messages(self.event),
            ),
            timeout=2,
        )

        self.assertEqual(set(self._registry()), {"file_1"})
        self.assertTrue(all("附件: file_1" in result for result in results))

    async def test_native_napcat_file_name_and_mime_survive_url_lookup(self):
        original_path = Path(self.temp_dir.name) / "report.pdf"
        original_path.write_bytes(b"original-report")
        self.responses["get_group_msg_history"] = {
            "messages": [
                self._message(
                    segments=[
                        {
                            "type": "file",
                            "data": {"file_id": "FILE", "file": "report.pdf"},
                        }
                    ]
                )
            ]
        }
        self.responses["get_group_file_url"] = {
            "url": "https://qq.example/download?token=example"
        }

        await self.plugin.get_qq_session_messages(self.event)
        component = self._registry()["file_1"]["component"]
        component.file_ = str(original_path)
        prepared = await self.plugin._prepare_wake_attachments(
            self.event, [], ["file_1"]
        )

        self.assertEqual(component.name, "report.pdf")
        self.assertFalse(prepared["fatal_failures"])
        self.assertEqual(prepared["files"][0]["name"], "report.pdf")
        self.assertEqual(prepared["files"][0]["mime_type"], "application/pdf")
        self.plugin._cleanup_wake_snapshots(prepared)

    async def test_history_file_ref_can_be_prepared_as_independent_snapshot(self):
        original_path = Path(self.temp_dir.name) / "original.pdf"
        original_path.write_bytes(b"original-file")
        segments = [
            {
                "type": "file",
                "data": {
                    "url": "https://qq.example/original.pdf",
                    "name": "original.pdf",
                },
            }
        ]
        self.responses["get_group_msg_history"] = {
            "messages": [self._message(segments=segments)]
        }
        await self.plugin.get_qq_session_messages(self.event)
        component = self._registry()["file_1"]["component"]
        component.file_ = str(original_path)

        prepared = await self.plugin._prepare_wake_attachments(
            self.event, [], ["file_1"]
        )

        self.assertFalse(prepared["fatal_failures"])
        snapshot = Path(prepared["files"][0]["path"])
        self.assertNotEqual(snapshot, original_path)
        self.assertEqual(snapshot.read_bytes(), b"original-file")
        self.plugin._cleanup_wake_snapshots(prepared)
        self.assertTrue(original_path.exists())

    async def test_original_gif_as_file_keeps_original_bytes_and_source(self):
        original = io.BytesIO()
        PILImage.new("RGB", (2, 2), color="green").save(original, format="GIF")
        original_bytes = original.getvalue()
        encoded = base64.b64encode(original_bytes).decode()
        preview = io.BytesIO()
        PILImage.new("RGB", (2, 2), color="green").save(preview, format="PNG")
        source_path = Path(self.temp_dir.name) / "source-original.gif"
        source_path.write_bytes(original_bytes)
        self.event.messages = [_Image.fromFileSystem(str(source_path))]
        registry = self.plugin._ensure_attachment_registry(self.event)
        registry["image_1"].update(
            name="original",
            snapshot_base64=encoded,
            llm_snapshot_base64=base64.b64encode(preview.getvalue()).decode(),
        )

        prepared = await self.plugin._prepare_wake_attachments(
            self.event, [], ["image_1"]
        )

        self.assertFalse(prepared["fatal_failures"])
        self.assertFalse(prepared["images"])
        self.assertEqual(len(prepared["files"]), 1)
        file_info = prepared["files"][0]
        snapshot = Path(file_info["path"])
        self.assertNotEqual(snapshot, source_path)
        self.assertEqual(snapshot.read_bytes(), original_bytes)
        self.assertEqual(file_info["name"], "original.gif")
        self.assertEqual(file_info["mime_type"], "image/gif")
        self.assertEqual(file_info["size"], len(original_bytes))
        self.plugin._cleanup_wake_snapshots(prepared)
        self.assertFalse(snapshot.exists())
        self.assertEqual(source_path.read_bytes(), original_bytes)
        self.assertEqual(registry["image_1"]["snapshot_base64"], encoded)


if __name__ == "__main__":
    unittest.main()
