import ast
import asyncio
import base64
import copy
import enum
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


class _MessageType(enum.Enum):
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"


class _Image:
    def __init__(self, file="", **kwargs):
        self.file = file
        self.path = kwargs.get("path")
        self.url = kwargs.get("url")

    @classmethod
    def fromBase64(cls, encoded):
        return cls(file=f"base64://{encoded}")

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=f"file://{path}", path=path)

    async def convert_to_base64(self):
        if self.file.startswith("base64://"):
            return self.file.removeprefix("base64://")
        raise AssertionError("The offline test must not download an image.")


class _File:
    def __init__(self, name="", file="", **kwargs):
        self.name = name
        self.file_ = file
        self.url = kwargs.get("url")


class _Plain:
    def __init__(self, text):
        self.text = text


class _Reply:
    chain = None


class _Chain:
    def __init__(self, chain=None, type=None):
        self.chain = list(chain or [])
        self.type = type

    def message(self, text):
        self.chain.append(_Plain(text))
        return self

    def base64_image(self, encoded):
        self.chain.append(_Image.fromBase64(encoded))
        return self

    def get_plain_text(self, with_other_comps_mark=False):
        return "".join(
            component.text for component in self.chain if isinstance(component, _Plain)
        )


class _TextPart:
    def __init__(self, text):
        self.text = text

    def mark_as_temp(self):
        return self


class _Event:
    def __init__(
        self,
        platform_id="napcat",
        adapter="aiocqhttp",
        target_type="FriendMessage",
        target_id="10001",
        sender_id="10001",
    ):
        self.platform_meta = SimpleNamespace(id=platform_id, name=adapter)
        self.unified_msg_origin = f"{platform_id}:{target_type}:{target_id}"
        self.session_id = target_id
        self._extras = {}
        self._result = None
        self.message_obj = SimpleNamespace(
            type=_MessageType(target_type),
            group_id=target_id if target_type == "GroupMessage" else "",
            sender=SimpleNamespace(user_id=sender_id, nickname="requester"),
            message=[_Plain("请把原图发给我")],
            message_str="请把原图发给我",
            raw_message=None,
            timestamp=1,
        )
        self.send = AsyncMock()

    def get_platform_id(self):
        return self.platform_meta.id

    def get_platform_name(self):
        return self.platform_meta.name

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname

    def get_group_id(self):
        return self.message_obj.group_id

    def get_message_str(self):
        return self.message_obj.message_str

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_result(self):
        return self._result

    def set_result(self, value):
        self._result = value

    def clear_result(self):
        self._result = None


class _Platform:
    def __init__(self, platform_id, adapter):
        self._meta = SimpleNamespace(id=platform_id, name=adapter)
        self.committed = []

    def meta(self):
        return self._meta

    def create_event(self, message):
        event = _Event(
            platform_id=self._meta.id,
            adapter=self._meta.name,
            target_type=message.type.value,
            target_id=message.session_id,
            sender_id=message.sender.user_id,
        )
        event.message_obj = message
        return event

    def commit_event(self, event):
        self.committed.append(event)


source_tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
plugin_class = next(
    node
    for node in source_tree.body
    if isinstance(node, ast.ClassDef) and node.name == "GossipSharer"
)
test_class = copy.deepcopy(plugin_class)
test_class.name = "TestableGossipSharer"
test_class.bases = []
test_class.decorator_list = []
for method in test_class.body:
    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
        method.decorator_list = [
            decorator
            for decorator in method.decorator_list
            if isinstance(decorator, ast.Name)
            and decorator.id in {"staticmethod", "classmethod"}
        ]
imports = [
    node
    for node in source_tree.body
    if isinstance(node, ast.Import)
    or (
        isinstance(node, ast.ImportFrom)
        and not (node.module or "").startswith(("astrbot", "PIL"))
    )
]
constants = [node for node in source_tree.body if isinstance(node, ast.Assign)]
module = ast.fix_missing_locations(
    ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            *imports,
            *constants,
            test_class,
        ],
        type_ignores=[],
    )
)
namespace = {
    "logger": Mock(),
    "AstrMessageEvent": _Event,
    "AstrBotMessage": SimpleNamespace,
    "Group": SimpleNamespace,
    "MessageMember": SimpleNamespace,
    "MessageType": _MessageType,
    "MessageChain": _Chain,
    "Image": _Image,
    "File": _File,
    "Plain": _Plain,
    "Reply": _Reply,
    "Forward": type("Forward", (), {}),
    "Node": type("Node", (), {}),
    "Nodes": type("Nodes", (), {}),
    "At": SimpleNamespace,
    "TextPart": _TextPart,
    "is_file_uri": lambda value: value.startswith("file://"),
    "file_uri_to_path": lambda value: value.removeprefix("file://"),
}
exec(compile(module, str(PLUGIN_PATH), "exec"), namespace)
GossipSharer = namespace["TestableGossipSharer"]
DELEGATED_TASK_EXTRA = namespace["DELEGATED_TASK_EXTRA"]
ATTACHMENT_REGISTRY_EXTRA = namespace["ATTACHMENT_REGISTRY_EXTRA"]
SYNTHETIC_EVENT_EXTRA = namespace["SYNTHETIC_EVENT_EXTRA"]


def _prepared(images=None, files=None, failures=None):
    return {
        "images": list(images or []),
        "files": list(files or []),
        "forwards": [],
        "cleanup_paths": [],
        "fatal_failures": list(failures or []),
        "warnings": [],
    }


def _image(encoded="b3JpZ2luYWw=", preview=None, is_gif=False):
    return {
        "ref": "image_1",
        "registry_id": "image_1",
        "name": "original.gif" if is_gif else "original.png",
        "base64": encoded,
        "llm_base64": preview or encoded,
        "is_gif": is_gif,
        "size": len(encoded) * 3 // 4,
        "standalone": True,
        "embedded": False,
    }


def _make_plugin():
    plugin = GossipSharer.__new__(GossipSharer)
    plugin.config = {}
    plugin.default_platform = "napcat2"
    plugin.sister_qq = "10001"
    plugin.sister_title = "姐姐"
    plugin.official_sister_openid = "SISTEROPENID"
    plugin.group_whitelist = ["860354493", "123456"]
    plugin.official_group_whitelist = ["GROUPOPENID"]
    plugin.enable_arbitrary_friend_targets = False
    plugin.enable_target_session_tasks = True
    plugin.enable_wake_images = True
    plugin.enable_wake_files = True
    plugin.enable_wake_forwards = True
    plugin.max_source_message_chars = 4000
    plugin.max_wake_images = 4
    plugin.max_wake_files = 3
    plugin.max_wake_image_mb = 15
    plugin.guarantee_threshold = 10
    plugin.no_share_counts = {}
    plugin._wake_signature_lock = asyncio.Lock()
    plugin._inflight_wake_signatures = set()
    plugin._recent_wake_signatures = {}
    plugin._official_sessions = {}
    plugin._captured_forward_sources = {}
    plugin._load_group_whitelist = Mock()
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(
            platform_insts=[
                _Platform("napcat", "aiocqhttp"),
                _Platform("napcat2", "aiocqhttp"),
                _Platform("official", "qq_official"),
            ]
        ),
        send_message=AsyncMock(return_value=True),
    )
    plugin._resolve_qq_self_id = AsyncMock(return_value="99999")
    plugin._prepare_wake_attachments = AsyncMock(side_effect=lambda *_: _prepared())
    plugin._prepare_wake_forwards = AsyncMock(
        return_value={"forwards": [], "fatal_failures": [], "warnings": []}
    )
    plugin._send_wake_payloads = AsyncMock(return_value=True)
    plugin._persist_cross_context = AsyncMock()
    return plugin


def _delegate(event, return_session):
    platform_id, target_type, target_id = return_session.split(":", 2)
    event.set_extra(SYNTHETIC_EVENT_EXTRA, True)
    event.set_extra(
        DELEGATED_TASK_EXTRA,
        {
            "requester_id": "10001",
            "requester_name": "requester",
            "return_session": return_session,
            "return_target_type": target_type,
            "return_target_id": target_id,
            "return_platform": platform_id,
        },
    )


async def _return_task_event(plugin):
    return await plugin._build_qq_task_wake_event(
        plugin._get_platform_by_id("napcat"),
        {
            "target_session": "napcat:GroupMessage:860354493",
            "target_type": "GroupMessage",
            "target_id": "860354493",
            "target_platform": "napcat",
            "return_session": "napcat:FriendMessage:10001",
            "return_target_type": "FriendMessage",
            "return_target_id": "10001",
            "return_platform": "napcat",
            "return_to_source": True,
            "source_session": "napcat:FriendMessage:10001",
            "requester_id": "10001",
            "requester_name": "requester",
            "task": "查找九宫格原图并回传",
        },
    )


class WakeReturnRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_group_lists_do_not_allow_cross_adapter_wake(self):
        for source_id, adapter, target_platform, target_id in (
            ("napcat", "aiocqhttp", "official", "GROUPOPENID"),
            ("official", "qq_official", "napcat", "123456"),
        ):
            with self.subTest(source=source_id, target=target_platform):
                plugin = _make_plugin()
                plugin.group_whitelist = []
                plugin.official_group_whitelist = []
                event = _Event(platform_id=source_id, adapter=adapter)

                result = await plugin._safe_wake_qq_session_task(
                    event,
                    target_id=target_id,
                    target_type="GroupMessage",
                    target_platform=target_platform,
                    task="检查跨适配器唤醒限制",
                )

                self.assertIn("唤醒失败", result)
                self.assertIn("不是同一类 QQ 适配器", result)
                self.assertTrue(
                    all(
                        not platform.committed
                        for platform in plugin.context.platform_manager.platform_insts
                    )
                )
                plugin._prepare_wake_attachments.assert_not_awaited()
                plugin.context.send_message.assert_not_awaited()

    async def test_nested_wakes_keep_original_private_return_route(self):
        plugin = _make_plugin()
        source = _Event()
        await plugin._safe_wake_qq_session_task(
            source,
            target_id="860354493",
            target_type="GroupMessage",
            target_platform="napcat",
            task="查找九宫格原图并回传",
            return_to_source=True,
        )
        platform = plugin._get_platform_by_id("napcat")
        first = platform.committed[-1]
        payload = first.get_extra(DELEGATED_TASK_EXTRA)
        self.assertEqual(payload["return_session"], source.unified_msg_origin)
        self.assertEqual(payload["return_target_type"], "FriendMessage")
        self.assertEqual(payload["return_target_id"], "10001")
        self.assertEqual(payload["return_platform"], "napcat")
        self.assertIs(first.get_extra("enable_streaming"), False)

        await plugin._safe_wake_qq_session_task(
            first,
            target_id="123456",
            target_type="GroupMessage",
            target_platform="napcat",
            task="继续查找原图",
            return_to_source=True,
        )
        second = platform.committed[-1]
        nested = second.get_extra(DELEGATED_TASK_EXTRA)
        self.assertEqual(nested["return_session"], source.unified_msg_origin)
        self.assertEqual(
            plugin._task_return_route(second),
            (source.unified_msg_origin, "FriendMessage", "10001", "napcat"),
        )
        self.assertEqual(nested["requester_id"], "10001")

    def test_plain_event_returns_to_actual_origin_despite_default_platform(self):
        plugin = _make_plugin()
        event = _Event()
        self.assertEqual(
            plugin._task_return_route(event),
            (event.unified_msg_origin, "FriendMessage", "10001", "napcat"),
        )

    def test_return_route_still_obeys_private_target_policy(self):
        plugin = _make_plugin()
        event = _Event(target_type="GroupMessage", target_id="860354493")
        _delegate(event, "napcat:FriendMessage:20002")
        with self.assertRaises(ValueError):
            plugin._task_return_route(event)

    def test_legacy_isolated_group_source_uses_group_id_for_policy(self):
        plugin = _make_plugin()
        event = _Event(target_type="GroupMessage", target_id="123456")
        event.set_extra(
            DELEGATED_TASK_EXTRA,
            {"source_session": "napcat:GroupMessage:10001_860354493"},
        )
        self.assertEqual(
            plugin._task_return_route(event),
            (
                "napcat:GroupMessage:10001_860354493",
                "GroupMessage",
                "860354493",
                "napcat",
            ),
        )

    async def test_same_session_wake_is_rejected_before_queue_commit(self):
        plugin = _make_plugin()
        event = _Event()
        result = await plugin._safe_wake_qq_session_task(
            event,
            target_id="10001",
            target_type="FriendMessage",
            target_platform="napcat",
            task="把原图发给我",
        )
        self.assertTrue(result.startswith("唤醒失败："), result)
        self.assertFalse(plugin._get_platform_by_id("napcat").committed)
        plugin._prepare_wake_attachments.assert_not_awaited()

    async def test_isolated_group_cannot_wake_the_same_visible_group(self):
        plugin = _make_plugin()
        event = _Event(target_type="GroupMessage", target_id="860354493")
        event.unified_msg_origin = "napcat:GroupMessage:10001_860354493"

        result = await plugin._safe_wake_qq_session_task(
            event,
            target_id="860354493",
            target_type="GroupMessage",
            target_platform="napcat",
            task="把原图发到这里",
        )

        self.assertTrue(result.startswith("唤醒失败："), result)
        self.assertFalse(plugin._get_platform_by_id("napcat").committed)

    def test_target_instructions_identify_execution_and_return_destinations(self):
        plugin = _make_plugin()
        prompt = plugin._build_target_task_text(
            {
                "target_session": "napcat:GroupMessage:860354493",
                "return_session": "napcat:FriendMessage:10001",
                "return_to_source": True,
                "requester_id": "10001",
                "requester_name": "requester",
                "source_session": "napcat:FriendMessage:10001",
                "task": "查找原图并回传",
            }
        )
        self.assertIn("napcat:GroupMessage:860354493", prompt)
        self.assertIn("napcat:FriendMessage:10001", prompt)
        self.assertIn("reply_qq_session_task", prompt)

    async def test_public_wake_reports_submission_and_preserves_dedup_status(self):
        plugin = _make_plugin()
        event = _Event()
        result = await plugin.wake_qq_session_task(
            event,
            "860354493",
            "查找原图",
            target_type="GroupMessage",
            target_platform="napcat",
        )
        self.assertIn("任务已提交", result)
        self.assertNotIn("已唤醒", result)
        result = await plugin.wake_qq_session_task(
            event,
            "860354493",
            "查找原图",
            target_type="GroupMessage",
            target_platform="napcat",
        )
        self.assertTrue(result.startswith("本次唤醒"), result)
        self.assertNotIn("已唤醒", result)
        self.assertEqual(len(plugin._get_platform_by_id("napcat").committed), 1)


class ReplyAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_event_sends_original_image_to_current_private_session(self):
        plugin = _make_plugin()
        event = _Event()
        plugin._prepare_wake_attachments.return_value = _prepared(images=[_image()])
        plugin._prepare_wake_attachments.side_effect = None

        result = await plugin.reply_qq_session_task(event, image_refs=["image_1"])

        self.assertTrue(result.startswith("结果发送调用成功："), result)
        sent_event, sent_session, prepared = plugin._send_wake_payloads.await_args.args
        self.assertEqual(sent_session, event.unified_msg_origin)
        self.assertEqual(prepared["target_platform"], "napcat")
        self.assertEqual(prepared["target_type"], "FriendMessage")
        self.assertEqual(prepared["target_id"], "10001")
        self.assertEqual(prepared["images"][0]["base64"], _image()["base64"])
        self.assertEqual(len(prepared["images"]), 1)
        self.assertEqual(len(prepared["files"]), 0)
        self.assertEqual(sent_event.unified_msg_origin, sent_session)

    async def test_official_group_reply_uses_private_target_event(self):
        plugin = _make_plugin()
        event = _Event(
            platform_id="official",
            adapter="qq_official",
            target_type="GroupMessage",
            target_id="GROUPOPENID",
        )
        _delegate(event, "official:FriendMessage:SISTEROPENID")
        plugin._prepare_wake_attachments.return_value = _prepared(images=[_image()])
        plugin._prepare_wake_attachments.side_effect = None

        async def build_event(platform, payload, *_args):
            target = _Event(
                platform_id=platform.meta().id,
                adapter=platform.meta().name,
                target_type=payload["target_type"],
                target_id=payload["target_id"],
                sender_id=payload["target_id"],
            )
            return target

        plugin._build_qq_task_wake_event = AsyncMock(side_effect=build_event)

        result = await plugin.reply_qq_session_task(event, image_refs=["image_1"])

        self.assertTrue(result.startswith("结果发送调用成功："), result)
        sent_event, sent_session, _ = plugin._send_wake_payloads.await_args.args
        self.assertIsNot(sent_event, event)
        self.assertEqual(sent_session, "official:FriendMessage:SISTEROPENID")
        self.assertEqual(sent_event.unified_msg_origin, sent_session)
        self.assertFalse(sent_event.get_group_id())
        self.assertEqual(sent_event.get_sender_id(), "SISTEROPENID")
        self.assertFalse(plugin._get_platform_by_id("official").committed)

    async def test_invalid_selected_attachment_does_not_send_any_result(self):
        plugin = _make_plugin()
        event = _Event()
        plugin._prepare_wake_attachments.return_value = _prepared(
            failures=["image_1 is unavailable"]
        )
        plugin._prepare_wake_attachments.side_effect = None

        result = await plugin.reply_qq_session_task(event, image_refs=["image_1"])

        self.assertTrue(result.startswith("回传失败："), result)
        plugin._send_wake_payloads.assert_not_awaited()
        plugin.context.send_message.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_attachment_preparation_exception_is_reported_without_sending(self):
        plugin = _make_plugin()
        event = _Event()
        plugin._prepare_wake_attachments.side_effect = ValueError("original missing")

        result = await plugin.reply_qq_session_task(event, image_refs=["image_1"])

        self.assertTrue(result.startswith("回传失败："), result)
        plugin._send_wake_payloads.assert_not_awaited()
        plugin.context.send_message.assert_not_awaited()

    async def test_empty_reply_does_not_report_attachment_delivery(self):
        plugin = _make_plugin()
        result = await plugin.reply_qq_session_task(_Event())
        self.assertTrue(result.startswith("回传失败："), result)
        plugin._send_wake_payloads.assert_not_awaited()

    async def test_return_mode_routes_final_output_and_suppresses_working_group_reply(
        self,
    ):
        plugin = _make_plugin()
        event = _Event(target_type="GroupMessage", target_id="860354493")
        _delegate(event, "napcat:FriendMessage:10001")
        payload = event.get_extra(DELEGATED_TASK_EXTRA)
        payload["return_to_source"] = True
        payload["target_session"] = event.unified_msg_origin
        event.set_extra(
            namespace["PENDING_WAKE_ATTACHMENTS_EXTRA"],
            _prepared(images=[_image()]),
        )
        response = SimpleNamespace(
            role="assistant",
            completion_text="已找到九宫格原图",
            result_chain=_Chain().message("已找到九宫格原图"),
        )
        plugin.reply_qq_session_task = AsyncMock(
            return_value="结果发送调用成功：napcat:FriendMessage:10001"
        )

        await plugin.send_pending_wake_attachments_for_empty_reply(
            event, None, response
        )

        plugin.reply_qq_session_task.assert_awaited_once()
        reply_call = plugin.reply_qq_session_task.await_args
        content = reply_call.kwargs.get("content")
        if content is None and len(reply_call.args) > 1:
            content = reply_call.args[1]
        self.assertTrue(content.startswith("已找到九宫格原图"), content)
        self.assertFalse(response.completion_text)
        self.assertFalse(getattr(response.result_chain, "chain", None))
        await plugin.send_pending_wake_attachments(event)
        plugin._send_wake_payloads.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_direct_tool_image_returns_to_private_without_duplicate_final(self):
        plugin = _make_plugin()
        event = await _return_task_event(plugin)
        original = _image()
        plugin._prepare_wake_attachments.return_value = _prepared(images=[original])
        plugin._prepare_wake_attachments.side_effect = None

        await event.send(
            _Chain(type="tool_direct_result").base64_image(original["base64"])
        )

        sent_event, destination, prepared = plugin._send_wake_payloads.await_args.args
        self.assertEqual(destination, "napcat:FriendMessage:10001")
        self.assertEqual(prepared["target_type"], "FriendMessage")
        self.assertEqual(prepared["images"][0]["base64"], original["base64"])
        self.assertEqual(sent_event.get_group_id(), "860354493")
        self.assertEqual(event.unified_msg_origin, "napcat:GroupMessage:860354493")
        self.assertEqual(event.get_group_id(), "860354493")

        response = SimpleNamespace(
            role="assistant",
            completion_text="",
            result_chain=_Chain().base64_image(original["base64"]),
        )
        await plugin.send_pending_wake_attachments_for_empty_reply(
            event, None, response
        )

        self.assertEqual(plugin._send_wake_payloads.await_count, 1)
        self.assertFalse(getattr(response.result_chain, "chain", None))

    async def test_final_media_chain_returns_original_image_to_requester(self):
        plugin = _make_plugin()
        event = await _return_task_event(plugin)
        original = _image()
        plugin._prepare_wake_attachments.return_value = _prepared(images=[original])
        plugin._prepare_wake_attachments.side_effect = None
        response = SimpleNamespace(
            role="assistant",
            completion_text="",
            result_chain=_Chain().base64_image(original["base64"]),
        )

        await plugin.send_pending_wake_attachments_for_empty_reply(
            event, None, response
        )

        _, destination, prepared = plugin._send_wake_payloads.await_args.args
        self.assertEqual(destination, "napcat:FriendMessage:10001")
        self.assertEqual(prepared["images"][0]["base64"], original["base64"])
        self.assertFalse(response.completion_text)
        self.assertFalse(getattr(response.result_chain, "chain", None))

    async def test_intermediate_text_does_not_swallow_final_original_image(self):
        plugin = _make_plugin()
        event = await _return_task_event(plugin)
        original = _image()

        def prepare(_event, image_refs, _file_refs):
            return _prepared(images=[original] if image_refs else [])

        plugin._prepare_wake_attachments.side_effect = prepare
        await event.send(_Chain().message("我去群里找找"))
        response = SimpleNamespace(
            role="assistant",
            completion_text="",
            result_chain=_Chain().base64_image(original["base64"]),
        )

        await plugin.send_pending_wake_attachments_for_empty_reply(
            event, None, response
        )

        image_deliveries = [
            call
            for call in plugin._send_wake_payloads.await_args_list
            if call.args[2]["images"]
        ]
        self.assertEqual(len(image_deliveries), 1)
        self.assertEqual(image_deliveries[0].args[1], "napcat:FriendMessage:10001")
        self.assertFalse(getattr(response.result_chain, "chain", None))

    def test_seeded_gif_keeps_original_bytes_and_separate_llm_preview(self):
        plugin = _make_plugin()
        original = base64.b64encode(b"GIF89a\x01\x00\x01\x00original").decode()
        preview = base64.b64encode(b"\x89PNG\r\n\x1a\npreview").decode()
        event = _Event(target_type="GroupMessage", target_id="860354493")
        plugin._seed_wake_attachment_registry(
            event, _prepared(images=[_image(original, preview, True)])
        )

        registry = event.get_extra(ATTACHMENT_REGISTRY_EXTRA)
        images = [entry for entry in registry.values() if entry["kind"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["snapshot_base64"], original)
        self.assertEqual(images[0]["llm_snapshot_base64"], preview)
        self.assertTrue(images[0]["is_gif"])

    async def test_synthetic_request_exposes_seeded_attachment_without_share_count(
        self,
    ):
        plugin = _make_plugin()
        event = _Event(target_type="GroupMessage", target_id="860354493")
        event.set_extra(SYNTHETIC_EVENT_EXTRA, True)
        plugin._seed_wake_attachment_registry(event, _prepared(images=[_image()]))
        request = SimpleNamespace(
            image_urls=[], extra_user_content_parts=[], func_tool=None, prompt="task"
        )

        await plugin.auto_share_logic(event, request)

        catalog = "\n".join(part.text for part in request.extra_user_content_parts)
        self.assertIn("image_1", catalog)
        self.assertFalse(plugin.no_share_counts)


class AttachmentSendRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_image_failure_retries_only_unsent_image(self):
        plugin = _make_plugin()
        event = _Event()
        first = _image(base64.b64encode(b"first original").decode())
        second = _image(base64.b64encode(b"second original").decode())
        second.update(ref="image_2", registry_id="image_2", name="second.png")
        prepared = _prepared(images=[first, second])
        plugin.context.send_message.side_effect = [
            True,
            RuntimeError("second upload failed"),
            True,
        ]

        first_attempt = await plugin._send_wake_attachments(
            event, event.unified_msg_origin, prepared
        )

        self.assertFalse(first_attempt)
        self.assertTrue(first["delivered"])
        self.assertFalse(second.get("delivered", False))
        self.assertEqual(second["delivery_error"], "second upload failed")

        retry = await plugin._send_wake_attachments(
            event, event.unified_msg_origin, prepared
        )

        self.assertTrue(retry)
        self.assertTrue(second["delivered"])
        self.assertNotIn("delivery_error", second)
        calls = plugin.context.send_message.await_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [call.args[1].chain[0].file for call in calls],
            [
                f"base64://{first['base64']}",
                f"base64://{second['base64']}",
                f"base64://{second['base64']}",
            ],
        )
        self.assertTrue(all(call.args[0] == event.unified_msg_origin for call in calls))

    async def test_official_mismatched_event_cannot_send_to_its_working_group(self):
        plugin = _make_plugin()
        event = _Event(
            platform_id="official",
            adapter="qq_official",
            target_type="GroupMessage",
            target_id="GROUPOPENID",
        )
        image = _image()

        sent = await plugin._send_wake_attachments(
            event,
            "official:FriendMessage:SISTEROPENID",
            _prepared(images=[image]),
        )

        self.assertFalse(sent)
        self.assertFalse(image.get("delivered", False))
        self.assertIn("delivery_error", image)
        event.send.assert_not_awaited()
        plugin.context.send_message.assert_not_awaited()

    async def test_official_isolated_group_event_can_send_to_its_plain_group_route(
        self,
    ):
        plugin = _make_plugin()
        event = _Event(
            platform_id="official",
            adapter="qq_official",
            target_type="GroupMessage",
            target_id="GROUPOPENID",
        )
        event.unified_msg_origin = "official:GroupMessage:10001_GROUPOPENID"
        image = _image()
        prepared = _prepared(images=[image])
        prepared.update(
            target_type="GroupMessage",
            target_id="GROUPOPENID",
            target_platform="official",
        )

        sent = await plugin._send_wake_attachments(
            event, "official:GroupMessage:GROUPOPENID", prepared
        )

        self.assertTrue(sent)
        self.assertTrue(image["delivered"])
        event.send.assert_awaited_once()
        self.assertEqual(
            event.send.await_args.args[0].chain[0].file,
            f"base64://{image['base64']}",
        )
        plugin.context.send_message.assert_not_awaited()

    async def test_official_plain_group_event_can_send_to_original_isolated_route(self):
        plugin = _make_plugin()
        event = _Event(
            platform_id="official",
            adapter="qq_official",
            target_type="GroupMessage",
            target_id="GROUPOPENID",
        )
        image = _image()
        prepared = _prepared(images=[image])
        prepared.update(
            target_type="GroupMessage",
            target_id="GROUPOPENID",
            target_platform="official",
        )

        sent = await plugin._send_wake_attachments(
            event, "official:GroupMessage:10001_GROUPOPENID", prepared
        )

        self.assertTrue(sent)
        self.assertTrue(image["delivered"])
        event.send.assert_awaited_once()
        self.assertEqual(
            event.send.await_args.args[0].chain[0].file,
            f"base64://{image['base64']}",
        )
        plugin.context.send_message.assert_not_awaited()

    async def test_reply_reprepares_then_preserves_partial_delivery_state(self):
        plugin = _make_plugin()
        event = _Event()
        first_encoded = base64.b64encode(b"first original").decode()
        second_encoded = base64.b64encode(b"second original").decode()
        snapshots = []

        def prepare(_event, _image_refs, _file_refs):
            first = _image(first_encoded)
            second = _image(second_encoded)
            second.update(ref="image_2", registry_id="image_2", name="second.png")
            prepared = _prepared(images=[first, second])
            snapshots.append(prepared)
            return prepared

        plugin._prepare_wake_attachments.side_effect = prepare
        del plugin._send_wake_payloads
        plugin.context.send_message.side_effect = [
            True,
            RuntimeError("second upload failed"),
            True,
        ]

        first_result = await plugin.reply_qq_session_task(
            event, image_refs=["image_1", "image_2"]
        )
        self.assertTrue(first_result.startswith("回传失败："), first_result)
        self.assertTrue(snapshots[0]["images"][0]["delivered"])

        retry_result = await plugin.reply_qq_session_task(
            event, image_refs=["image_1", "image_2"]
        )
        self.assertTrue(retry_result.startswith("结果发送调用成功："), retry_result)
        self.assertEqual(len(snapshots), 2)
        self.assertIsNot(snapshots[0], snapshots[1])
        self.assertTrue(all(image["delivered"] for image in snapshots[1]["images"]))
        self.assertEqual(
            [
                call.args[1].chain[0].file
                for call in plugin.context.send_message.await_args_list
            ],
            [
                f"base64://{first_encoded}",
                f"base64://{second_encoded}",
                f"base64://{second_encoded}",
            ],
        )

        duplicate = await plugin.reply_qq_session_task(
            event, image_refs=["image_1", "image_2"]
        )
        self.assertEqual(duplicate, retry_result)
        self.assertEqual(plugin.context.send_message.await_count, 3)
        self.assertEqual(len(snapshots), 2)


if __name__ == "__main__":
    unittest.main()
