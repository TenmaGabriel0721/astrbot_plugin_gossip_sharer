import base64
import copy
import hashlib
import html
import io
import json
import math
import mimetypes
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import (
    At,
    File,
    Forward,
    Image,
    Node,
    Nodes,
    Plain,
    Reply,
)
from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import (
    get_astrbot_temp_path,
    get_astrbot_workspaces_path,
)
from astrbot.core.utils.media_utils import file_uri_to_path, is_file_uri

PLUGIN_VERSION = "1.9.6"
SYNTHETIC_EVENT_EXTRA = "gossip_sharer_synthetic_event"
DELEGATED_TASK_EXTRA = "gossip_sharer_delegated_target_task"
ATTACHMENT_REGISTRY_EXTRA = "gossip_sharer_attachment_registry"
FORWARD_REGISTRY_EXTRA = "gossip_sharer_forward_registry"
CAPTURED_FORWARD_SOURCES_EXTRA = "gossip_sharer_captured_forward_sources"
PENDING_WAKE_ATTACHMENTS_EXTRA = "gossip_sharer_pending_wake_attachments"
WAKE_ATTACHMENTS_SENT_EXTRA = "gossip_sharer_wake_attachments_sent"

FORWARD_CAPTURE_TTL_SECONDS = 180
FORWARD_CAPTURE_MAX_MESSAGES = 30


@register(
    "astrbot_plugin_gossip_sharer",
    "TenmaGabriel0721",
    "全能消息转发与告状工具",
    PLUGIN_VERSION,
)
class GossipSharer(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)

        self.config = config or {}
        self.default_platform = str(self.config.get("default_platform", "")).strip()
        self.sister_qq = str(self.config.get("sister_qq", "")).strip()
        self.enable_arbitrary_friend_targets = bool(
            self.config.get("enable_arbitrary_friend_targets", False)
        )
        self.enable_target_session_tasks = bool(
            self.config.get("enable_target_session_tasks", True)
        )
        self.enable_wake_images = self._normalize_bool(
            self.config.get("enable_wake_images", True)
        )
        self.enable_wake_files = self._normalize_bool(
            self.config.get("enable_wake_files", True)
        )
        self.enable_wake_forwards = self._normalize_bool(
            self.config.get("enable_wake_forwards", True)
        )
        self.allow_remote_attachment_urls = self._normalize_bool(
            self.config.get("allow_remote_attachment_urls", True)
        )
        self.max_wake_images = self._config_int("max_wake_images", 4, minimum=0)
        self.max_wake_files = self._config_int("max_wake_files", 3, minimum=0)
        self.max_wake_forwards = self._config_int(
            "max_wake_forwards", 10, minimum=0, maximum=50
        )
        self.max_forward_nodes = self._config_int(
            "max_forward_nodes", 50, minimum=1, maximum=200
        )
        self.max_wake_image_mb = self._config_int("max_wake_image_mb", 15, minimum=1)
        self.max_wake_file_mb = self._config_int("max_wake_file_mb", 50, minimum=1)
        self.max_wake_total_mb = self._config_int("max_wake_total_mb", 100, minimum=1)
        self.max_source_message_chars = self._config_int(
            "max_source_message_chars", 4000, minimum=0
        )
        self.wake_route_cooldown_seconds = self._config_int(
            "wake_route_cooldown_seconds", 60, minimum=0, maximum=86400
        )
        self.wake_target_window_seconds = self._config_int(
            "wake_target_window_seconds", 1800, minimum=0, maximum=604800
        )
        self.wake_target_max_in_window = self._config_int(
            "wake_target_max_in_window", 3, minimum=0, maximum=1000
        )
        self.attachment_allowed_roots = self._normalize_string_list(
            self.config.get("attachment_allowed_roots", []),
            split_whitespace=False,
        )
        self.group_whitelist = []
        self._load_group_whitelist()
        self.guarantee_threshold = int(self.config.get("guarantee_threshold", 10))
        self.guarantee_injection_method = str(
            self.config.get("guarantee_injection_method", "extra_user_content")
        ).strip()
        if self.guarantee_injection_method not in {
            "extra_user_content",
            "user_message_before",
            "user_message_after",
        }:
            logger.warning(
                "未知的主动社交提醒注入位置 "
                f"{self.guarantee_injection_method}，已回退到 extra_user_content"
            )
            self.guarantee_injection_method = "extra_user_content"
        self.no_share_counts: dict[str, int] = {}
        self._captured_forward_sources: dict[str, dict] = {}
        self._wake_route_last_sent: dict[str, float] = {}
        self._wake_target_history: dict[str, list[float]] = {}

        if not self.default_platform:
            logger.warning(
                "转发告状工具未配置 default_platform，发送时需要显式传入 target_platform"
            )
        if not self.sister_qq:
            logger.warning(
                "转发告状工具未配置 sister_qq，默认私聊目标与保底提示将不可用"
            )

        logger.info(
            f"转发告状工具 v{PLUGIN_VERSION} 已加载。姐姐: {self.sister_qq or '未配置'}，"
            f"默认平台: {self.default_platform or '未配置'}，白名单群数量: {len(self.group_whitelist)}，"
            f"保底阈值/注入位置: {self.guarantee_threshold}/{self.guarantee_injection_method}，"
            f"任意私聊目标: {self.enable_arbitrary_friend_targets}，"
            f"目标会话任务唤醒: {self.enable_target_session_tasks}，"
            f"唤醒图片/文件/合并记录: "
            f"{self.enable_wake_images}/{self.enable_wake_files}/{self.enable_wake_forwards}，"
            f"wake 限流: 路由冷却 {self.wake_route_cooldown_seconds}s，"
            f"目标 {self.wake_target_window_seconds}s/{self.wake_target_max_in_window} 次"
        )

    async def terminate(self):
        self._captured_forward_sources.clear()
        self._wake_route_last_sent.clear()
        self._wake_target_history.clear()

    def _soft_whitelist_config_path(self) -> str:
        return os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "config",
                "astrbot_plugin_soft_whitelist_config.json",
            )
        )

    def _config_group_whitelist(self) -> list[str]:
        return [
            str(x).strip()
            for x in self.config.get("group_whitelist", [])
            if str(x).strip()
        ]

    def _load_soft_whitelist_groups(self) -> list[str]:
        path = self._soft_whitelist_config_path()
        if not os.path.exists(path):
            logger.warning(f"软白名单配置不存在，跳过读取: {path}")
            return []

        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"加载软白名单配置失败: {e}")
            return []

        if not isinstance(data, dict):
            logger.warning("软白名单配置格式不是对象，跳过读取")
            return []

        groups = [
            str(x).strip() for x in data.get("group_whitelist", []) if str(x).strip()
        ]
        logger.info(f"已读取软白名单群配置 {len(groups)} 个")
        return groups

    def _load_group_whitelist(self):
        groups = self._load_soft_whitelist_groups() + self._config_group_whitelist()
        self.group_whitelist = list(dict.fromkeys(groups))

    def _event_key(self, event: AstrMessageEvent | None) -> str:
        if event is None:
            return "未知会话"
        return str(
            getattr(event, "unified_msg_origin", None)
            or getattr(event, "session", "未知会话")
        )

    def _unwrap_message_event(self, event_or_context) -> AstrMessageEvent | None:
        if event_or_context is None:
            return None

        candidates = [event_or_context]
        seen = set()
        while candidates:
            candidate = candidates.pop(0)
            if candidate is None:
                continue
            marker = id(candidate)
            if marker in seen:
                continue
            seen.add(marker)

            if callable(getattr(candidate, "get_platform_name", None)) and callable(
                getattr(candidate, "get_sender_id", None)
            ):
                return candidate

            inner_context = getattr(candidate, "context", None)
            candidates.append(getattr(inner_context, "event", None))
            candidates.append(getattr(candidate, "event", None))

        return None

    def _describe_event_like(self, event_or_context) -> str:
        if event_or_context is None:
            return "None"
        inner_context = getattr(event_or_context, "context", None)
        inner_event = getattr(inner_context, "event", None)
        if inner_event is not None:
            return (
                f"{type(event_or_context).__name__}"
                f"(context={type(inner_context).__name__}, event={type(inner_event).__name__})"
            )
        return type(event_or_context).__name__

    def _get_delegated_task_payload(self, event: AstrMessageEvent | None) -> dict:
        if event is None:
            return {}
        try:
            payload = event.get_extra(DELEGATED_TASK_EXTRA, {})
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _get_effective_requester(
        self, event: AstrMessageEvent | None
    ) -> tuple[str, str]:
        payload = self._get_delegated_task_payload(event)
        requester_id = str(payload.get("requester_id") or "").strip()
        requester_name = str(payload.get("requester_name") or "").strip()
        if not requester_id and event is not None:
            requester_id = str(
                getattr(event, "get_sender_id", lambda: "")() or ""
            ).strip()
        if not requester_name and event is not None:
            requester_name = str(
                getattr(event, "get_sender_name", lambda: "")() or ""
            ).strip()
        if not requester_name:
            requester_name = requester_id
        return requester_id, requester_name

    def _reset_no_share_count(self, event: AstrMessageEvent | None) -> None:
        self.no_share_counts.pop(self._event_key(event), None)

    def _wake_rate_limit_error(
        self,
        source_session: str,
        target_session: str,
        *,
        now: float | None = None,
    ) -> str | None:
        """Return a natural tool error when a wake would exceed its rate limit.

        Args:
            source_session: Unified session that initiated the wake.
            target_session: Unified session that would receive the wake.
            now: Optional monotonic timestamp used by deterministic tests.

        Returns:
            A tool-facing error string when blocked, otherwise ``None``.
        """

        current = time.monotonic() if now is None else float(now)
        route_key = f"{source_session}\n{target_session}"
        if self.wake_route_cooldown_seconds > 0:
            last_sent = self._wake_route_last_sent.get(route_key)
            if last_sent is not None:
                remaining = self.wake_route_cooldown_seconds - (current - last_sent)
                if remaining > 0:
                    wait_seconds = max(1, math.ceil(remaining))
                    return (
                        "唤醒失败：频率限制：同一来源会话到该目标的两次联系"
                        f"至少间隔 {self.wake_route_cooldown_seconds} 秒，"
                        f"还需等待约 {wait_seconds} 秒。请勿立即重试；"
                        "继续在当前会话自然回复，稍后有新内容时再考虑联系。"
                    )

        if self.wake_target_window_seconds > 0 and self.wake_target_max_in_window > 0:
            cutoff = current - self.wake_target_window_seconds
            history = [
                timestamp
                for timestamp in self._wake_target_history.get(target_session, [])
                if timestamp > cutoff
            ]
            if history:
                self._wake_target_history[target_session] = history
            else:
                self._wake_target_history.pop(target_session, None)
            if len(history) >= self.wake_target_max_in_window:
                remaining = history[0] + self.wake_target_window_seconds - current
                wait_seconds = max(1, math.ceil(remaining))
                window_text = (
                    f"{self.wake_target_window_seconds // 60} 分钟"
                    if self.wake_target_window_seconds % 60 == 0
                    else f"{self.wake_target_window_seconds} 秒"
                )
                return (
                    f"唤醒失败：频率限制：该目标会话在最近 {window_text} "
                    f"已经接收 {len(history)} 次跨会话联系，上限为 "
                    f"{self.wake_target_max_in_window} 次，约 {wait_seconds} 秒后"
                    "才会释放名额。请勿立即重试；继续在当前会话自然回复。"
                )
        return None

    def _build_session_id(
        self, target_type: str, target_id: str, target_platform: str = None
    ) -> str | None:
        platform = str(target_platform or self.default_platform).strip()
        if not platform:
            return None
        return f"{platform}:{target_type}:{str(target_id)}"

    def _validate_target(
        self, target_type: str, target_id: str, target_platform: str = None
    ) -> str | None:
        self._load_group_whitelist()
        if target_type not in ("FriendMessage", "GroupMessage"):
            return "发送失败：target_type 只允许为 FriendMessage 或 GroupMessage。"
        if not str(target_id).strip():
            return "发送失败：target_id 不能为空。"
        if not str(target_platform or self.default_platform).strip():
            return "发送失败：未配置默认平台 ID，请先配置 default_platform 或传入 target_platform。"
        if target_type == "GroupMessage" and str(target_id) not in self.group_whitelist:
            return f"发送失败：群 {target_id} 不在白名单里。"
        if target_type == "FriendMessage":
            if not self.sister_qq:
                return "发送失败：未配置 sister_qq，无法校验默认私聊目标。"
            if (
                not self.enable_arbitrary_friend_targets
                and str(target_id) != self.sister_qq
            ):
                return (
                    "发送失败：当前未开启任意私聊目标，仅允许发送给配置的 sister_qq。"
                )
        return None

    def _build_image_context_notes(
        self,
        image_url: str | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
    ) -> list[str]:
        if not image_url and not image_path and not image_base64:
            return []
        return [
            "图片已随跨会话消息发送；如需让 Bot 再次识别，请引用目标会话中的图片消息。"
        ]

    def _normalize_bool(self, value) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        return text in ("1", "true", "yes", "y", "on", "是", "开启")

    def _config_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """Read and clamp an integer plugin configuration value.

        Args:
            key: Configuration key to read.
            default: Fallback used when the configured value is invalid.
            minimum: Optional inclusive lower bound.
            maximum: Optional inclusive upper bound.

        Returns:
            The parsed and clamped integer value.
        """

        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _normalize_string_list(
        self, value, *, split_whitespace: bool = True
    ) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            items = []
            for item in value:
                items.extend(
                    self._normalize_string_list(item, split_whitespace=split_whitespace)
                )
            return items
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                    return self._normalize_string_list(
                        parsed, split_whitespace=split_whitespace
                    )
                except Exception:
                    pass
            pattern = r"[\s,，;；]+" if split_whitespace else r"[,，;；]+"
            return [part.strip() for part in re.split(pattern, text) if part.strip()]
        text = str(value).strip()
        return [text] if text else []

    def _normalize_at_qqs(self, at_qqs) -> list[str]:
        qqs = []
        seen = set()
        for raw in self._normalize_string_list(at_qqs):
            qq = raw.strip().lstrip("@")
            if qq.lower().startswith("qq="):
                qq = qq[3:].strip()
            if not qq or qq.lower() == "all" or qq == "全体成员":
                continue
            if qq not in seen:
                seen.add(qq)
                qqs.append(qq)
        return qqs

    def _normalize_at_names(self, at_names) -> list[str]:
        return self._normalize_string_list(at_names, split_whitespace=False)

    def _normalize_target_type_name(
        self, target_type: str | None, default: str = "GroupMessage"
    ) -> str:
        text = str(target_type or default).strip()
        mapping = {
            "group": "GroupMessage",
            "groupmessage": "GroupMessage",
            "group_message": "GroupMessage",
            "群": "GroupMessage",
            "群聊": "GroupMessage",
            "qq群": "GroupMessage",
            "friend": "FriendMessage",
            "private": "FriendMessage",
            "friendmessage": "FriendMessage",
            "friend_message": "FriendMessage",
            "private_message": "FriendMessage",
            "私聊": "FriendMessage",
            "好友": "FriendMessage",
        }
        return mapping.get(text.lower(), text)

    def _is_qq_source_event(self, event: AstrMessageEvent | None) -> bool:
        if event is None:
            return False
        try:
            return event.get_platform_name() == "aiocqhttp"
        except Exception:
            return False

    def _event_message_id(self, event: AstrMessageEvent | None) -> str:
        if event is None:
            return ""
        message_obj = getattr(event, "message_obj", None)
        message_id = getattr(message_obj, "message_id", None)
        if message_id is None:
            raw = getattr(message_obj, "raw_message", None)
            if isinstance(raw, dict):
                message_id = raw.get("message_id")
        return str(message_id or "").strip()

    def _normalize_forward_time(self, value, default=None) -> str:
        candidate = value if value not in (None, "") else default
        if candidate in (None, ""):
            return ""
        try:
            timestamp = int(float(candidate))
        except (TypeError, ValueError):
            return ""
        if timestamp > 100_000_000_000:
            timestamp //= 1000
        return str(timestamp) if timestamp > 0 else ""

    def _event_timestamp(self, event: AstrMessageEvent | None) -> str:
        if event is None:
            return ""
        message_obj = getattr(event, "message_obj", None)
        timestamp = getattr(message_obj, "timestamp", None)
        raw = getattr(message_obj, "raw_message", None)
        if timestamp in (None, "") and isinstance(raw, dict):
            timestamp = raw.get("time") or raw.get("timestamp")
        return self._normalize_forward_time(timestamp)

    def _event_raw_segments(self, event: AstrMessageEvent | None) -> list[dict]:
        if event is None:
            return []
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return []
        segments = raw.get("message")
        if not isinstance(segments, list):
            return []
        return [copy.deepcopy(item) for item in segments if isinstance(item, dict)]

    def _extract_forward_ids_from_segments(self, segments) -> list[str]:
        ids = []
        for segment in segments or []:
            if not isinstance(segment, dict):
                continue
            seg_type = str(segment.get("type") or "").lower()
            data = segment.get("data")
            if not isinstance(data, dict):
                data = {}
            if seg_type in {"forward", "forward_msg"}:
                forward_id = str(data.get("id") or data.get("message_id") or "").strip()
                if forward_id:
                    ids.append(forward_id)
        return list(dict.fromkeys(ids))

    def _extract_forward_components(self, components) -> list[dict]:
        """Find native or inline merged-forward components recursively."""

        found = []
        for component in components or []:
            if isinstance(component, Forward):
                forward_id = str(getattr(component, "id", "") or "").strip()
                if forward_id:
                    found.append({"forward_id": forward_id})
            elif isinstance(component, Nodes):
                found.append({"nodes_component": component})
            elif isinstance(component, Node):
                found.append({"nodes_component": Nodes([component])})
            elif isinstance(component, Reply) and component.chain:
                for item in self._extract_forward_components(component.chain):
                    found.append({**item, "from_reply": True})
        return found

    def _build_capture_entries(self, event: AstrMessageEvent) -> list[dict]:
        """Capture source message IDs before debounce plugins rebuild the event."""

        if not self._is_qq_source_event(event):
            return []
        try:
            if event.get_extra(SYNTHETIC_EVENT_EXTRA, False):
                return []
        except Exception:
            pass

        message_id = self._event_message_id(event)
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        sender_name = str(
            getattr(event, "get_sender_name", lambda: "")() or sender_id
        ).strip()
        source_platform = str(
            getattr(event, "get_platform_id", lambda: "")() or ""
        ).strip()
        message_time = self._event_timestamp(event)
        raw_segments = self._event_raw_segments(event)
        components = getattr(event, "get_messages", lambda: [])() or []
        forward_components = self._extract_forward_components(components)
        direct_forward_components = [
            item for item in forward_components if not item.get("from_reply")
        ]
        forward_ids = self._extract_forward_ids_from_segments(raw_segments)
        forward_ids.extend(
            item["forward_id"]
            for item in direct_forward_components
            if item.get("forward_id")
        )
        forward_ids = list(dict.fromkeys(forward_ids))

        now = time.monotonic()
        entries = []
        for forward_id in forward_ids:
            entries.append(
                {
                    "kind": "forward",
                    "forward_id": forward_id,
                    "source_message_id": message_id,
                    "source": "当前消息",
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "source_platform": source_platform,
                    "time": message_time,
                    "captured_at": now,
                }
            )

        for item in direct_forward_components:
            nodes_component = item.get("nodes_component")
            if nodes_component is None:
                continue
            entries.append(
                {
                    "kind": "forward",
                    "nodes_component": nodes_component,
                    "source_message_id": message_id,
                    "source": "引用消息" if item.get("from_reply") else "当前消息",
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "source_platform": source_platform,
                    "time": message_time,
                    "captured_at": now,
                }
            )

        raw_has_non_forward = any(
            str(segment.get("type") or "").lower()
            not in {"forward", "forward_msg", "reply"}
            for segment in raw_segments
        )
        if message_id or raw_segments:
            if not forward_ids or raw_has_non_forward:
                entries.append(
                    {
                        "kind": "message",
                        "message_id": message_id,
                        "source": "当前消息",
                        "sender_id": sender_id,
                        "sender_name": sender_name,
                        "source_platform": source_platform,
                        "time": message_time,
                        "raw_segments": raw_segments,
                        "raw_has_non_forward": raw_has_non_forward,
                        "contains_forward": bool(
                            forward_ids or direct_forward_components
                        ),
                        "captured_at": now,
                    }
                )

        for component in components:
            if not isinstance(component, Reply):
                continue
            reply_id = str(getattr(component, "id", "") or "").strip()
            reply_chain = list(component.chain or [])
            nested_forwards = self._extract_forward_components(reply_chain)
            for item in nested_forwards:
                forward_id = str(item.get("forward_id") or "").strip()
                if forward_id:
                    entries.append(
                        {
                            "kind": "forward",
                            "forward_id": forward_id,
                            "source_message_id": reply_id,
                            "source": "引用消息",
                            "sender_id": str(
                                getattr(component, "sender_id", "") or sender_id
                            ).strip(),
                            "sender_name": str(
                                getattr(component, "sender_nickname", "")
                                or getattr(component, "sender_id", "")
                                or sender_name
                            ).strip(),
                            "source_platform": source_platform,
                            "time": self._normalize_forward_time(
                                getattr(component, "time", None), message_time
                            ),
                            "captured_at": now,
                        }
                    )
                elif item.get("nodes_component") is not None:
                    entries.append(
                        {
                            "kind": "forward",
                            "nodes_component": item["nodes_component"],
                            "source_message_id": reply_id,
                            "source": "引用消息",
                            "sender_id": str(
                                getattr(component, "sender_id", "") or sender_id
                            ).strip(),
                            "sender_name": str(
                                getattr(component, "sender_nickname", "")
                                or getattr(component, "sender_id", "")
                                or sender_name
                            ).strip(),
                            "source_platform": source_platform,
                            "time": self._normalize_forward_time(
                                getattr(component, "time", None), message_time
                            ),
                            "captured_at": now,
                        }
                    )
            if reply_id and not nested_forwards:
                entries.append(
                    {
                        "kind": "message",
                        "message_id": reply_id,
                        "source": "引用消息",
                        "sender_id": str(
                            getattr(component, "sender_id", "") or sender_id
                        ).strip(),
                        "sender_name": str(
                            getattr(component, "sender_nickname", "")
                            or getattr(component, "sender_id", "")
                            or sender_name
                        ).strip(),
                        "source_platform": source_platform,
                        "time": self._normalize_forward_time(
                            getattr(component, "time", None), message_time
                        ),
                        "component_chain": reply_chain,
                        "captured_at": now,
                    }
                )
        return entries

    def _capture_entry_identity(self, entry: dict) -> tuple:
        kind = str(entry.get("kind") or "")
        if kind == "forward":
            forward_id = str(entry.get("forward_id") or "")
            if forward_id:
                return kind, forward_id, str(entry.get("source_message_id") or "")
            return (
                kind,
                "inline",
                str(entry.get("source_message_id") or ""),
                str(entry.get("source") or ""),
            )
        message_id = str(entry.get("message_id") or "")
        if message_id:
            return kind, message_id
        return kind, json.dumps(
            entry.get("raw_segments") or [], sort_keys=True, default=str
        )

    def _prune_captured_forward_sources(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, payload in self._captured_forward_sources.items()
            if now - float(payload.get("updated_at") or 0) > FORWARD_CAPTURE_TTL_SECONDS
        ]
        for key in expired:
            self._captured_forward_sources.pop(key, None)

    def _store_captured_forward_sources(
        self, event: AstrMessageEvent, entries: list[dict]
    ) -> None:
        if not entries:
            return
        current_entries = []
        current_identities = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identity = self._capture_entry_identity(entry)
            if identity in current_identities:
                continue
            current_entries.append(entry)
            current_identities.add(identity)
        if not current_entries:
            return

        self._prune_captured_forward_sources()
        event_key = self._event_key(event)
        payload = self._captured_forward_sources.setdefault(
            event_key, {"updated_at": time.monotonic(), "entries": []}
        )
        identities = {self._capture_entry_identity(item) for item in payload["entries"]}
        for entry in current_entries:
            identity = self._capture_entry_identity(entry)
            if identity in identities:
                continue
            payload["entries"].append(entry)
            identities.add(identity)
        payload["entries"] = payload["entries"][-FORWARD_CAPTURE_MAX_MESSAGES:]
        payload["updated_at"] = time.monotonic()
        try:
            event.set_extra(CAPTURED_FORWARD_SOURCES_EXTRA, list(current_entries))
        except Exception:
            pass

    def _ensure_forward_registry(
        self, event: AstrMessageEvent | None
    ) -> dict[str, dict]:
        if event is None:
            return {}
        try:
            existing = event.get_extra(FORWARD_REGISTRY_EXTRA, {})
        except Exception:
            existing = {}
        if isinstance(existing, dict) and existing:
            return existing

        self._prune_captured_forward_sources()
        event_key = self._event_key(event)
        cached_payload = self._captured_forward_sources.get(event_key, {})
        entries = list(cached_payload.get("entries") or [])
        current_entries = []
        try:
            current_entries.extend(
                event.get_extra(CAPTURED_FORWARD_SOURCES_EXTRA, []) or []
            )
        except Exception:
            pass
        current_entries.extend(self._build_capture_entries(event))
        current_identities = {
            self._capture_entry_identity(entry)
            for entry in current_entries
            if isinstance(entry, dict)
        }
        entries.extend(current_entries)

        deduped = []
        identities = set()
        forward_source_message_ids = {
            str(item.get("source_message_id") or "").strip()
            for item in entries
            if isinstance(item, dict) and item.get("kind") == "forward"
        }
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("kind") == "message"
                and str(entry.get("message_id") or "").strip()
                and str(entry.get("message_id") or "").strip()
                in forward_source_message_ids
                and not entry.get("contains_forward")
            ):
                continue
            identity = self._capture_entry_identity(entry)
            if identity in identities:
                continue
            identities.add(identity)
            deduped.append(entry)

        if self.max_wake_forwards <= 0:
            deduped = []
        elif len(deduped) > self.max_wake_forwards:
            deduped = deduped[-self.max_wake_forwards :]

        registry = {}
        counters = {"forward": 0, "message": 0}
        message_position = 0
        current_message_count = sum(
            1
            for entry in deduped
            if entry.get("kind") == "message"
            and self._capture_entry_identity(entry) in current_identities
            and entry.get("source") == "当前消息"
        )
        for entry in deduped:
            kind = str(entry.get("kind") or "")
            if kind not in counters:
                continue
            counters[kind] += 1
            ref_id = f"{kind}_{counters[kind]}"
            is_current_event = self._capture_entry_identity(entry) in current_identities
            item = {
                **entry,
                "id": ref_id,
                "is_current_event": is_current_event,
            }
            if (
                kind == "message"
                and is_current_event
                and item.get("source") == "当前消息"
            ):
                message_position += 1
                if current_message_count > 1:
                    item["source"] = f"本轮第 {message_position} 条消息"
            elif item.get("source") == "当前消息":
                item["source"] = "近期消息"

            if kind == "message":
                preview = self._onebot_content_preview(
                    item.get("raw_segments") or [], limit=180
                )
                if not preview and item.get("component_chain"):
                    preview_parts = []
                    for component in item["component_chain"]:
                        if isinstance(component, Plain):
                            text = re.sub(r"\s+", " ", component.text).strip()
                            if text and text not in preview_parts:
                                preview_parts.append(text)
                            continue
                        component_type = getattr(component, "type", "")
                        component_type = str(
                            getattr(component_type, "value", component_type)
                        ).lower()
                        if isinstance(component, Image) or component_type == "image":
                            preview_parts.append("[图片]")
                        elif isinstance(component, File) or component_type == "file":
                            preview_parts.append(
                                f"[文件:{getattr(component, 'name', '') or 'file'}]"
                            )
                        elif isinstance(component, At) or component_type == "at":
                            preview_parts.append(
                                f"@{getattr(component, 'name', '') or getattr(component, 'qq', '') or '成员'}"
                            )
                        elif component_type == "face":
                            preview_parts.append("[表情]")
                    preview = " ".join(preview_parts)
                    if len(preview) > 180:
                        preview = preview[:180] + "…"
                item["preview"] = preview
            aliases = {ref_id}
            for alias in (
                item.get("forward_id"),
                item.get("message_id"),
                item.get("source_message_id"),
            ):
                alias = str(alias or "").strip()
                if alias:
                    aliases.add(alias)
            item["aliases"] = aliases
            registry[ref_id] = item

        try:
            event.set_extra(FORWARD_REGISTRY_EXTRA, registry)
        except Exception:
            pass
        return registry

    def _format_forward_catalog(self, registry: dict[str, dict]) -> str:
        if not registry or not self.enable_wake_forwards or self.max_wake_forwards <= 0:
            return ""
        lines = [
            "[可整理并发送为 QQ 合并聊天记录的来源]",
            "只有确实需要发送时，才把下面的短引用传给 wake_qq_session_task 的 forward_refs，或在 forward_items 中引用。",
            "forward_1 表示已有合并记录；message_1 表示一条零散消息，可把多个 message_* 组合成一条新的可展开记录。",
            "目录按时间顺序列出最近来源，冒号后的内容预览就是实际会被转发的消息。叙述涉及多条消息时必须选齐全部相关引用；不要只因编号靠前就选择，也不要只选附近的表情包。",
            "只要原消息仍有 message_*，优先用 forward_refs 保留真实内容和发送者；仅在确实需要改写或补写节点时使用 forward_items。",
        ]
        catalog_items = list(registry.items())[: self.max_wake_forwards]
        for ref_id, item in catalog_items:
            if item.get("kind") == "forward":
                kind_name = "已有合并聊天记录"
            else:
                kind_name = "可作为转发节点的零散消息"
            sender_id = str(item.get("sender_id") or "").strip()
            sender_name = str(item.get("sender_name") or sender_id).strip()
            sender = (
                f"{sender_name}({sender_id})"
                if sender_id and sender_name != sender_id
                else sender_name or "未知发送者"
            )
            preview = str(item.get("preview") or "").strip()
            preview_note = f"，内容：{preview}" if preview else ""
            lines.append(
                f"- {ref_id}: {kind_name}，{item.get('source') or '当前消息'}，"
                f"发送者 {sender}{preview_note}"
            )
        if len(registry) > len(catalog_items):
            lines.append(
                f"- 另有 {len(registry) - len(catalog_items)} 条来源未展示；"
                "如需更多请提高 max_wake_forwards。"
            )
        return "\n".join(lines)

    def _find_forward_entry(self, registry: dict[str, dict], ref: str) -> dict | None:
        direct = registry.get(ref)
        if direct:
            return direct
        for item in registry.values():
            if ref in item.get("aliases", set()):
                return item
        return None

    def _normalize_forward_items(self, value) -> list[dict]:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith(("[", "{")):
                try:
                    return self._normalize_forward_items(json.loads(text))
                except Exception:
                    pass
            return [{"text": text}]
        if isinstance(value, (list, tuple)):
            items = []
            for item in value:
                items.extend(self._normalize_forward_items(item))
            return items
        return [{"text": str(value)}]

    def _forward_item_attachment_refs(
        self, forward_items: list[dict]
    ) -> tuple[list[str], list[str]]:
        image_refs = []
        file_refs = []
        for item in forward_items:
            image_refs.extend(
                self._normalize_attachment_refs(
                    item.get("image_refs") or item.get("images")
                )
            )
            file_refs.extend(
                self._normalize_attachment_refs(
                    item.get("file_refs") or item.get("files")
                )
            )
        return list(dict.fromkeys(image_refs)), list(dict.fromkeys(file_refs))

    def _unwrap_onebot_action_payload(self, payload):
        if hasattr(payload, "data") and not isinstance(payload, dict):
            try:
                payload = payload.data
            except Exception:
                pass
        if not isinstance(payload, dict):
            return payload
        data = payload.get("data")
        if isinstance(data, dict) and not any(
            key in payload for key in ("messages", "message", "nodes", "nodeList")
        ):
            return data
        return payload

    def _normalize_onebot_content(self, raw_content) -> list[dict]:
        if isinstance(raw_content, list):
            return [
                copy.deepcopy(segment)
                for segment in raw_content
                if isinstance(segment, dict)
            ]
        if isinstance(raw_content, str):
            text = raw_content.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return self._normalize_onebot_content(parsed)
            return [{"type": "text", "data": {"text": text}}]
        return []

    def _sanitize_custom_forward_content(self, content: list[dict]) -> list[dict]:
        sanitized = []
        for segment in content:
            seg_type = str(segment.get("type") or "").lower()
            if seg_type in {"forward", "forward_msg", "node", "nodes"}:
                sanitized.append(
                    {
                        "type": "text",
                        "data": {"text": "[嵌套合并聊天记录]"},
                    }
                )
                continue
            sanitized.append(segment)
        return sanitized

    def _custom_node_from_raw_message(self, raw_node: dict) -> dict | None:
        if str(raw_node.get("type") or "").lower() == "node":
            data = raw_node.get("data")
            if isinstance(data, dict):
                content = self._sanitize_custom_forward_content(
                    self._normalize_onebot_content(data.get("content") or [])
                )
                if content:
                    sender_id = str(data.get("user_id") or data.get("uin") or "0")
                    sender_name = str(
                        data.get("nickname")
                        or data.get("name")
                        or sender_id
                        or "聊天记录"
                    )
                    node_data = {
                        "user_id": sender_id,
                        "uin": sender_id,
                        "nickname": sender_name,
                        "name": sender_name,
                        "content": content,
                    }
                    node_time = self._normalize_forward_time(
                        data.get("time") or data.get("timestamp")
                    )
                    if node_time:
                        node_data["time"] = node_time
                    return {"type": "node", "data": node_data}
        sender = raw_node.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        sender_id = str(
            sender.get("user_id")
            or raw_node.get("user_id")
            or raw_node.get("uin")
            or "0"
        )
        sender_name = str(
            sender.get("card")
            or sender.get("nickname")
            or raw_node.get("nickname")
            or raw_node.get("name")
            or sender_id
            or "聊天记录"
        )
        content = self._sanitize_custom_forward_content(
            self._normalize_onebot_content(
                raw_node.get("message") or raw_node.get("content") or []
            )
        )
        if not content:
            raw_text = str(raw_node.get("raw_message") or "").strip()
            if raw_text:
                content = [{"type": "text", "data": {"text": raw_text}}]
        if not content:
            return None
        node_data = {
            "user_id": sender_id,
            "uin": sender_id,
            "nickname": sender_name,
            "name": sender_name,
            "content": content,
        }
        node_time = self._normalize_forward_time(
            raw_node.get("time") or raw_node.get("timestamp")
        )
        if node_time:
            node_data["time"] = node_time
        return {"type": "node", "data": node_data}

    def _extract_forward_raw_nodes(self, payload) -> list[dict]:
        payload = self._unwrap_onebot_action_payload(payload)
        if not isinstance(payload, dict):
            return []
        nodes = (
            payload.get("messages")
            or payload.get("message")
            or payload.get("nodes")
            or payload.get("nodeList")
        )
        return [node for node in nodes or [] if isinstance(node, dict)]

    def _onebot_content_preview(self, content, limit: int = 120) -> str:
        parts: list[tuple[str, bool]] = []
        seen_texts = set()
        for segment in self._normalize_onebot_content(content):
            seg_type = str(segment.get("type") or "").lower()
            data = segment.get("data")
            if not isinstance(data, dict):
                data = {}
            if seg_type in {"text", "plain"}:
                text = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()
                if text and text not in seen_texts:
                    parts.append((text, False))
                    seen_texts.add(text)
            elif seg_type == "image":
                summary = html.unescape(
                    str(data.get("summary") or data.get("name") or "")
                ).strip()
                if summary.startswith("[") and summary.endswith("]"):
                    summary = summary[1:-1].strip()
                parts.append((f"[图片:{summary}]" if summary else "[图片]", True))
            elif seg_type == "file":
                parts.append(
                    (
                        f"[文件:{data.get('name') or data.get('file') or 'file'}]",
                        True,
                    )
                )
            elif seg_type in {"record", "voice"}:
                parts.append(("[语音]", True))
            elif seg_type == "video":
                parts.append(("[视频]", True))
            elif seg_type == "at":
                parts.append((f"@{data.get('name') or data.get('qq') or '成员'}", True))
            elif seg_type in {"face", "mface"}:
                summary = html.unescape(
                    str(
                        data.get("summary")
                        or data.get("name")
                        or data.get("text")
                        or ""
                    )
                ).strip()
                if summary.startswith("[") and summary.endswith("]"):
                    summary = summary[1:-1].strip()
                parts.append((f"[表情:{summary}]" if summary else "[表情]", True))
            elif seg_type == "reply":
                parts.append(("[回复消息]", True))
            elif seg_type in {"json", "xml"}:
                parts.append(("[卡片消息]", True))
            elif seg_type == "markdown":
                text = re.sub(
                    r"\s+",
                    " ",
                    str(data.get("content") or data.get("text") or ""),
                ).strip()
                if text:
                    if text not in seen_texts:
                        parts.append((text, False))
                        seen_texts.add(text)
                else:
                    parts.append(("[Markdown]", True))
            elif seg_type == "share":
                title = str(data.get("title") or "链接").strip()
                parts.append((f"[链接:{title}]", True))
            elif seg_type == "contact":
                parts.append(("[联系人]", True))
            elif seg_type == "location":
                title = str(data.get("title") or data.get("content") or "位置").strip()
                parts.append((f"[位置:{title}]", True))
            elif seg_type == "music":
                parts.append(("[音乐]", True))
            elif seg_type == "dice":
                parts.append(("[骰子]", True))
            elif seg_type == "rps":
                parts.append(("[猜拳]", True))
            elif seg_type == "poke":
                parts.append(("[戳一戳]", True))
            elif seg_type in {"forward", "forward_msg", "nodes"}:
                parts.append(("[嵌套合并记录]", True))

        preview = ""
        previous_separated = False
        for value, separated in parts:
            if (
                preview
                and (previous_separated or separated)
                and not preview.endswith(" ")
            ):
                preview += " "
            preview += value
            previous_separated = separated
        return preview if len(preview) <= limit else preview[:limit] + "…"

    def _forward_card_nodes_for_metadata(
        self,
        primary_nodes: list[dict],
        fallback_nodes: list[dict],
    ) -> list[dict]:
        # ID-only nodes have no sender/content fields. Prefer any reconstructed
        # nodes available for stable card metadata, even if only part of the
        # original record could be reconstructed.
        return fallback_nodes or primary_nodes

    def _forward_card_source(
        self,
        primary_nodes: list[dict],
        fallback_nodes: list[dict],
        *,
        is_group_record: bool,
    ) -> str:
        if is_group_record:
            return "群聊的聊天记录"

        sender_names = []
        for node in self._forward_card_nodes_for_metadata(
            primary_nodes, fallback_nodes
        ):
            data = node.get("data") if isinstance(node, dict) else None
            if not isinstance(data, dict):
                continue
            sender_name = str(data.get("nickname") or data.get("name") or "").strip()
            if sender_name and sender_name not in sender_names:
                sender_names.append(sender_name)
            if len(sender_names) >= 4:
                break
        return "和".join(sender_names) + "的聊天记录" if sender_names else "聊天记录"

    def _forward_card_news(
        self,
        primary_nodes: list[dict],
        fallback_nodes: list[dict],
        limit: int = 4,
    ) -> list[dict]:
        nodes = self._forward_card_nodes_for_metadata(primary_nodes, fallback_nodes)
        news = []
        for node in nodes:
            data = node.get("data") if isinstance(node, dict) else None
            if not isinstance(data, dict):
                continue
            content = data.get("content") or data.get("message") or []
            preview = self._onebot_content_preview(content)
            if not preview:
                continue
            sender_name = str(
                data.get("nickname") or data.get("name") or "未知发送者"
            ).strip()
            news.append({"text": f"{sender_name}: {preview}"})
            if len(news) >= limit:
                break
        return news

    def _forward_preview(self, raw_nodes: list[dict], limit: int = 1200) -> str:
        lines = []
        for raw_node in raw_nodes:
            sender = raw_node.get("sender")
            if not isinstance(sender, dict):
                sender = {}
            sender_name = str(
                sender.get("card")
                or sender.get("nickname")
                or raw_node.get("nickname")
                or raw_node.get("name")
                or "未知发送者"
            )
            preview = self._onebot_content_preview(
                raw_node.get("message") or raw_node.get("content") or []
            )
            if preview:
                lines.append(f"{sender_name}: {preview}")
            if len("\n".join(lines)) >= limit:
                break
        preview = "\n".join(lines)
        if len(preview) > limit:
            preview = preview[:limit] + "\n[预览已截断]"
        return preview

    def _prepared_image_for_ref(self, prepared: dict, ref: str) -> dict | None:
        for image in prepared.get("images", []):
            if ref in {image.get("ref"), image.get("registry_id")}:
                return image
        return None

    def _prepared_file_for_ref(self, prepared: dict, ref: str) -> dict | None:
        for file_info in prepared.get("files", []):
            if ref in {file_info.get("ref"), file_info.get("registry_id")}:
                return file_info
        return None

    def _forward_sender_name_key(self, value) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lstrip("@"))
        return text.casefold()

    def _add_forward_sender_identity(
        self,
        directory: dict,
        sender_id,
        sender_name,
        aliases=None,
    ) -> None:
        sender_id = str(sender_id or "").strip()
        sender_name = str(sender_name or sender_id).strip()
        if not sender_id:
            return
        identity = {"sender_id": sender_id, "sender_name": sender_name}
        directory["by_id"].setdefault(sender_id, identity)
        for alias in [sender_name, *(aliases or [])]:
            key = self._forward_sender_name_key(alias)
            if not key:
                continue
            existing = directory["by_name"].get(key, ...)
            if existing is ...:
                directory["by_name"][key] = identity
            elif existing and existing.get("sender_id") != sender_id:
                # Duplicate cards/nicknames cannot be mapped to a real QQ safely.
                directory["by_name"][key] = None

    def _source_group_id(self, event: AstrMessageEvent | None) -> str:
        if event is None:
            return ""
        try:
            group_id = str(event.get_group_id() or "").strip()
            if group_id:
                return group_id
        except Exception:
            pass
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            group_id = str(raw.get("group_id") or "").strip()
            if group_id:
                return group_id
        origin = self._event_key(event)
        marker = ":GroupMessage:"
        if marker in origin:
            return origin.rsplit(marker, 1)[-1].strip()
        return ""

    async def _build_forward_sender_directory(
        self,
        event: AstrMessageEvent,
        registry: dict[str, dict],
    ) -> dict:
        directory = {"by_id": {}, "by_name": {}}
        requester_id, requester_name = self._get_effective_requester(event)
        self._add_forward_sender_identity(directory, requester_id, requester_name)
        for entry in registry.values():
            self._add_forward_sender_identity(
                directory,
                entry.get("sender_id"),
                entry.get("sender_name"),
            )

        source_group_id = self._source_group_id(event)
        source_platform = str(
            getattr(event, "get_platform_id", lambda: "")() or ""
        ).strip()
        if not source_group_id or not source_platform:
            return directory
        try:
            member_data = await self._call_source_platform_action(
                event,
                source_platform,
                "get_group_member_list",
                group_id=(
                    int(source_group_id)
                    if source_group_id.isdigit()
                    else source_group_id
                ),
                no_cache=False,
            )
        except Exception as e:
            logger.debug(f"读取来源群成员以还原合并记录发送者失败: {e}")
            return directory
        members = self._unwrap_list_data(member_data)
        if not isinstance(members, list):
            return directory
        for member in members:
            if not isinstance(member, dict):
                continue
            sender_id = member.get("user_id") or member.get("uin") or member.get("id")
            card = str(member.get("card") or "").strip()
            nickname = str(member.get("nickname") or member.get("name") or "").strip()
            sender_name = card or nickname or sender_id
            self._add_forward_sender_identity(
                directory,
                sender_id,
                sender_name,
                aliases=[card, nickname],
            )
        return directory

    def _synthetic_forward_sender_id(
        self,
        sender_key: str,
        directory: dict,
    ) -> str:
        digest = hashlib.sha256(f"gossip-sharer-forward:{sender_key}".encode()).digest()
        value = 1_000_000_000 + int.from_bytes(digest[:8], "big") % 2_900_000_000
        reserved = set(directory.get("by_id", {}))
        while str(value) in reserved:
            value = 1_000_000_000 + (value - 999_999_999) % 2_900_000_000
        return str(value)

    def _match_forward_sender_identity(
        self,
        directory: dict,
        sender_name: str,
    ) -> dict | None:
        key = self._forward_sender_name_key(sender_name)
        if not key:
            return None
        exact = directory.get("by_name", {}).get(key)
        if exact:
            return exact
        if len(key) < 2:
            return None
        candidates = {}
        for alias, identity in directory.get("by_name", {}).items():
            if not identity or not alias:
                continue
            if key in alias or alias in key:
                candidates[identity["sender_id"]] = identity
        if len(candidates) == 1:
            return next(iter(candidates.values()))
        return None

    def _resolve_custom_forward_sender(
        self,
        item: dict,
        directory: dict,
        inherited_sender: dict | None,
        item_index: int,
    ) -> tuple[str, str]:
        explicit_id = str(
            item.get("sender_id")
            or item.get("sender_qq")
            or item.get("user_id")
            or item.get("uin")
            or ""
        ).strip()
        explicit_name = str(
            item.get("sender_name")
            or item.get("name")
            or item.get("nickname")
            or item.get("card")
            or ""
        ).strip()
        inherited_id = str((inherited_sender or {}).get("sender_id") or "").strip()
        inherited_name = str(
            (inherited_sender or {}).get("sender_name") or inherited_id
        ).strip()

        if explicit_id:
            known = directory.get("by_id", {}).get(explicit_id) or {}
            return explicit_id, explicit_name or known.get("sender_name") or explicit_id
        if inherited_id:
            return inherited_id, explicit_name or inherited_name or inherited_id

        lookup_name = explicit_name or inherited_name
        if lookup_name:
            matched = self._match_forward_sender_identity(directory, lookup_name)
            if matched:
                return matched["sender_id"], explicit_name or matched["sender_name"]

        sender_name = lookup_name or f"聊天记录节点 {item_index}"
        sender_key = lookup_name or f"item-{item_index}"
        return self._synthetic_forward_sender_id(sender_key, directory), sender_name

    def _build_custom_forward_item_node(
        self,
        event: AstrMessageEvent,
        item: dict,
        prepared: dict,
        sender_directory: dict,
        inherited_sender: dict | None = None,
        item_index: int = 1,
        default_time=None,
    ) -> tuple[dict | None, list[str]]:
        failures = []
        content = []
        at_names = self._normalize_at_names(item.get("at_names"))
        for index, qq in enumerate(self._normalize_at_qqs(item.get("at_qqs"))):
            content.append(
                {
                    "type": "at",
                    "data": {
                        "qq": str(qq),
                        "name": at_names[index] if index < len(at_names) else qq,
                    },
                }
            )
        text = str(item.get("text") or item.get("content") or "").strip()
        if text:
            content.append({"type": "text", "data": {"text": text}})

        for ref in self._normalize_attachment_refs(
            item.get("image_refs") or item.get("images")
        ):
            image = self._prepared_image_for_ref(prepared, ref)
            if not image:
                failures.append(f"整理节点中的图片 {ref} 未准备成功")
                continue
            content.append(
                {
                    "type": "image",
                    "data": {"file": f"base64://{image['base64']}"},
                }
            )

        for ref in self._normalize_attachment_refs(
            item.get("file_refs") or item.get("files")
        ):
            file_info = self._prepared_file_for_ref(prepared, ref)
            if not file_info:
                failures.append(f"整理节点中的文件 {ref} 未准备成功")
                continue
            content.append(
                {
                    "type": "file",
                    "data": {
                        "name": file_info["name"],
                        "file": file_info["path"],
                    },
                }
            )

        if not content:
            return None, failures or ["整理节点没有可发送内容"]

        sender_id, sender_name = self._resolve_custom_forward_sender(
            item,
            sender_directory,
            inherited_sender,
            item_index,
        )
        node_time = self._normalize_forward_time(
            item.get("time") or item.get("timestamp"), default_time
        )
        node_data = {
            "user_id": sender_id,
            "uin": sender_id,
            "nickname": sender_name,
            "name": sender_name,
            "content": content,
        }
        if node_time:
            node_data["time"] = node_time
        node = {
            "type": "node",
            "data": node_data,
        }
        return node, failures

    def _ensure_attachment_registry(
        self,
        event: AstrMessageEvent | None,
        provider_image_refs: list[str] | None = None,
    ) -> dict[str, dict]:
        """Build stable short references for current and quoted attachments.

        Args:
            event: Source message event whose attachments should be exposed.
            provider_image_refs: Additional image paths resolved by AstrBot core,
                including reply-ID-only quoted images.

        Returns:
            A mapping such as ``image_1`` or ``file_1`` to component metadata.
        """

        if event is None:
            return {}
        try:
            existing = event.get_extra(ATTACHMENT_REGISTRY_EXTRA, {})
        except Exception:
            existing = {}
        registry: dict[str, dict] = existing if isinstance(existing, dict) else {}
        counters = {
            "image": sum(
                1 for item in registry.values() if item.get("kind") == "image"
            ),
            "file": sum(1 for item in registry.values() if item.get("kind") == "file"),
        }

        def register(component, source: str) -> None:
            """Register one image or file component with a stable short ID.

            Args:
                component: AstrBot ``Image`` or ``File`` message component.
                source: Human-readable source such as current or quoted message.
            """

            kind = "image" if isinstance(component, Image) else "file"
            counters[kind] += 1
            ref_id = f"{kind}_{counters[kind]}"
            if isinstance(component, Image):
                raw_ref = str(
                    getattr(component, "path", None)
                    or getattr(component, "url", None)
                    or getattr(component, "file", None)
                    or ""
                ).strip()
                if raw_ref.startswith(("base64://", "data:")):
                    name = ref_id
                elif raw_ref.startswith(("http://", "https://")):
                    name = Path(urlparse(raw_ref).path).name or ref_id
                else:
                    name = Path(file_uri_to_path(raw_ref)).name if raw_ref else ref_id
            else:
                raw_ref = str(
                    getattr(component, "file_", None)
                    or getattr(component, "url", None)
                    or ""
                ).strip()
                name = str(getattr(component, "name", None) or "").strip()
                if not name and raw_ref:
                    name = Path(urlparse(raw_ref).path).name
                name = name or ref_id

            aliases = {ref_id, name}
            if raw_ref and len(raw_ref) <= 2048:
                aliases.add(raw_ref)
            aliases.discard("")
            registry[ref_id] = {
                "id": ref_id,
                "kind": kind,
                "component": component,
                "source": source,
                "name": name,
                "raw_ref": raw_ref,
                "aliases": aliases,
            }

        if not registry:
            messages = getattr(event, "get_messages", lambda: [])() or []
            for component in messages:
                if isinstance(component, Image | File):
                    register(component, "当前消息")
                elif isinstance(component, Reply) and component.chain:
                    for reply_component in component.chain:
                        if isinstance(reply_component, Image | File):
                            register(reply_component, "引用消息")

        known_image_aliases = {
            alias
            for item in registry.values()
            if item.get("kind") == "image"
            for alias in item.get("aliases", set())
        }
        represented_image_count = counters["image"]
        for index, image_ref in enumerate(provider_image_refs or []):
            if index < represented_image_count:
                continue
            image_ref = str(image_ref or "").strip()
            if not image_ref or image_ref in known_image_aliases:
                continue
            register(Image(file=image_ref), "消息或引用图片")
            known_image_aliases.add(image_ref)

        try:
            event.set_extra(ATTACHMENT_REGISTRY_EXTRA, registry)
        except Exception:
            pass
        return registry

    def _format_attachment_catalog(self, registry: dict[str, dict]) -> str:
        """Format attachment references for the source LLM.

        Args:
            registry: Attachment registry returned by ``_ensure_attachment_registry``.

        Returns:
            A compact system reminder, or an empty string when no attachments exist.
        """

        if not registry:
            return ""
        lines = [
            "[可携带到目标 QQ 会话的附件]",
            "只有确实需要发送时，才把下面的短引用传给 wake_qq_session_task。",
            "消息或引用图片必须使用 image_1 这类短引用；不要复用历史中的 /data/temp/media_image_* 临时路径。",
        ]
        for ref_id, item in registry.items():
            kind_name = "图片" if item["kind"] == "image" else "文件"
            lines.append(
                f"- {ref_id}: {kind_name}，{item['source']}，名称 {item['name']}"
            )
        return "\n".join(lines)

    def _normalize_attachment_refs(self, value) -> list[str]:
        """Normalize tool-provided attachment references without splitting spaces.

        Args:
            value: A list, JSON list string, or comma-separated string.

        Returns:
            Ordered unique non-empty attachment references.
        """

        refs = self._normalize_string_list(value, split_whitespace=False)
        return list(dict.fromkeys(refs))

    def _prepare_image_for_llm(self, encoded: str) -> tuple[str | None, bool, str]:
        """Convert GIF data to a static PNG for LLM-compatible recognition.

        Args:
            encoded: Raw base64 image data without a URI prefix.

        Returns:
            LLM-safe base64 data, whether the original is GIF, and an error message.
        """

        try:
            raw = base64.b64decode(encoded)
        except Exception as e:
            return None, False, f"base64 解码失败: {e}"
        if not raw.startswith((b"GIF87a", b"GIF89a")):
            return encoded, False, ""
        try:
            with PILImage.open(io.BytesIO(raw)) as image:
                image.seek(0)
                frame = image.convert("RGBA")
                output = io.BytesIO()
                frame.save(output, format="PNG")
            return base64.b64encode(output.getvalue()).decode(), True, ""
        except Exception as e:
            return None, True, f"GIF 首帧转换失败: {e}"

    def _allowed_attachment_paths(self) -> list[Path]:
        """Return local roots permitted for model-selected generated files.

        Returns:
            Resolved default and user-configured attachment roots.
        """

        roots = [Path(get_astrbot_temp_path()), Path(get_astrbot_workspaces_path())]
        roots.extend(Path(path).expanduser() for path in self.attachment_allowed_roots)
        resolved = []
        for root in roots:
            try:
                resolved.append(root.resolve())
            except OSError:
                continue
        return resolved

    def _validate_attachment_path(self, value: str, *, trusted: bool = False) -> Path:
        """Validate a model-selected local attachment path.

        Args:
            value: Plain local path or file URI.
            trusted: Whether the path came directly from the current platform event.

        Returns:
            The resolved existing file path.

        Raises:
            ValueError: If the file does not exist or is outside allowed roots.
        """

        local_value = file_uri_to_path(value) if is_file_uri(value) else value
        path = Path(local_value).expanduser().resolve()
        if not path.is_file():
            raise ValueError("文件不存在")
        if trusted:
            return path
        for root in self._allowed_attachment_paths():
            try:
                path.relative_to(root)
                return path
            except ValueError:
                continue
        raise ValueError("路径不在允许的附件目录中")

    def _find_attachment_entry(
        self, registry: dict[str, dict], ref: str, kind: str
    ) -> dict | None:
        """Find a registry entry by short ID or exact attachment alias.

        Args:
            registry: Current event attachment registry.
            ref: Tool-provided short ID, path, URL, or filename.
            kind: Required attachment kind, ``image`` or ``file``.

        Returns:
            The matching registry entry, if any.
        """

        direct = registry.get(ref)
        if direct and direct.get("kind") == kind:
            return direct
        for item in registry.values():
            if item.get("kind") == kind and ref in item.get("aliases", set()):
                return item
        return None

    async def _prepare_wake_attachments(
        self,
        event: AstrMessageEvent,
        image_refs,
        file_refs,
        embedded_image_refs=None,
        embedded_file_refs=None,
    ) -> dict:
        """Resolve selected images and files for delivery and target LLM context.

        Args:
            event: Source message event.
            image_refs: Model-selected standalone image references.
            file_refs: Model-selected standalone file references.
            embedded_image_refs: Image references embedded inside custom forward nodes.
            embedded_file_refs: File references embedded inside custom forward nodes.

        Returns:
            Prepared image payloads, file paths, cleanup paths, and failures.
        """

        registry = self._ensure_attachment_registry(event)
        images = []
        files = []
        failures = []
        cleanup_paths: list[Path] = []
        total_bytes = 0
        total_limit = self.max_wake_total_mb * 1024 * 1024

        standalone_image_refs = self._normalize_attachment_refs(image_refs)
        embedded_image_refs = self._normalize_attachment_refs(embedded_image_refs)
        normalized_images = list(
            dict.fromkeys([*standalone_image_refs, *embedded_image_refs])
        )
        if normalized_images and not self.enable_wake_images:
            failures.append("图片发送功能已在配置中关闭")
            normalized_images = []
        if len(normalized_images) > self.max_wake_images:
            failures.append(
                f"图片数量超过上限 {self.max_wake_images}，仅处理前 {self.max_wake_images} 张"
            )
            normalized_images = normalized_images[: self.max_wake_images]

        for ref in normalized_images:
            try:
                entry = self._find_attachment_entry(registry, ref, "image")
                if not entry:
                    image_entries = [
                        item
                        for item in registry.values()
                        if item.get("kind") == "image"
                    ]
                    is_stale_astrbot_temp = False
                    if not ref.startswith(
                        ("http://", "https://", "base64://", "data:")
                    ):
                        local_ref = file_uri_to_path(ref) if is_file_uri(ref) else ref
                        candidate = Path(local_ref).expanduser().resolve()
                        try:
                            candidate.relative_to(
                                Path(get_astrbot_temp_path()).resolve()
                            )
                            is_stale_astrbot_temp = candidate.name.startswith(
                                "media_image_"
                            )
                        except ValueError:
                            pass
                    if is_stale_astrbot_temp and len(image_entries) == 1:
                        entry = image_entries[0]
                        logger.info(
                            f"失效的临时图片引用 {ref} 已回退到本轮唯一图片 "
                            f"{entry['id']}"
                        )
                if entry:
                    component = entry["component"]
                    name = entry["name"]
                elif ref.startswith(("http://", "https://")):
                    if not self.allow_remote_attachment_urls:
                        raise ValueError("配置禁止直接使用远程附件 URL")
                    component = Image.fromURL(ref)
                    name = Path(urlparse(ref).path).name or "image"
                elif ref.startswith(("base64://", "data:")):
                    component = Image(file=ref)
                    name = "image"
                else:
                    path = self._validate_attachment_path(ref)
                    component = Image.fromFileSystem(str(path))
                    name = path.name

                encoded = (
                    entry.get("snapshot_base64") if entry else None
                ) or await component.convert_to_base64()
                size = len(encoded) * 3 // 4
                if size > self.max_wake_image_mb * 1024 * 1024:
                    raise ValueError(f"超过单张图片 {self.max_wake_image_mb} MB 限制")
                if total_bytes + size > total_limit:
                    raise ValueError(f"超过附件总大小 {self.max_wake_total_mb} MB 限制")
                total_bytes += size
                llm_encoded = entry.get("llm_snapshot_base64") if entry else None
                is_gif = bool(entry.get("is_gif")) if entry else False
                llm_error = str(entry.get("llm_snapshot_error") or "") if entry else ""
                if llm_encoded is None and not llm_error:
                    llm_encoded, is_gif, llm_error = self._prepare_image_for_llm(
                        encoded
                    )
                if llm_encoded and len(llm_encoded) * 3 // 4 > (
                    self.max_wake_image_mb * 1024 * 1024
                ):
                    llm_encoded = None
                    llm_error = "GIF 首帧 PNG 超过单张图片识别大小限制"
                if llm_error:
                    failures.append(
                        f"图片 {ref} 的 LLM 识别副本: {llm_error}；仍会发送原图"
                    )
                images.append(
                    {
                        "ref": ref,
                        "registry_id": entry.get("id") if entry else None,
                        "name": name,
                        "base64": encoded,
                        "llm_base64": llm_encoded,
                        "is_gif": is_gif,
                        "size": size,
                        "standalone": ref in standalone_image_refs,
                        "embedded": ref in embedded_image_refs,
                    }
                )
            except Exception as e:
                failures.append(f"图片 {ref}: {e}")

        standalone_file_refs = self._normalize_attachment_refs(file_refs)
        embedded_file_refs = self._normalize_attachment_refs(embedded_file_refs)
        normalized_files = list(
            dict.fromkeys([*standalone_file_refs, *embedded_file_refs])
        )
        if normalized_files and not self.enable_wake_files:
            failures.append("文件发送功能已在配置中关闭")
            normalized_files = []
        if len(normalized_files) > self.max_wake_files:
            failures.append(
                f"文件数量超过上限 {self.max_wake_files}，仅处理前 {self.max_wake_files} 个"
            )
            normalized_files = normalized_files[: self.max_wake_files]

        for ref in normalized_files:
            downloaded = False
            path: Path | None = None
            try:
                entry = self._find_attachment_entry(registry, ref, "file")
                if entry:
                    component = entry["component"]
                    name = entry["name"]
                    had_local_file = bool(getattr(component, "file_", None))
                    file_path = await component.get_file()
                    downloaded = (
                        bool(getattr(component, "url", None)) and not had_local_file
                    )
                    path = self._validate_attachment_path(file_path, trusted=True)
                elif ref.startswith(("http://", "https://")):
                    if not self.allow_remote_attachment_urls:
                        raise ValueError("配置禁止直接使用远程附件 URL")
                    name = Path(urlparse(ref).path).name or "file"
                    component = File(name=name, url=ref)
                    path = self._validate_attachment_path(
                        await component.get_file(), trusted=True
                    )
                    downloaded = True
                else:
                    path = self._validate_attachment_path(ref)
                    name = path.name

                size = path.stat().st_size
                if size > self.max_wake_file_mb * 1024 * 1024:
                    raise ValueError(f"超过单个文件 {self.max_wake_file_mb} MB 限制")
                if total_bytes + size > total_limit:
                    raise ValueError(f"超过附件总大小 {self.max_wake_total_mb} MB 限制")
                total_bytes += size
                mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                snapshot_dir = Path(get_astrbot_temp_path()) / "gossip_sharer_wake"
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                suffix = Path(name).suffix
                if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
                    suffix = ""
                snapshot_path = snapshot_dir / f"{uuid.uuid4().hex}{suffix}"
                shutil.copy2(path, snapshot_path)
                files.append(
                    {
                        "ref": ref,
                        "registry_id": entry.get("id") if entry else None,
                        "name": name,
                        "path": str(snapshot_path),
                        "size": size,
                        "mime_type": mime_type,
                        "standalone": ref in standalone_file_refs,
                        "embedded": ref in embedded_file_refs,
                    }
                )
                cleanup_paths.append(snapshot_path)
                if downloaded and path != snapshot_path:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as e:
                        logger.warning(f"清理附件下载缓存失败 {path}: {e}")
            except Exception as e:
                failures.append(f"文件 {ref}: {e}")
                if downloaded and path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

        return {
            "images": images,
            "files": files,
            "failures": failures,
            "cleanup_paths": cleanup_paths,
            "total_bytes": total_bytes,
        }

    async def _component_chain_to_onebot_segments(self, components) -> list[dict]:
        segments = []
        for component in components or []:
            if isinstance(component, Reply):
                continue
            if isinstance(component, Image):
                try:
                    encoded = await component.convert_to_base64()
                    segments.append(
                        {
                            "type": "image",
                            "data": {"file": f"base64://{encoded}"},
                        }
                    )
                except Exception as e:
                    logger.debug(f"合并记录节点图片转换失败，已跳过: {e}")
                continue
            if isinstance(component, Forward | Node | Nodes):
                continue
            try:
                to_dict = getattr(component, "to_dict", None)
                if callable(to_dict):
                    segment = await to_dict()
                else:
                    segment = component.toDict()
            except Exception as e:
                logger.debug(f"合并记录节点组件转换失败，已跳过: {e}")
                continue
            if isinstance(segment, dict):
                segments.append(segment)
        return segments

    async def _call_source_platform_action(
        self,
        event: AstrMessageEvent,
        platform_id: str,
        action: str,
        **kwargs,
    ):
        current_platform_id = str(
            getattr(event, "get_platform_id", lambda: "")() or ""
        ).strip()
        if platform_id and current_platform_id == platform_id:
            bot = getattr(event, "bot", None)
            for caller in (
                getattr(bot, "call_action", None),
                getattr(getattr(bot, "api", None), "call_action", None),
            ):
                if not callable(caller):
                    continue
                try:
                    result = await caller(action, **kwargs)
                    return self._unwrap_onebot_action_payload(result)
                except Exception as e:
                    logger.debug(f"通过来源事件调用 {action} 失败: {e}")
        return self._unwrap_onebot_action_payload(
            await self._call_platform_action(platform_id, action, **kwargs)
        )

    async def _forward_entry_nodes(
        self,
        event: AstrMessageEvent,
        entry: dict,
        target_platform: str,
    ) -> tuple[list[dict], list[dict], str, list[str]]:
        failures = []
        source_platform = str(
            entry.get("source_platform")
            or getattr(event, "get_platform_id", lambda: "")()
            or ""
        ).strip()
        raw_nodes = []

        forward_id = str(entry.get("forward_id") or "").strip()
        nodes_component = entry.get("nodes_component")
        if forward_id:
            payload = await self._call_source_platform_action(
                event,
                source_platform,
                "get_forward_msg",
                id=int(forward_id) if forward_id.isdigit() else forward_id,
            )
            raw_nodes = self._extract_forward_raw_nodes(payload)
            if not raw_nodes:
                return (
                    [],
                    [],
                    "",
                    [
                        f"{entry.get('id') or forward_id}: 获取合并聊天记录内容失败或记录已过期"
                    ],
                )
        elif nodes_component is not None:
            try:
                payload = await nodes_component.to_dict()
                raw_nodes = self._extract_forward_raw_nodes(payload)
            except Exception as e:
                return (
                    [],
                    [],
                    "",
                    [f"{entry.get('id') or '合并记录'}: 转换内联记录失败：{e}"],
                )
        elif entry.get("kind") == "message":
            message_id = str(entry.get("message_id") or "").strip()
            raw_segments = entry.get("raw_segments") or []
            if entry.get("contains_forward"):
                message_id = ""
                raw_segments = [
                    segment
                    for segment in raw_segments
                    if isinstance(segment, dict)
                    and str(segment.get("type") or "").lower()
                    not in {"forward", "forward_msg", "reply", "node", "nodes"}
                ]
            if not raw_segments and entry.get("component_chain"):
                raw_segments = await self._component_chain_to_onebot_segments(
                    entry.get("component_chain")
                )
            raw_node = {
                "message_id": message_id,
                "sender": {
                    "user_id": entry.get("sender_id") or "0",
                    "nickname": entry.get("sender_name")
                    or entry.get("sender_id")
                    or "聊天记录",
                },
                "message": raw_segments,
                "time": entry.get("time"),
            }
            raw_nodes = [raw_node]

        primary_nodes = []
        fallback_nodes = []
        for raw_node in raw_nodes:
            custom_node = self._custom_node_from_raw_message(raw_node)
            message_id = str(
                raw_node.get("message_id")
                or (
                    raw_node.get("data", {}).get("id")
                    if isinstance(raw_node.get("data"), dict)
                    else ""
                )
                or ""
            ).strip()
            if source_platform == target_platform and message_id:
                primary_nodes.append({"type": "node", "data": {"id": message_id}})
            elif custom_node:
                primary_nodes.append(custom_node)
            if custom_node:
                fallback_nodes.append(custom_node)

        if not primary_nodes:
            failures.append(
                f"{entry.get('id') or forward_id or '消息'}: 没有可构造的转发节点"
            )
        return primary_nodes, fallback_nodes, self._forward_preview(raw_nodes), failures

    async def _prepare_wake_forwards(
        self,
        event: AstrMessageEvent,
        target_platform: str,
        task: str,
        forward_refs,
        forward_items,
        prepared_attachments: dict,
    ) -> dict:
        refs = self._normalize_attachment_refs(forward_refs)
        items = self._normalize_forward_items(forward_items)
        if not refs and not items:
            return {"forwards": [], "failures": []}
        if not self.enable_wake_forwards:
            return {
                "forwards": [],
                "failures": ["合并聊天记录发送功能已在配置中关闭"],
            }
        if self.max_wake_forwards <= 0:
            return {
                "forwards": [],
                "failures": ["合并聊天记录发送数量上限为 0"],
            }
        if len(refs) > self.max_wake_forwards:
            refs = refs[: self.max_wake_forwards]
            limit_failure = (
                f"已有合并记录/消息引用超过上限 {self.max_wake_forwards}，"
                f"仅处理前 {self.max_wake_forwards} 项"
            )
        else:
            limit_failure = ""

        registry = self._ensure_forward_registry(event)
        task_mentions_media = bool(
            re.search(
                r"图片|截图|表情包|表情|贴纸|动图|照片|相片|梗图|发图|看图|"
                r"这张|那张|图里|图中|gif|meme",
                str(task or ""),
                re.IGNORECASE,
            )
        )
        if refs and not items and not task_mentions_media:
            selected_entries = []
            selected_canonical_refs = set()
            pure_media_selection = True
            for ref in refs:
                entry = self._find_forward_entry(registry, ref)
                if not entry or entry.get("kind") != "message":
                    pure_media_selection = False
                    break
                preview = str(entry.get("preview") or "").strip()
                remaining_preview = re.sub(
                    r"\[(?:图片|表情)(?::[^\]]*)?\]|\[回复消息\]",
                    "",
                    preview,
                ).strip()
                if not preview or remaining_preview:
                    pure_media_selection = False
                    break
                selected_entries.append(entry)
                selected_canonical_refs.add(str(entry.get("id") or ref))

            if pure_media_selection and selected_entries:
                task_text = str(task or "").casefold()
                task_compact = re.sub(r"\s+", "", task_text)
                task_cjk = "".join(re.findall(r"[\u4e00-\u9fff]", task_text))
                task_words = set(re.findall(r"[a-z0-9_]{2,}", task_text))
                registry_items = list(registry.items())
                selected_positions = [
                    index
                    for index, (ref_id, _) in enumerate(registry_items)
                    if ref_id in selected_canonical_refs
                ]
                candidates = []
                for index, (ref_id, entry) in enumerate(registry_items):
                    if (
                        ref_id in selected_canonical_refs
                        or entry.get("kind") != "message"
                    ):
                        continue
                    preview = str(entry.get("preview") or "").strip()
                    textual_preview = re.sub(
                        r"\[(?:图片|表情)(?::[^\]]*)?\]|\[回复消息\]",
                        "",
                        preview,
                    ).strip()
                    if not textual_preview:
                        continue

                    candidate_text = textual_preview.casefold()
                    candidate_compact = re.sub(r"\s+", "", candidate_text)
                    content_score = 0
                    if (
                        len(candidate_compact) >= 2
                        and candidate_compact in task_compact
                    ):
                        content_score += 4
                    candidate_cjk = "".join(
                        re.findall(r"[\u4e00-\u9fff]", candidate_text)
                    )
                    candidate_bigrams = {
                        candidate_cjk[offset : offset + 2]
                        for offset in range(max(len(candidate_cjk) - 1, 0))
                    }
                    content_score += sum(
                        1 for token in candidate_bigrams if token in task_cjk
                    )
                    candidate_words = set(re.findall(r"[a-z0-9_]{2,}", candidate_text))
                    content_score += len(candidate_words & task_words)
                    if content_score <= 0:
                        continue
                    score = content_score

                    sender_name = str(entry.get("sender_name") or "").casefold()
                    sender_cjk = "".join(re.findall(r"[\u4e00-\u9fff]", sender_name))
                    sender_bigrams = {
                        sender_cjk[offset : offset + 2]
                        for offset in range(max(len(sender_cjk) - 1, 0))
                    }
                    if any(token in task_cjk for token in sender_bigrams):
                        score += 2
                    sender_words = set(re.findall(r"[a-z0-9_]{2,}", sender_name))
                    if sender_words & task_words:
                        score += 2
                    distance = (
                        min(abs(index - position) for position in selected_positions)
                        if selected_positions
                        else len(registry_items)
                    )
                    candidates.append((score, distance, index, ref_id))

                candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
                remaining_slots = max(self.max_wake_forwards - len(refs), 0)
                auto_added = candidates[: min(3, remaining_slots)]
                auto_added.sort(key=lambda item: item[2])
                if auto_added:
                    added_refs = [item[3] for item in auto_added]
                    refs.extend(added_refs)
                    logger.info(
                        "合并记录引用只命中纯图片/表情且任务未要求媒体，"
                        f"已按任务内容补入相关文字消息: {added_refs}"
                    )

        primary_nodes = []
        fallback_nodes = []
        previews = []
        failures = [limit_failure] if limit_failure else []
        selected_refs = []
        source_ref_count = 0
        successful_source_count = 0
        source_ref_limit_reported = False
        source_is_group = bool(self._source_group_id(event))
        force_group_source = False
        has_constructed_nodes = False
        base_forward_time = int(
            self._normalize_forward_time(self._event_timestamp(event), time.time())
        )

        async def append_registry_ref(ref: str) -> dict | None:
            nonlocal force_group_source, has_constructed_nodes
            nonlocal source_ref_count, successful_source_count
            nonlocal source_ref_limit_reported
            if source_ref_count >= self.max_wake_forwards:
                if not source_ref_limit_reported:
                    failures.append(
                        f"合并记录来源超过上限 {self.max_wake_forwards}，"
                        "已忽略超出的来源"
                    )
                    source_ref_limit_reported = True
                return None
            entry = self._find_forward_entry(registry, ref)
            if not entry:
                failures.append(f"合并记录来源 {ref}: 当前消息中不存在此引用")
                return None
            source_ref_count += 1
            if source_is_group and entry.get("kind") == "message":
                force_group_source = True
            if entry.get("kind") == "message":
                has_constructed_nodes = True
            nodes, fallback, preview, entry_failures = await self._forward_entry_nodes(
                event, entry, target_platform
            )
            primary_nodes.extend(nodes)
            fallback_nodes.extend(fallback)
            failures.extend(entry_failures)
            if preview:
                previews.append(preview)
            if nodes:
                selected_refs.append(entry.get("id") or ref)
                successful_source_count += 1
            return entry

        needs_sender_directory = any(
            any(
                item.get(key)
                for key in (
                    "text",
                    "content",
                    "image_refs",
                    "images",
                    "file_refs",
                    "files",
                    "at_qqs",
                )
            )
            and not any(
                item.get(key) for key in ("sender_id", "sender_qq", "user_id", "uin")
            )
            for item in items
        )
        sender_directory = (
            await self._build_forward_sender_directory(event, registry)
            if needs_sender_directory
            else {"by_id": {}, "by_name": {}}
        )

        for ref in refs:
            await append_registry_ref(ref)

        for item_index, item in enumerate(items, 1):
            item_refs = self._normalize_attachment_refs(
                item.get("ref")
                or item.get("message_ref")
                or item.get("forward_ref")
                or item.get("refs")
            )
            inherited_sender = None
            for ref in item_refs:
                entry = await append_registry_ref(ref)
                if inherited_sender is None and entry:
                    inherited_sender = {
                        "sender_id": entry.get("sender_id"),
                        "sender_name": entry.get("sender_name"),
                    }

            sender_refs = self._normalize_attachment_refs(item.get("sender_ref"))
            if sender_refs and inherited_sender is None:
                sender_entry = self._find_forward_entry(registry, sender_refs[0])
                if sender_entry:
                    inherited_sender = {
                        "sender_id": sender_entry.get("sender_id"),
                        "sender_name": sender_entry.get("sender_name"),
                    }
                else:
                    failures.append(
                        f"发送者来源 {sender_refs[0]}: 当前消息中不存在此引用"
                    )

            has_custom_content = any(
                item.get(key)
                for key in (
                    "text",
                    "content",
                    "image_refs",
                    "images",
                    "file_refs",
                    "files",
                    "at_qqs",
                )
            )
            if has_custom_content:
                has_constructed_nodes = True
                if source_is_group:
                    force_group_source = True
                node, item_failures = self._build_custom_forward_item_node(
                    event,
                    item,
                    prepared_attachments,
                    sender_directory,
                    inherited_sender=inherited_sender,
                    item_index=item_index,
                    default_time=(base_forward_time - max(len(items) - item_index, 0)),
                )
                failures.extend(item_failures)
                if node:
                    primary_nodes.append(node)
                    fallback_nodes.append(copy.deepcopy(node))
                    selected_refs.append("自定义节点")

        # A single existing forward card is kept as close to its native shape as
        # possible. Combining multiple source cards creates a new record and
        # therefore needs stable metadata just like message/custom-node records.
        if successful_source_count > 1:
            has_constructed_nodes = True
            if source_is_group:
                force_group_source = True

        if len(primary_nodes) > self.max_forward_nodes:
            failures.append(
                f"合并记录共有 {len(primary_nodes)} 个节点，超过上限 "
                f"{self.max_forward_nodes}，本次不发送合并记录"
            )
            primary_nodes = []
            fallback_nodes = []

        forwards = []
        if primary_nodes:
            source_title = ""
            news = (
                self._forward_card_news(primary_nodes, fallback_nodes)
                if has_constructed_nodes
                else []
            )
            summary = ""
            prompt = ""
            if has_constructed_nodes:
                source_title = self._forward_card_source(
                    primary_nodes,
                    fallback_nodes,
                    is_group_record=force_group_source,
                )
                summary = f"查看{len(primary_nodes)}条转发消息"
                prompt = "[聊天记录]"
            combined_previews = list(previews)
            for item in news:
                text = str(item.get("text") or "").strip()
                if text and text not in combined_previews:
                    combined_previews.append(text)
            forwards.append(
                {
                    "nodes": primary_nodes,
                    "fallback_nodes": fallback_nodes,
                    "node_count": len(primary_nodes),
                    "refs": selected_refs,
                    "preview": "\n".join(combined_previews),
                    "source": source_title,
                    "news": news,
                    "summary": summary,
                    "prompt": prompt,
                }
            )
        return {"forwards": forwards, "failures": failures}

    def _onebot_action_succeeded(self, result) -> bool:
        if result is False:
            return False
        if not isinstance(result, dict):
            return result is not None
        if result.get("status") in {"failed", "error"}:
            return False
        retcode = result.get("retcode")
        if retcode not in (None, 0, "0"):
            return False
        return True

    async def _send_wake_forwards(
        self,
        target_type: str,
        target_id: str,
        target_platform: str,
        prepared: dict,
    ) -> bool:
        forwards = prepared.get("forwards", [])
        if not forwards:
            return True
        platform = self._get_platform_by_id(target_platform)
        bot = getattr(platform, "bot", None) if platform is not None else None
        caller = getattr(bot, "call_action", None)
        if not callable(caller):
            caller = getattr(getattr(bot, "api", None), "call_action", None)
        if not callable(caller):
            return False

        action = (
            "send_group_forward_msg"
            if target_type == "GroupMessage"
            else "send_private_forward_msg"
        )
        target_key = "group_id" if target_type == "GroupMessage" else "user_id"
        target_value = int(target_id) if str(target_id).isdigit() else target_id
        for package in forwards:

            def build_action_kwargs(nodes: list[dict]) -> dict:
                kwargs = {target_key: target_value, "messages": nodes}
                for key in ("source", "news", "summary", "prompt"):
                    value = package.get(key)
                    if value:
                        kwargs[key] = value
                return kwargs

            primary_nodes = package.get("nodes") or []
            original_id_nodes = sum(
                1
                for node in primary_nodes
                if isinstance(node, dict)
                and isinstance(node.get("data"), dict)
                and node["data"].get("id")
            )
            if original_id_nodes == len(primary_nodes):
                primary_mode = "原消息ID节点"
            elif original_id_nodes:
                primary_mode = "原消息ID与自定义节点混合"
            else:
                primary_mode = "自定义节点"
            logger.info(
                f"准备投递 QQ 合并记录: target={target_type}:{target_id}, "
                f"mode={primary_mode}, nodes={len(primary_nodes)}"
            )
            try:
                result = await caller(
                    action,
                    **build_action_kwargs(primary_nodes),
                )
                if self._onebot_action_succeeded(result):
                    logger.info(
                        f"QQ 合并记录投递成功: target={target_type}:{target_id}, "
                        f"mode={primary_mode}, nodes={len(primary_nodes)}"
                    )
                    continue
                raise RuntimeError(f"平台返回失败结果: {result}")
            except Exception as primary_error:
                fallback_nodes = package.get("fallback_nodes") or []
                if (
                    not fallback_nodes
                    or len(fallback_nodes) != len(package.get("nodes") or [])
                    or fallback_nodes == package.get("nodes")
                ):
                    logger.warning(
                        f"目标 QQ 合并记录投递失败: {primary_error}",
                        exc_info=True,
                    )
                    return False
                logger.info(
                    "按原消息 ID 投递合并记录失败，改用已保存的自定义节点重试；"
                    "QQ PC 端可能无法完整显示自定义节点的头像昵称"
                )
                try:
                    result = await caller(
                        action,
                        **build_action_kwargs(fallback_nodes),
                    )
                    if not self._onebot_action_succeeded(result):
                        raise RuntimeError(f"平台返回失败结果: {result}")
                    logger.info(
                        f"QQ 合并记录降级投递成功: target={target_type}:{target_id}, "
                        f"mode=自定义节点, nodes={len(fallback_nodes)}"
                    )
                except Exception as fallback_error:
                    logger.warning(
                        f"目标 QQ 合并记录自定义节点降级投递失败: {fallback_error}",
                        exc_info=True,
                    )
                    return False
        return True

    async def _send_wake_attachments(self, session_id: str, prepared: dict) -> bool:
        """Send selected attachments to the visible target QQ session.

        Args:
            session_id: Unified target session ID.
            prepared: Result returned by ``_prepare_wake_attachments``.

        Returns:
            Whether the platform accepted the attachment message chain.
        """

        chain = MessageChain()
        for image in prepared.get("images", []):
            if image.get("standalone", True):
                chain.base64_image(image["base64"])
        for file_info in prepared.get("files", []):
            if file_info.get("standalone", True):
                chain.chain.append(File(name=file_info["name"], file=file_info["path"]))
        if not chain.chain:
            return True
        return bool(await self.context.send_message(session_id, chain))

    async def _send_wake_payloads(self, session_id: str, prepared: dict) -> bool:
        """Send native merged records first, then standalone attachments."""

        target_type = str(prepared.get("target_type") or "GroupMessage")
        target_id = str(prepared.get("target_id") or "").strip()
        target_platform = str(prepared.get("target_platform") or "").strip()
        forwards_ok = await self._send_wake_forwards(
            target_type,
            target_id,
            target_platform,
            prepared,
        )
        attachments_ok = await self._send_wake_attachments(session_id, prepared)
        return forwards_ok and attachments_ok

    def _format_wake_attachment_summary(
        self, prepared: dict, *, delivered: bool | None
    ) -> str:
        """Describe attachment delivery results for the target LLM and tool caller.

        Args:
            prepared: Result returned by ``_prepare_wake_attachments``.
            delivered: Whether the visible target message was accepted. ``None``
                means delivery is queued until after the target LLM reply.

        Returns:
            A concise multiline attachment status description.
        """

        lines = []
        images = prepared.get("images", [])
        files = prepared.get("files", [])
        forwards = prepared.get("forwards", [])
        if images or files or forwards:
            if delivered is None:
                delivery_text = "将在本次回复发送完成后投递到目标会话"
            else:
                delivery_text = (
                    "已发送到目标会话" if delivered else "未能发送到目标会话"
                )
            lines.append(f"跨会话内容投递状态: {delivery_text}")
        if delivered is not False:
            for image in images:
                if image.get("standalone", True):
                    recognition_note = (
                        "，目标 LLM 使用首帧 PNG 识别" if image.get("is_gif") else ""
                    )
                    lines.append(
                        f"- 图片: {image['name']} ({image['size'] / 1024 / 1024:.2f} MB{recognition_note})"
                    )
                elif image.get("embedded"):
                    lines.append(f"- 合并记录内图片: {image['name']}")
            for file_info in files:
                if file_info.get("standalone", True):
                    lines.append(
                        f"- 文件: {file_info['name']}，{file_info['mime_type']} "
                        f"({file_info['size'] / 1024 / 1024:.2f} MB)"
                    )
                elif file_info.get("embedded"):
                    lines.append(f"- 合并记录内文件: {file_info['name']}")
            for forward in forwards:
                preview = str(forward.get("preview") or "").strip()
                lines.append(f"- 合并聊天记录: {forward.get('node_count', 0)} 个节点")
                if preview:
                    lines.append(f"  预览: {preview[:600]}")
        for failure in prepared.get("failures", []):
            lines.append(f"- 内容准备失败: {failure}")
        return "\n".join(lines)

    def _effective_target_platform_id(
        self,
        event: AstrMessageEvent | None = None,
        target_platform: str | None = None,
    ) -> str:
        platform_id = str(target_platform or self.default_platform or "").strip()
        if platform_id or event is None:
            return platform_id

        try:
            if event.get_platform_name() == "aiocqhttp":
                return str(event.get_platform_id() or "").strip()
        except Exception:
            pass
        return ""

    def _format_at_note(
        self, at_qqs: list[str] | None = None, at_all: bool = False
    ) -> str:
        mentions = []
        if at_all:
            mentions.append("@全体成员")
        mentions.extend([f"@{qq}" for qq in at_qqs or []])
        return ", ".join(mentions)

    def _build_message_chain(
        self,
        content: str = "",
        image_url: str | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
        at_qqs: list[str] | None = None,
        at_names: list[str] | None = None,
        at_all: bool = False,
    ) -> MessageChain:
        chain = MessageChain()
        if at_all:
            chain.at_all()
        at_names = at_names or []
        for idx, qq in enumerate(at_qqs or []):
            name = at_names[idx] if idx < len(at_names) else qq
            chain.at(name, qq)
        if content:
            chain.message(content)
        if image_url:
            chain.url_image(image_url.strip())
        if image_path:
            chain.file_image(image_path.strip())
        if image_base64:
            data = image_base64.strip()
            if data.startswith("data:image/") and "," in data:
                data = data.split(",", 1)[1]
            if data.startswith("base64://"):
                data = data.removeprefix("base64://")
            chain.base64_image(data)
        return chain

    def _build_bridge_history_pair(
        self,
        event: AstrMessageEvent,
        session_id: str,
        content: str,
        image_url: str | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
        at_qqs: list[str] | None = None,
        at_all: bool = False,
    ) -> tuple[dict, dict]:
        source_session = getattr(event, "session", None) or getattr(
            event, "unified_msg_origin", "未知会话"
        )
        source_platform = getattr(event, "get_platform_id", lambda: "未知平台")()
        source_sender = (
            getattr(event, "get_sender_name", lambda: None)()
            or getattr(event, "get_sender_id", lambda: None)()
            or "未知发送者"
        )
        image_notes = self._build_image_context_notes(
            image_url, image_path, image_base64
        )
        at_note = self._format_at_note(at_qqs, at_all)
        target_note = f"目标会话: {session_id}"
        if at_note:
            target_note += f"\n目标提及: {at_note}"
        image_note = ""
        if image_notes:
            image_note += "\n" + "\n".join(image_notes)
        bridge_text = (
            f"[跨会话转入]\n"
            f"来源会话: {source_session}\n"
            f"来源平台: {source_platform}\n"
            f"来源发送者: {source_sender}\n"
            f"{target_note}{image_note}\n"
            f"转发内容:\n{content or '[无文字内容]'}"
        )
        user_message = {
            "role": "user",
            "content": bridge_text,
        }
        assistant_message = {
            "role": "assistant",
            "content": (
                "我已收到这条来自其他会话的转述消息。"
                "后续如果当前会话有人回复，应把它理解为对上面这条转述内容的继续回应，而不是一条完全无上下文的新话题。"
            ),
        }
        return user_message, assistant_message

    def _is_synthetic_event(self, event: AstrMessageEvent | None) -> bool:
        if event is None:
            return False
        try:
            return bool(event.get_extra(SYNTHETIC_EVENT_EXTRA, False))
        except Exception:
            return False

    async def _resolve_qq_self_id(self, platform) -> str:
        bot = getattr(platform, "bot", None)
        caller = getattr(bot, "call_action", None)
        if callable(caller):
            try:
                info = await caller("get_login_info")
                if isinstance(info, dict):
                    data = (
                        info.get("data") if isinstance(info.get("data"), dict) else info
                    )
                    self_id = data.get("user_id") or data.get("self_id")
                    if self_id:
                        return str(self_id)
            except Exception as e:
                logger.debug(f"获取 QQ self_id 失败，使用平台 ID 兜底: {e}")

        try:
            platform_id = platform.meta().id
            if platform_id:
                return str(platform_id)
        except Exception:
            pass
        return str(self.default_platform or "")

    async def _persist_cross_context(
        self,
        event: AstrMessageEvent,
        session_id: str,
        content: str,
        image_url: str | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
        at_qqs: list[str] | None = None,
        at_all: bool = False,
    ) -> None:
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            logger.warning(
                "当前 Context 未提供 conversation_manager，跳过目标会话上下文注入"
            )
            return

        cid = await conv_mgr.get_curr_conversation_id(session_id)
        if not cid:
            parts = session_id.split(":", 2)
            platform_id = parts[0] if len(parts) >= 3 else None
            cid = await conv_mgr.new_conversation(session_id, platform_id=platform_id)

        user_message, assistant_message = self._build_bridge_history_pair(
            event,
            session_id,
            content,
            image_url,
            image_path,
            image_base64,
            at_qqs,
            at_all,
        )
        await conv_mgr.add_message_pair(cid, user_message, assistant_message)
        logger.info(
            f"已将跨会话转发内容写入目标上下文: session={session_id}, cid={cid}"
        )

    async def _safe_send(
        self,
        event: AstrMessageEvent,
        target_type: str,
        target_id: str,
        content: str = "",
        target_platform: str = None,
        image_url: str | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
        at_qqs=None,
        at_names=None,
        at_all: bool = False,
    ) -> str:
        original_event = event
        event = self._unwrap_message_event(event)
        if event is None:
            return (
                "发送失败：无法从工具上下文识别当前来源事件。"
                f"收到的对象类型：{self._describe_event_like(original_event)}。"
            )

        target_id = str(target_id).strip()
        content = str(content or "")
        image_url = str(image_url).strip() if image_url else None
        image_path = str(image_path).strip() if image_path else None
        image_base64 = str(image_base64).strip() if image_base64 else None
        at_qq_list = self._normalize_at_qqs(at_qqs)
        at_name_list = self._normalize_at_names(at_names)
        at_all_enabled = self._normalize_bool(at_all)

        error = self._validate_target(target_type, target_id, target_platform)
        if error:
            return error
        if (at_qq_list or at_all_enabled) and target_type != "GroupMessage":
            return "发送失败：at_qqs/at_all 仅支持 GroupMessage 目标。"
        if (
            not content
            and not image_url
            and not image_path
            and not image_base64
            and not at_qq_list
            and not at_all_enabled
        ):
            return "发送失败：content、image_url、image_path、image_base64、at_qqs、at_all 不能全部为空。"

        session_id = self._build_session_id(target_type, target_id, target_platform)
        if not session_id:
            return "发送失败：未配置默认平台 ID，请先配置 default_platform 或传入 target_platform。"

        try:
            chain = self._build_message_chain(
                content,
                image_url,
                image_path,
                image_base64,
                at_qq_list,
                at_name_list,
                at_all_enabled,
            )
        except Exception as e:
            return f"发送失败：构造消息链失败：{e}"

        sent = await self.context.send_message(session_id, chain)
        if not sent:
            return f"发送失败：未找到目标平台，session={session_id}"

        try:
            await self._persist_cross_context(
                event,
                session_id,
                content,
                image_url,
                image_path,
                image_base64,
                at_qq_list,
                at_all_enabled,
            )
        except Exception as e:
            logger.warning(f"消息已发出，但写入目标会话上下文失败: {e}")

        self._reset_no_share_count(event)
        return session_id

    async def _call_possible_async(self, method):
        result = method()
        if hasattr(result, "__await__"):
            result = await result
        return result

    async def _try_get_group_list(self, event: AstrMessageEvent | None = None):
        candidates = []

        if event is not None:
            bot = getattr(event, "bot", None)
            if bot is not None:
                candidates.append(getattr(bot, "get_group_list", None))

        candidates.extend(
            [
                getattr(self.context, "get_group_list", None),
                getattr(
                    getattr(self.context, "platform", None), "get_group_list", None
                ),
                getattr(
                    getattr(self.context, "provider", None), "get_group_list", None
                ),
                getattr(getattr(self.context, "adapter", None), "get_group_list", None),
                getattr(getattr(self.context, "client", None), "get_group_list", None),
            ]
        )

        for method in candidates:
            if not callable(method):
                continue
            try:
                result = await self._call_possible_async(method)
                if result is not None:
                    return result
            except Exception as e:
                logger.debug(f"尝试获取群列表失败: {e}")

        return None

    async def _try_get_friend_list(self, event: AstrMessageEvent | None = None):
        candidates = []

        if event is not None:
            bot = getattr(event, "bot", None)
            if bot is not None:
                candidates.append(getattr(bot, "get_friend_list", None))

        candidates.extend(
            [
                getattr(self.context, "get_friend_list", None),
                getattr(
                    getattr(self.context, "platform", None), "get_friend_list", None
                ),
                getattr(
                    getattr(self.context, "provider", None), "get_friend_list", None
                ),
                getattr(
                    getattr(self.context, "adapter", None), "get_friend_list", None
                ),
                getattr(getattr(self.context, "client", None), "get_friend_list", None),
            ]
        )

        for method in candidates:
            if not callable(method):
                continue
            try:
                result = await self._call_possible_async(method)
                if result is not None:
                    return result
            except Exception as e:
                logger.debug(f"尝试获取好友列表失败: {e}")

        return None

    def _get_platform_by_id(self, platform_id: str):
        platform_mgr = getattr(self.context, "platform_manager", None)
        platforms = getattr(platform_mgr, "platform_insts", []) or []
        for platform in platforms:
            try:
                if platform.meta().id == platform_id:
                    return platform
            except Exception:
                continue
        return None

    async def _call_platform_action(self, platform_id: str, action: str, **kwargs):
        platform = self._get_platform_by_id(platform_id)
        if platform is None:
            return None
        bot = getattr(platform, "bot", None)
        if bot is None:
            return None

        for caller in (
            getattr(bot, "call_action", None),
            getattr(getattr(bot, "api", None), "call_action", None),
        ):
            if not callable(caller):
                continue
            try:
                result = await caller(action, **kwargs)
                if (
                    isinstance(result, dict)
                    and "data" in result
                    and any(
                        key in result for key in ("retcode", "status", "msg", "wording")
                    )
                ):
                    return result.get("data")
                return result
            except Exception as e:
                logger.debug(f"调用平台动作 {action} 失败: {e}")
        return None

    async def _try_get_target_group_members(
        self,
        target_id: str,
        target_platform: str | None = None,
    ):
        platform_id = str(target_platform or self.default_platform).strip()
        if not platform_id:
            return None
        return await self._call_platform_action(
            platform_id,
            "get_group_member_list",
            group_id=int(target_id) if str(target_id).isdigit() else target_id,
            no_cache=True,
        )

    def _unwrap_list_data(self, data):
        if isinstance(data, dict):
            for key in ("data", "groups", "friends", "list", "result"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return data

    def _format_group_list(self, group_data) -> str:
        if not group_data:
            return ""

        group_data = self._unwrap_list_data(group_data)

        if not isinstance(group_data, list):
            return f"已获取群列表信息，但数据结构暂不支持直接展示：{type(group_data).__name__}"

        if not group_data:
            return "当前群列表为空。"

        lines = []
        whitelist_set = set(self.group_whitelist)
        for item in group_data[:50]:
            if isinstance(item, dict):
                gid = (
                    item.get("group_id")
                    or item.get("group_code")
                    or item.get("id")
                    or "未知群号"
                )
                group_name = (
                    item.get("group_name")
                    or item.get("group_remark")
                    or item.get("name")
                    or "未知群名"
                )
                status = " [白名单可转发]" if str(gid) in whitelist_set else ""
                lines.append(f"- {group_name} ({gid}){status}")
            else:
                lines.append(f"- {str(item)}")

        extra = ""
        if len(group_data) > 50:
            extra = f"\n仅展示前 50 项，共 {len(group_data)} 项。"

        return "Bot 当前可感知到的群列表：\n" + "\n".join(lines) + extra

    def _format_friend_list(self, friend_data) -> str:
        if not friend_data:
            return ""

        friend_data = self._unwrap_list_data(friend_data)

        if not isinstance(friend_data, list):
            return f"已获取好友信息，但数据结构暂不支持直接展示：{type(friend_data).__name__}"

        if not friend_data:
            return "当前好友列表为空。"

        lines = []
        for item in friend_data[:50]:
            if isinstance(item, dict):
                uid = (
                    item.get("user_id")
                    or item.get("uin")
                    or item.get("qq")
                    or item.get("id")
                    or "未知ID"
                )
                nickname = (
                    item.get("nickname")
                    or item.get("remark")
                    or item.get("card")
                    or item.get("name")
                    or "未知昵称"
                )
                if str(uid) == self.sister_qq:
                    mark = " [姐姐/默认可转发]"
                elif self.enable_arbitrary_friend_targets:
                    mark = " [可转发]"
                else:
                    mark = ""
                lines.append(f"- {nickname} ({uid}){mark}")
            else:
                lines.append(f"- {str(item)}")

        extra = ""
        if len(friend_data) > 50:
            extra = f"\n仅展示前 50 项，共 {len(friend_data)} 项。"

        return "Bot 当前可感知到的好友列表：\n" + "\n".join(lines) + extra

    def _format_target_group_members(
        self, member_data, keyword: str = "", limit: int = 50
    ) -> str:
        if not member_data:
            return ""

        member_data = self._unwrap_list_data(member_data)
        if not isinstance(member_data, list):
            return f"已获取群成员信息，但数据结构暂不支持直接展示：{type(member_data).__name__}"
        if not member_data:
            return "目标群成员列表为空。"

        keyword = str(keyword or "").strip().lower()
        limit = max(1, min(int(limit or 50), 200))

        filtered = []
        for item in member_data:
            if not isinstance(item, dict):
                text = str(item)
                if not keyword or keyword in text.lower():
                    filtered.append(item)
                continue
            uid = str(
                item.get("user_id")
                or item.get("uin")
                or item.get("qq")
                or item.get("id")
                or ""
            )
            nickname = str(item.get("nickname") or item.get("name") or "")
            card = str(item.get("card") or item.get("card_name") or "")
            alias = card or nickname or "未知昵称"
            haystack = f"{uid} {nickname} {card}".lower()
            if not keyword or keyword in haystack:
                filtered.append({**item, "_uid": uid, "_alias": alias})

        if not filtered:
            return f"没有找到匹配 `{keyword}` 的目标群成员。"

        lines = []
        for item in filtered[:limit]:
            if isinstance(item, dict):
                uid = (
                    item.get("_uid")
                    or item.get("user_id")
                    or item.get("uin")
                    or item.get("qq")
                    or item.get("id")
                    or "未知ID"
                )
                alias = (
                    item.get("_alias")
                    or item.get("card")
                    or item.get("nickname")
                    or item.get("name")
                    or "未知昵称"
                )
                role = item.get("role") or ""
                role_note = f" [{role}]" if role else ""
                lines.append(f"- {alias} ({uid}){role_note}")
            else:
                lines.append(f"- {str(item)}")

        extra = ""
        if len(filtered) > limit:
            extra = f"\n仅展示前 {limit} 项，匹配 {len(filtered)} 项。"

        return "目标群成员列表：\n" + "\n".join(lines) + extra

    def _build_guarantee_prompt(self, count: int) -> str:
        target_hint = (
            f"姐姐({self.sister_qq})不是默认收件箱；只有内容与她本人、她关心的人或"
            "正在进行的事情直接相关，而且确实值得打断她时，才考虑联系她。"
            if self.sister_qq
            else "只有内容与某个目标直接相关且确实值得打断对方时，才考虑联系该会话。"
        )
        return (
            "[插件临时上下文｜非用户原话]\n"
            "以下内容仅用于帮助你自主判断，不代表当前用户提出了转发要求，"
            "也不要向用户复述本段内容。\n"
            "[主动社交提醒]\n"
            f"当前会话已经连续 {count} 次 LLM 请求没有发起跨会话行动。"
            "这只是一次低频复盘，不是要求你本轮必须联系别人，也不是需要完成的调用指标。"
            "如果当前用户明确要求联系、转发或委托目标会话处理事情，可按用户要求正常判断；"
            "除此之外，自主联系必须同时满足：有明确的新信息；与目标本人或目标会话正在处理的"
            "事情直接相关；对方大概率希望现在就知道，而不是以后顺带再说；近期没有向同一目标"
            "转达过相同或近似主题。"
            "不要为了维持存在感、填补沉默、分享每个有趣瞬间或响应这条提醒而联系别人。"
            "普通闲聊、轻微吐槽、无关趣闻、重复话题、已经说过的进展，以及留在当前会话处理"
            "更自然的内容，都不要跨会话发送；拿不准时默认不发送。"
            "同一主题的零散消息应先等待并合并成一次完整转达，不要逐条追发；"
            "硬频率上限只是安全上限，不是鼓励你用满的配额。"
            f"{target_hint}"
            "如果要保留聊天记录形式，可选择提示中的 forward_1，或把多个 message_* 整理到 forward_refs；"
            "请在 task 中写清目标会话里的你应如何自然表达和处理；"
            "绝大多数情况下正常留在当前会话回复即可，不要提及这条内部提醒。"
        )

    def _build_target_task_text(self, task_payload: dict) -> str:
        requester_id = str(task_payload.get("requester_id") or "").strip()
        requester_name = str(task_payload.get("requester_name") or requester_id).strip()
        source_session = str(task_payload.get("source_session") or "").strip()
        source_message = str(task_payload.get("source_message") or "").strip()
        task = str(task_payload.get("task") or "").strip()
        attachment_summary = str(task_payload.get("attachment_summary") or "").strip()

        if self.max_source_message_chars <= 0:
            source_message = ""
        elif len(source_message) > self.max_source_message_chars:
            source_message = (
                source_message[: self.max_source_message_chars] + "\n[原始消息已截断]"
            )

        lines = [
            "[跨会话行动]",
            f"来源会话: {source_session}",
            f"请求者: {requester_name}({requester_id})"
            if requester_id
            else f"请求者: {requester_name}",
        ]
        if source_message:
            lines.extend(["原始消息:", source_message])
        if attachment_summary:
            lines.extend(["附件信息:", attachment_summary])
        if task_payload.get("has_pending_attachments") or task_payload.get(
            "has_pending_forwards"
        ):
            lines.extend(
                [
                    "跨会话内容执行规则:",
                    "插件已经锁定本次选中的图片、文件和合并聊天记录，会在你的文字回复发送完成后自动投递。",
                    "合并聊天记录会以 QQ 原生可展开卡片发送；不要自行重组、搜索、替换、补发，也不要调用其他发送工具重复投递。",
                    "你只需完成文字、At、群管理等其余行动；如果任务明确要求只发送选中的内容且不要文字，可以保持最终回复为空。",
                ]
            )
        lines.extend(
            [
                "行动目标:",
                task,
                "请结合当前目标会话的人设、关系和历史自然完成行动。",
                "直接在当前会话中说话或调用工具，不要复述内部说明，也不要只回复任务已收到。",
            ]
        )
        return "\n".join(lines)

    async def _build_qq_task_wake_event(
        self,
        platform,
        task_payload: dict,
        wake_images_base64: list[str] | None = None,
    ) -> AstrMessageEvent:
        """Build a synthetic QQ event for the delegated target session.

        Args:
            platform: Target QQ platform instance.
            task_payload: Source and task metadata exposed to the target LLM.
            wake_images_base64: One-shot images available to the target LLM.

        Returns:
            The synthetic target-session message event.
        """

        target_id = str(task_payload.get("target_id") or "").strip()
        target_type = self._normalize_target_type_name(
            task_payload.get("target_type"), "GroupMessage"
        )
        requester_id = str(task_payload.get("requester_id") or "").strip()
        requester_name = str(
            task_payload.get("requester_name") or requester_id or "跨会话任务"
        ).strip()
        self_id = await self._resolve_qq_self_id(platform)
        task_text = self._build_target_task_text(task_payload)

        message = AstrBotMessage()
        message.self_id = self_id
        message.message_id = f"gossip-task-{uuid.uuid4().hex}"
        message.timestamp = int(time.time())
        message.raw_message = None
        message.message_str = task_text
        message.message = []
        if target_type == "GroupMessage" and self_id:
            message.message.append(At(qq=self_id, name=""))
        message.message.append(Plain(task_text))
        for image_base64 in wake_images_base64 or []:
            if isinstance(image_base64, str) and image_base64:
                message.message.append(Image.fromBase64(image_base64))
        if target_type == "GroupMessage":
            message.type = MessageType.GROUP_MESSAGE
            message.group_id = target_id
            message.group = Group(group_id=target_id)
            message.sender = MessageMember(
                user_id=requester_id, nickname=requester_name
            )
        else:
            message.type = MessageType.FRIEND_MESSAGE
            message.group = None
            # Private-session routing is derived from the synthetic sender ID.
            # The original requester remains available in DELEGATED_TASK_EXTRA.
            message.sender = MessageMember(user_id=target_id, nickname=target_id)
        message.session_id = target_id

        target_event = platform.create_event(message)
        target_event.set_extra(SYNTHETIC_EVENT_EXTRA, True)
        target_event.set_extra(DELEGATED_TASK_EXTRA, task_payload)
        target_event.set_extra(
            "gossip_sharer_source_session", task_payload.get("source_session")
        )
        target_event.set_extra(
            "gossip_sharer_target_session", task_payload.get("target_session")
        )
        target_event.is_wake = True
        target_event.is_at_or_wake_command = True
        return target_event

    async def _safe_wake_qq_session_task(
        self,
        event: AstrMessageEvent,
        target_id: str,
        task: str,
        target_type: str = "GroupMessage",
        target_platform: str | None = None,
        image_refs=None,
        file_refs=None,
        forward_refs=None,
        forward_items=None,
    ) -> str:
        if not self.enable_target_session_tasks:
            return "唤醒失败：目标会话任务唤醒工具未启用。"

        original_event = event
        event = self._unwrap_message_event(event)
        if event is None:
            return (
                "唤醒失败：无法从工具上下文识别当前来源事件。"
                f"收到的对象类型：{self._describe_event_like(original_event)}。"
            )

        try:
            if event.get_platform_name() != "aiocqhttp":
                return (
                    "唤醒失败：目标会话任务当前只支持 QQ OneBot(aiocqhttp) 来源事件，"
                    f"实际来源平台为 {event.get_platform_name()}。"
                )
        except Exception:
            return "唤醒失败：无法识别当前来源平台。"

        requester_id, requester_name = self._get_effective_requester(event)
        if not requester_id:
            return "唤醒失败：无法识别请求者 QQ。"

        target_type = self._normalize_target_type_name(target_type, "GroupMessage")
        target_id = str(target_id or "").strip()
        task = str(task or "").strip()
        platform_id = self._effective_target_platform_id(event, target_platform)
        if not task:
            return "唤醒失败：task 不能为空。"
        if not platform_id:
            return "唤醒失败：未配置默认平台 ID，也无法从当前 QQ 事件推断目标平台。"
        if target_type not in ("GroupMessage", "FriendMessage"):
            return "唤醒失败：target_type 只允许为 FriendMessage 或 GroupMessage。"

        error = self._validate_target(target_type, target_id, platform_id)
        if error:
            return error.replace("发送失败：", "唤醒失败：", 1)

        session_id = self._build_session_id(target_type, target_id, platform_id)
        if not session_id:
            return "唤醒失败：无法构造目标会话。"

        platform = self._get_platform_by_id(platform_id)
        if platform is None:
            return f"唤醒失败：未找到目标平台 {platform_id}。"
        try:
            platform_meta = platform.meta()
        except Exception:
            return "唤醒失败：无法读取目标平台信息。"
        if platform_meta.name != "aiocqhttp":
            return (
                "唤醒失败：目标会话 LLM 唤醒只支持 QQ OneBot(aiocqhttp)，"
                f"实际平台为 {platform_meta.name}。"
            )

        source_session = self._event_key(event)
        rate_limit_error = self._wake_rate_limit_error(
            source_session,
            session_id,
        )
        if rate_limit_error:
            logger.info(
                f"已拦截过于频繁的 QQ 会话唤醒: source={source_session}, "
                f"target={session_id}"
            )
            return rate_limit_error

        normalized_forward_items = self._normalize_forward_items(forward_items)
        embedded_image_refs, embedded_file_refs = self._forward_item_attachment_refs(
            normalized_forward_items
        )
        prepared = await self._prepare_wake_attachments(
            event,
            image_refs,
            file_refs,
            embedded_image_refs=embedded_image_refs,
            embedded_file_refs=embedded_file_refs,
        )
        forward_prepared = await self._prepare_wake_forwards(
            event,
            platform_id,
            task,
            forward_refs,
            normalized_forward_items,
            prepared,
        )
        prepared["forwards"] = forward_prepared.get("forwards", [])
        prepared["failures"].extend(forward_prepared.get("failures", []))
        prepared["target_type"] = target_type
        prepared["target_id"] = target_id
        prepared["target_platform"] = platform_id
        attachment_summary = self._format_wake_attachment_summary(
            prepared, delivered=None
        )
        task_payload = {
            "target_session": session_id,
            "target_type": target_type,
            "target_id": target_id,
            "target_platform": platform_id,
            "task": task,
            "requester_id": requester_id,
            "requester_name": requester_name,
            "source_session": source_session,
            "source_platform": getattr(event, "get_platform_id", lambda: "")(),
            "source_message": getattr(event, "get_message_str", lambda: "")(),
            "attachment_summary": attachment_summary,
            "has_pending_attachments": bool(prepared["images"] or prepared["files"]),
            "has_pending_forwards": bool(prepared["forwards"]),
            "origin": "gossip_sharer",
        }

        try:
            target_event = await self._build_qq_task_wake_event(
                platform,
                task_payload,
                [
                    image["llm_base64"]
                    for image in prepared["images"]
                    if image.get("llm_base64")
                ],
            )
            rate_limit_error = self._wake_rate_limit_error(
                source_session,
                session_id,
            )
            if rate_limit_error:
                for cleanup_path in prepared.get("cleanup_paths", []):
                    try:
                        cleanup_path.unlink(missing_ok=True)
                    except OSError as cleanup_error:
                        logger.warning(
                            f"清理被限流唤醒的临时附件失败 {cleanup_path}: "
                            f"{cleanup_error}"
                        )
                logger.info(
                    "附件准备期间目标触发新的频率限制，已取消本次 QQ 会话唤醒: "
                    f"source={source_session}, target={session_id}"
                )
                return rate_limit_error
            target_event.set_extra(PENDING_WAKE_ATTACHMENTS_EXTRA, prepared)
            for cleanup_path in prepared.get("cleanup_paths", []):
                target_event.track_temporary_local_file(str(cleanup_path))
            platform.commit_event(target_event)
            committed_at = time.monotonic()
            route_key = f"{source_session}\n{session_id}"
            if self.wake_route_cooldown_seconds > 0:
                self._wake_route_last_sent[route_key] = committed_at
            if (
                self.wake_target_window_seconds > 0
                and self.wake_target_max_in_window > 0
            ):
                cutoff = committed_at - self.wake_target_window_seconds
                history = [
                    timestamp
                    for timestamp in self._wake_target_history.get(session_id, [])
                    if timestamp > cutoff
                ]
                history.append(committed_at)
                self._wake_target_history[session_id] = history
        except Exception as e:
            for cleanup_path in prepared.get("cleanup_paths", []):
                try:
                    cleanup_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    logger.warning(
                        f"清理未投递的临时附件失败 {cleanup_path}: {cleanup_error}"
                    )
            logger.warning(f"投递目标 QQ 会话 LLM 唤醒事件失败: {e}", exc_info=True)
            return f"唤醒失败：投递目标 QQ 会话 LLM 唤醒事件失败：{e}"

        logger.info(
            f"已投递目标 QQ 会话 LLM 唤醒事件: target={session_id}, "
            f"requester={requester_id}, task={task}, "
            f"forward_nodes={sum(item.get('node_count', 0) for item in prepared['forwards'])}"
        )
        self._reset_no_share_count(event)
        result = f"{session_id} <- {task}"
        if attachment_summary:
            result += f"\n{attachment_summary}"
        return result

    @filter.on_agent_done(priority=1000)
    async def send_pending_wake_attachments_for_empty_reply(
        self,
        event: AstrMessageEvent,
        run_context,
        response: LLMResponse | None,
    ) -> None:
        """Deliver pending attachments or merged records when the final reply is empty.

        Args:
            event: Synthetic target-session event.
            run_context: Completed agent run context.
            response: Final response produced by the target agent.
        """

        if (
            not self._is_synthetic_event(event)
            or response is None
            or getattr(response, "role", "") != "assistant"
        ):
            return
        prepared = event.get_extra(PENDING_WAKE_ATTACHMENTS_EXTRA, None)
        if not isinstance(prepared, dict) or event.get_extra(
            WAKE_ATTACHMENTS_SENT_EXTRA, False
        ):
            return
        result_chain = getattr(response, "result_chain", None)
        if result_chain and getattr(result_chain, "chain", None):
            return
        if str(getattr(response, "completion_text", "") or "").strip():
            return

        event.set_extra(WAKE_ATTACHMENTS_SENT_EXTRA, True)
        session_id = str(
            event.get_extra("gossip_sharer_target_session", "") or ""
        ).strip()
        if not session_id:
            logger.warning("目标 QQ 会话最终回复为空，但缺少附件投递会话 ID")
            return
        try:
            delivered = await self._send_wake_payloads(session_id, prepared)
            has_pending = bool(
                prepared.get("images")
                or prepared.get("files")
                or prepared.get("forwards")
            )
            if not delivered and has_pending:
                logger.warning(f"目标平台未接受空回复兜底跨会话内容: {session_id}")
                return
            if has_pending:
                logger.info(f"目标会话空回复兜底内容已投递: target={session_id}")
        except Exception as e:
            logger.warning(
                f"目标 QQ 会话空回复兜底内容投递失败: target={session_id}, error={e}",
                exc_info=True,
            )

    @filter.after_message_sent(priority=1000)
    async def send_pending_wake_attachments(self, event: AstrMessageEvent) -> None:
        """Deliver delegated attachments or merged records after the target reply.

        Args:
            event: Event that has completed AstrBot's response stage.
        """

        if not self._is_synthetic_event(event):
            return
        prepared = event.get_extra(PENDING_WAKE_ATTACHMENTS_EXTRA, None)
        if not isinstance(prepared, dict) or event.get_extra(
            WAKE_ATTACHMENTS_SENT_EXTRA, False
        ):
            return

        # Mark before awaiting the platform send to prevent duplicate hook delivery.
        event.set_extra(WAKE_ATTACHMENTS_SENT_EXTRA, True)
        session_id = str(
            event.get_extra("gossip_sharer_target_session", "") or ""
        ).strip()
        if not session_id:
            logger.warning("目标 QQ 会话回复已完成，但缺少附件投递会话 ID")
            return

        try:
            delivered = await self._send_wake_payloads(session_id, prepared)
            has_pending = bool(
                prepared.get("images")
                or prepared.get("files")
                or prepared.get("forwards")
            )
            if not delivered and has_pending:
                logger.warning(f"目标平台未接受回复后的跨会话内容: {session_id}")
                return
            if has_pending:
                logger.info(f"目标会话回复后内容已投递: target={session_id}")
        except Exception as e:
            logger.warning(
                f"目标 QQ 会话回复后内容投递失败: target={session_id}, error={e}",
                exc_info=True,
            )

    @filter.llm_tool("get_available_groups")
    async def get_groups(self, event: AstrMessageEvent):
        """
        获取 Bot 当前可感知到的群聊列表，并标注哪些群在白名单中可用于转发。
        """
        try:
            event = self._unwrap_message_event(event)
            group_data = await self._try_get_group_list(event)
            formatted = self._format_group_list(group_data)

            whitelist_tip = (
                f"\n当前群白名单：{', '.join(self.group_whitelist)}"
                if self.group_whitelist
                else "\n当前没有任何群聊白名单。"
            )

            if formatted:
                return formatted + whitelist_tip

            if self.group_whitelist:
                return (
                    "当前平台暂未提供可读取的群列表接口。"
                    f"不过已配置的可转发群白名单为：{', '.join(self.group_whitelist)}"
                )
            return "当前平台暂未提供可读取的群列表接口，且目前没有任何群聊白名单。"
        except Exception as e:
            return f"获取群列表失败：{e}"

    @filter.llm_tool("get_friend_list")
    async def get_friend_list(self, event: AstrMessageEvent):
        """
        获取 Bot 当前可感知到的好友列表；若当前平台不支持，则返回降级说明。
        """
        try:
            event = self._unwrap_message_event(event)
            friend_data = await self._try_get_friend_list(event)
            formatted = self._format_friend_list(friend_data)
            if formatted:
                return formatted
            if self.sister_qq:
                return (
                    f"当前平台暂未提供可读取的好友列表接口。"
                    f"不过姐姐({self.sister_qq}) 仍可作为默认私聊目标。"
                )
            return "当前平台暂未提供可读取的好友列表接口，且未配置 sister_qq。"
        except Exception as e:
            return f"获取好友列表失败：{e}"

    @filter.llm_tool("get_target_group_members")
    async def get_target_group_members(
        self,
        event: AstrMessageEvent,
        target_id: str,
        target_platform: str = None,
        keyword: str = "",
        limit: int = 50,
    ):
        """
        获取目标群聊成员列表，用于转发消息前确认应该 at 哪些目标会话成员。

        Args:
            target_id (str): 目标群号。该群必须在群白名单中。
            target_platform (str): 可选。平台 ID。默认使用配置值 default_platform。
            keyword (str): 可选。按 QQ、群名片或昵称过滤成员。
            limit (int): 可选。最多展示多少名成员，默认 50，最大 200。
        """
        try:
            event = self._unwrap_message_event(event)
            error = self._validate_target("GroupMessage", target_id, target_platform)
            if error:
                return error
            member_data = await self._try_get_target_group_members(
                target_id, target_platform
            )
            formatted = self._format_target_group_members(member_data, keyword, limit)
            if formatted:
                return formatted
            return (
                "当前目标平台暂未提供可读取的群成员列表接口，或 Bot 无法读取该群成员。"
            )
        except Exception as e:
            return f"获取目标群成员失败：{e}"

    async def send_cross_message(
        self,
        event: AstrMessageEvent,
        target_type: str,
        target_id: str,
        content: str = "",
        target_platform: str = None,
        image_url: str = None,
        image_path: str = None,
        image_base64: str = None,
        at_qqs: list[str] = None,
        at_names: list[str] = None,
        at_all: bool = False,
    ):
        """
        向指定私聊或群聊发送文字、图片或图文混合消息。

        这是插件内部发送层，不注册为 LLM 工具。正常跨会话行动必须先通过
        wake_qq_session_task 的目标校验、频率限制和目标会话处理流程。

        Args:
            target_type (str): 消息类型。'FriendMessage' (私聊) 或 'GroupMessage' (群聊)。
            target_id (str): 接收目标的 QQ 号或群号。默认安全策略下，私聊只允许配置的姐姐 QQ。
            content (str): 可选。要发送的文字内容。
            target_platform (str): 可选。平台 ID。默认使用配置值 default_platform。
            image_url (str): 可选。要发送的 HTTP/HTTPS 图片链接。
            image_path (str): 可选。要发送的 Bot 本地可读图片路径。
            image_base64 (str): 可选。要发送的图片 base64 内容，可带或不带 data:image 前缀。
            at_qqs (list[string]): 可选。目标群聊里要 at 的 QQ 号列表，也兼容逗号或空格分隔的字符串。
            at_names (list[string]): 可选。与 at_qqs 对应的显示名；QQ 平台通常会按 QQ 号自行解析。
            at_all (bool): 可选。是否 at 全体成员；仅 GroupMessage 可用。
        """
        try:
            session_id = await self._safe_send(
                event,
                target_type,
                target_id,
                content,
                target_platform,
                image_url,
                image_path,
                image_base64,
                at_qqs,
                at_names,
                at_all,
            )
            if session_id.startswith("发送失败："):
                return session_id
            return f"消息已送达：{session_id}"
        except Exception as e:
            return f"发送失败：{str(e)}"

    @filter.llm_tool("wake_qq_session_task")
    async def wake_qq_session_task(
        self,
        event: AstrMessageEvent,
        target_id: str,
        task: str,
        target_type: str = "GroupMessage",
        target_platform: str = None,
        image_refs: list[str] = None,
        file_refs: list[str] = None,
        forward_refs: list[str] = None,
        forward_items: list[dict] = None,
    ):
        """
        将任务委派给指定 QQ 群聊或私聊的目标 LLM。

        用户明确要求联系、转发或委托目标会话处理事情时，可按要求调用；硬频率限制仍然生效。
        模型自主发起时应默认克制，只有同时满足以下条件才调用：存在明确的新信息；内容与目标
        本人或目标会话正在处理的事情直接相关；对方大概率希望现在就知道；近期没有转达过相同
        或近似主题。不要为了维持存在感、填补沉默、分享每个有趣瞬间或仅仅因为看到了主动提醒
        而调用。普通闲聊、无关趣闻、轻微吐槽、重复进展或留在当前会话处理更自然的内容不要
        调用；拿不准时不调用。同一主题的零散消息应先合并，不要逐条追发。硬频率限制只是安全
        上限，不是应该用满的发送配额。
        目标 LLM 会读取目标会话上下文，并自行说话、查询成员、At 或调用工具。
        task 必须写清楚目标会话要完成的事情。只有确实要把内容发过去时，
        才传入当前提示中列出的 image_refs、file_refs、forward_refs 或 forward_items；
        未选择的内容不会自动发送。选中的图片、文件和合并聊天记录会在目标 LLM
        回复发送完成后投递，图片仍会先提供给目标 LLM 识别。
        转述真实对话时必须依据目录中的内容预览选齐所有相关 message_*，优先使用
        forward_refs 保留原消息和真实发送者，不要默认选择编号最小的消息或只选择附近表情包。
        工具带有按来源路由和目标会话计算的硬频率限制；若返回频率限制，禁止立即重试，
        应继续在当前会话自然回复，等待确实出现新的内容且冷却结束后再考虑联系。

        Args:
            target_id (str): 目标 QQ 群号或好友 QQ。群目标必须在白名单中；私聊目标遵循私聊安全配置。
            task (str): 目标 LLM 要完成的自然语言行动。
            target_type (str): 目标会话类型。支持 GroupMessage 和 FriendMessage，默认 GroupMessage。
            target_platform (str): 可选。QQ 平台 ID。默认使用 default_platform；未配置时尝试使用当前 QQ 平台。
            image_refs (list[string]): 可选。要主动发送的图片引用。当前消息或引用图片必须优先使用 image_1 这类短引用，不要复用历史中的 media_image 临时路径；也支持仍然有效的允许路径、URL 或 base64。
            file_refs (list[string]): 可选。要主动发送的文件短引用、允许路径或 HTTP/HTTPS URL。
            forward_refs (list[string]): 可选。要发送为原生 QQ 合并聊天记录的来源引用。使用提示中列出的 forward_1（已有合并记录）或 message_1、message_2（零散消息），并严格核对每项后的实际内容预览；叙述涉及多条消息时必须把所有相关引用按原顺序选齐，不能只选表情包或默认选择 message_1。多个引用会按顺序整理成一张可展开卡片。只要真实 message_* 可用，就优先使用本参数，以最大限度保留 QQ 各客户端中的原内容、头像和昵称。
            forward_items (list[dict]): 可选。用于整理新的合并聊天记录。每项可包含 ref/message_ref/forward_ref（插入已有来源并自动保留原发送者）、sender_ref（只继承某条来源的真实发送者）、text、image_refs、file_refs、at_qqs、at_names、sender_name、sender_id、time。若原消息已有 message_*，应直接用 forward_refs 或只填 ref，不要复制其 text 重写成自定义节点；QQ PC 对自定义节点头像昵称的兼容性不如原消息 ID。自定义节点必须尽量填写真实 sender_name，已知 QQ 时同时填写 sender_id；仅有名称时插件会优先匹配来源群成员，匹配不到也会生成独立显示身份。有图片的节点必须把提示中的 image_1 等短引用放入该节点的 image_refs，不要只在 text 中写“[图片]”；仅放在顶层 image_refs 会变成卡片外单独发送。at_names 可与 at_qqs 按顺序对应，用于稳定显示历史 At 名称。time 可传 Unix 秒或毫秒时间戳，不传时插件会按节点顺序生成连续时间。例如 [{"sender_name":"甲","text":"第一条"},{"sender_name":"乙","text":"看图","image_refs":["image_1"]}]。只有确实需要自定义节点时才使用，不要把内部说明写进 text。
        """
        try:
            result = await self._safe_wake_qq_session_task(
                event,
                target_id=target_id,
                task=task,
                target_type=target_type,
                target_platform=target_platform,
                image_refs=image_refs,
                file_refs=file_refs,
                forward_refs=forward_refs,
                forward_items=forward_items,
            )
            if result.startswith("唤醒失败："):
                return result
            return f"目标会话 LLM 已唤醒：{result}"
        except Exception as e:
            return f"唤醒失败：{str(e)}"

    @filter.event_message_type(filter.EventMessageType.ALL, priority=60)
    async def capture_forward_sources(self, event: AstrMessageEvent):
        """Capture QQ message IDs before debounce/reconstruction plugins rewrite them."""

        if not self.enable_wake_forwards:
            return
        try:
            if self._is_synthetic_event(event):
                return
        except Exception:
            pass
        entries = self._build_capture_entries(event)
        self._store_captured_forward_sources(event, entries)

    @filter.on_llm_request()
    async def auto_share_logic(self, event: AstrMessageEvent, req: ProviderRequest):
        if self._is_synthetic_event(event):
            return

        registry = self._ensure_attachment_registry(event, req.image_urls)
        forward_registry = self._ensure_forward_registry(event)
        snapshotted = 0
        for item in registry.values():
            if item.get("kind") != "image" or item.get("snapshot_base64"):
                continue
            if snapshotted >= self.max_wake_images:
                break
            try:
                encoded = await item["component"].convert_to_base64()
                size = len(encoded) * 3 // 4
                if size > self.max_wake_image_mb * 1024 * 1024:
                    continue
                item["snapshot_base64"] = encoded
                item["snapshot_size"] = size
                llm_encoded, is_gif, llm_error = self._prepare_image_for_llm(encoded)
                item["llm_snapshot_base64"] = llm_encoded
                item["llm_snapshot_error"] = llm_error
                item["is_gif"] = is_gif
                snapshotted += 1
            except Exception as e:
                logger.debug(f"提前快照附件图片失败 {item.get('id')}: {e}")

        llm_image_urls = []
        for image_ref in req.image_urls:
            entry = self._find_attachment_entry(registry, str(image_ref), "image")
            if entry and entry.get("is_gif"):
                llm_encoded = entry.get("llm_snapshot_base64")
                if llm_encoded:
                    llm_image_urls.append(f"base64://{llm_encoded}")
                else:
                    logger.warning(
                        f"GIF {entry.get('id')} 无法生成 LLM 识别首帧，"
                        "已从本次 LLM 图片输入中移除，但仍可通过 wake 发送原图"
                    )
                continue
            if entry:
                llm_image_urls.append(image_ref)
                continue
            try:
                encoded = await Image(file=str(image_ref)).convert_to_base64()
                llm_encoded, is_gif, llm_error = self._prepare_image_for_llm(encoded)
                if is_gif:
                    if llm_encoded:
                        llm_image_urls.append(f"base64://{llm_encoded}")
                    else:
                        logger.warning(
                            f"GIF 图片无法生成 LLM 识别首帧，已忽略本次识别输入: {llm_error}"
                        )
                    continue
            except Exception:
                pass
            llm_image_urls.append(image_ref)
        req.image_urls = llm_image_urls
        attachment_catalog = self._format_attachment_catalog(registry)
        if attachment_catalog:
            req.extra_user_content_parts.append(
                TextPart(text=attachment_catalog).mark_as_temp()
            )
        forward_catalog = self._format_forward_catalog(forward_registry)
        if forward_catalog:
            req.extra_user_content_parts.append(
                TextPart(text=forward_catalog).mark_as_temp()
            )

        if self.guarantee_threshold <= 0:
            return

        event_key = self._event_key(event)
        count = self.no_share_counts.get(event_key, 0) + 1
        self.no_share_counts[event_key] = count
        if count < self.guarantee_threshold:
            return

        self.no_share_counts[event_key] = 0
        prompt = self._build_guarantee_prompt(count)
        reminder_part = TextPart(text=prompt).mark_as_temp()
        if self.guarantee_injection_method in {
            "user_message_before",
            "user_message_after",
        }:
            original_prompt = str(req.prompt or "")
            existing_parts = list(req.extra_user_content_parts or [])
            user_part = [TextPart(text=original_prompt)] if original_prompt else []
            req.prompt = None
            if self.guarantee_injection_method == "user_message_before":
                req.extra_user_content_parts = [
                    reminder_part,
                    *user_part,
                    *existing_parts,
                ]
            else:
                req.extra_user_content_parts = [
                    *user_part,
                    reminder_part,
                    *existing_parts,
                ]
        else:
            req.extra_user_content_parts.append(reminder_part)
        logger.info(
            f"已为会话 {event_key} 触发周期性主动社交提示并重新计数，"
            f"注入位置={self.guarantee_injection_method}，"
            "提示 Bot 自主决定是否调用 wake_qq_session_task"
        )
