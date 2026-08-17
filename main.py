import asyncio
import base64
import copy
import io
import json
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

PLUGIN_VERSION = "1.9.3"
SYNTHETIC_EVENT_EXTRA = "gossip_sharer_synthetic_event"
DELEGATED_TASK_EXTRA = "gossip_sharer_delegated_target_task"
ATTACHMENT_REGISTRY_EXTRA = "gossip_sharer_attachment_registry"
FORWARD_REGISTRY_EXTRA = "gossip_sharer_forward_registry"
CAPTURED_FORWARD_SOURCES_EXTRA = "gossip_sharer_captured_forward_sources"
PENDING_WAKE_ATTACHMENTS_EXTRA = "gossip_sharer_pending_wake_attachments"
WAKE_ATTACHMENTS_SENT_EXTRA = "gossip_sharer_wake_attachments_sent"
WAKE_ATTACHMENTS_SENDING_EXTRA = "gossip_sharer_wake_attachments_sending"
WAKE_ATTACHMENTS_ATTEMPTED_EXTRA = "gossip_sharer_wake_attachments_attempted"

FORWARD_CAPTURE_TTL_SECONDS = 180
FORWARD_CAPTURE_MAX_MESSAGES = 30

# 跨会话唤醒幂等窗口：同一 requester+target+task 在该秒数内重复调用视为重复投递并拦截。
# 实测上游 LLM 会在一次 tool-loop 内相隔约 12-14s 重复调用，30s 留足余量。
WAKE_DEDUP_WINDOW_SECONDS = 30


@register(
    "astrbot_plugin_gossip_sharer", "gabriel", "全能消息转发与告状工具", PLUGIN_VERSION
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
        # 跨会话唤醒幂等状态：处理中签名与已成功提交签名分开管理。
        self._wake_signature_lock = asyncio.Lock()
        self._inflight_wake_signatures: set[str] = set()
        self._recent_wake_signatures: dict[str, float] = {}

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
            f"{self.enable_wake_images}/{self.enable_wake_files}/{self.enable_wake_forwards}"
        )

    async def terminate(self):
        self._captured_forward_sources.clear()

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
                forward_id = str(
                    data.get("id") or data.get("message_id") or ""
                ).strip()
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
        sender_id = str(
            getattr(event, "get_sender_id", lambda: "")() or ""
        ).strip()
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
            if now - float(payload.get("updated_at") or 0)
            > FORWARD_CAPTURE_TTL_SECONDS
        ]
        for key in expired:
            self._captured_forward_sources.pop(key, None)

    def _store_captured_forward_sources(
        self, event: AstrMessageEvent, entries: list[dict]
    ) -> None:
        if not entries:
            return
        self._prune_captured_forward_sources()
        event_key = self._event_key(event)
        payload = self._captured_forward_sources.setdefault(
            event_key, {"updated_at": time.monotonic(), "entries": []}
        )
        identities = {
            self._capture_entry_identity(item) for item in payload["entries"]
        }
        for entry in entries:
            identity = self._capture_entry_identity(entry)
            if identity in identities:
                continue
            payload["entries"].append(entry)
            identities.add(identity)
        payload["entries"] = payload["entries"][-FORWARD_CAPTURE_MAX_MESSAGES:]
        payload["updated_at"] = time.monotonic()
        try:
            event.set_extra(CAPTURED_FORWARD_SOURCES_EXTRA, list(payload["entries"]))
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
        cached_payload = self._captured_forward_sources.pop(event_key, {})
        entries = list(cached_payload.get("entries") or [])
        try:
            entries.extend(
                event.get_extra(CAPTURED_FORWARD_SOURCES_EXTRA, []) or []
            )
        except Exception:
            pass
        entries.extend(self._build_capture_entries(event))

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

        registry = {}
        counters = {"forward": 0, "message": 0}
        message_position = 0
        current_message_count = sum(
            1
            for entry in deduped
            if entry.get("kind") == "message" and entry.get("source") == "当前消息"
        )
        for entry in deduped:
            kind = str(entry.get("kind") or "")
            if kind not in counters:
                continue
            counters[kind] += 1
            ref_id = f"{kind}_{counters[kind]}"
            item = {**entry, "id": ref_id}
            if kind == "message" and item.get("source") == "当前消息":
                message_position += 1
                if current_message_count > 1:
                    item["source"] = f"本轮第 {message_position} 条消息"
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
            "只有确实需要发送时，才把下面的短引用传给 wake_qq_session_task 的 forward_refs。",
            "forward_1 表示已有合并记录；message_1 表示一条零散消息，可把多个 message_* 组合成一条新的可展开记录。",
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
            lines.append(
                f"- {ref_id}: {kind_name}，{item.get('source') or '当前消息'}，发送者 {sender}"
            )
        if len(registry) > len(catalog_items):
            lines.append(
                f"- 另有 {len(registry) - len(catalog_items)} 条来源未展示；"
                "如需更多请提高 max_wake_forwards。"
            )
        return "\n".join(lines)

    def _find_forward_entry(
        self, registry: dict[str, dict], ref: str
    ) -> dict | None:
        direct = registry.get(ref)
        if direct:
            return direct
        for item in registry.values():
            if ref in item.get("aliases", set()):
                return item
        return None

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
                    sender_id = str(
                        data.get("user_id") or data.get("uin") or "0"
                    )
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
        for segment in self._normalize_onebot_content(content):
            seg_type = str(segment.get("type") or "").lower()
            data = segment.get("data")
            if not isinstance(data, dict):
                data = {}
            if seg_type in {"text", "plain"}:
                text = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()
                if text:
                    parts.append((text, False))
            elif seg_type == "image":
                parts.append(("[图片]", True))
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
                parts.append(
                    (f"@{data.get('name') or data.get('qq') or '成员'}", True)
                )
            elif seg_type in {"face", "mface"}:
                parts.append(("[表情]", True))
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
                parts.append((text or "[Markdown]", bool(not text)))
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
            if preview and (previous_separated or separated) and not preview.endswith(" "):
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
            sender_name = str(
                data.get("nickname") or data.get("name") or ""
            ).strip()
            if sender_name and sender_name not in sender_names:
                sender_names.append(sender_name)
            if len(sender_names) >= 4:
                break
        return (
            "和".join(sender_names) + "的聊天记录"
            if sender_names
            else "聊天记录"
        )

    def _forward_card_news(
        self,
        primary_nodes: list[dict],
        fallback_nodes: list[dict],
        limit: int = 4,
    ) -> list[dict]:
        nodes = self._forward_card_nodes_for_metadata(
            primary_nodes, fallback_nodes
        )
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

    @staticmethod
    def _wake_prepare_issue(
        kind: str, code: str, message: str, ref: str = ""
    ) -> dict:
        return {"kind": kind, "ref": ref, "code": code, "message": message}

    @staticmethod
    def _wake_issue_text(issue) -> str:
        if not isinstance(issue, dict):
            return str(issue)
        prefix = str(issue.get("kind") or "内容")
        ref = str(issue.get("ref") or "").strip()
        message = str(issue.get("message") or "未知错误")
        return f"{prefix} {ref}: {message}" if ref else f"{prefix}: {message}"

    def _resolve_wake_image_ref(
        self, registry: dict[str, dict], ref: str
    ) -> tuple[str, dict | None]:
        """Resolve a selected image without guessing stale temporary paths."""

        ref = str(ref or "").strip()
        basename = Path(file_uri_to_path(ref) if is_file_uri(ref) else ref).name
        is_short_ref = bool(re.fullmatch(r"image_\d+", ref))
        is_media_temp_ref = basename.startswith("media_image_")
        if is_short_ref:
            entry = self._find_attachment_entry(registry, ref, "image")
            if entry:
                return str(entry.get("id") or ref), entry
            raise ValueError("当前消息附件目录中不存在此图片短引用")
        if is_media_temp_ref:
            matches = []
            for item in registry.values():
                if item.get("kind") != "image":
                    continue
                candidates = {str(item.get("raw_ref") or "")}
                candidates.update(str(alias) for alias in item.get("aliases", set()))
                if any(
                    candidate == ref
                    or Path(
                        file_uri_to_path(candidate) if is_file_uri(candidate) else candidate
                    ).name
                    == basename
                    for candidate in candidates
                    if candidate
                ):
                    matches.append(item)
            if len(matches) == 1:
                matched = matches[0]
                logger.info(
                    f"历史临时图片引用 {ref} 已精确映射到本轮 {matched['id']}"
                )
                return str(matched["id"]), matched
            if len(matches) > 1:
                raise ValueError("临时图片引用在当前消息中存在多个匹配，拒绝猜测")
            raise ValueError(
                "历史临时图片已失效，且无法精确映射到当前消息附件；请使用 image_N"
            )
        entry = self._find_attachment_entry(registry, ref, "image")
        if entry:
            return str(entry.get("id") or ref), entry
        return ref, None

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
        fatal_failures = []
        warnings = []
        cleanup_paths: list[Path] = []
        total_bytes = 0
        total_limit = self.max_wake_total_mb * 1024 * 1024

        standalone_image_refs = self._normalize_attachment_refs(image_refs)
        embedded_image_refs = self._normalize_attachment_refs(embedded_image_refs)
        normalized_images = list(
            dict.fromkeys([*standalone_image_refs, *embedded_image_refs])
        )
        if normalized_images and not self.enable_wake_images:
            fatal_failures.append(
                self._wake_prepare_issue("图片", "disabled", "图片发送功能已在配置中关闭")
            )
            normalized_images = []
        if len(normalized_images) > self.max_wake_images:
            fatal_failures.append(
                self._wake_prepare_issue(
                    "图片",
                    "count_limit",
                    f"图片数量超过上限 {self.max_wake_images}，本次不提交",
                )
            )
            normalized_images = []

        for ref in normalized_images:
            try:
                canonical_ref, entry = self._resolve_wake_image_ref(registry, ref)
                if entry:
                    component = entry["component"]
                    name = entry["name"]
                elif canonical_ref.startswith(("http://", "https://")):
                    if not self.allow_remote_attachment_urls:
                        raise ValueError("配置禁止直接使用远程附件 URL")
                    component = Image.fromURL(canonical_ref)
                    name = Path(urlparse(canonical_ref).path).name or "image"
                elif canonical_ref.startswith(("base64://", "data:")):
                    component = Image(file=canonical_ref)
                    name = "image"
                else:
                    path = self._validate_attachment_path(canonical_ref)
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
                    warnings.append(
                        self._wake_prepare_issue(
                            "图片",
                            "llm_preview_unavailable",
                            f"LLM 识别副本不可用：{llm_error}；原图仍会发送",
                            canonical_ref,
                        )
                    )
                images.append(
                    {
                        "ref": canonical_ref,
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
                fatal_failures.append(
                    self._wake_prepare_issue("图片", "prepare_failed", str(e), ref)
                )

        standalone_file_refs = self._normalize_attachment_refs(file_refs)
        embedded_file_refs = self._normalize_attachment_refs(embedded_file_refs)
        normalized_files = list(
            dict.fromkeys([*standalone_file_refs, *embedded_file_refs])
        )
        if normalized_files and not self.enable_wake_files:
            fatal_failures.append(
                self._wake_prepare_issue("文件", "disabled", "文件发送功能已在配置中关闭")
            )
            normalized_files = []
        if len(normalized_files) > self.max_wake_files:
            fatal_failures.append(
                self._wake_prepare_issue(
                    "文件",
                    "count_limit",
                    f"文件数量超过上限 {self.max_wake_files}，本次不提交",
                )
            )
            normalized_files = []

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
                fatal_failures.append(
                    self._wake_prepare_issue("文件", "prepare_failed", str(e), ref)
                )
                if downloaded and path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

        return {
            "images": images,
            "files": files,
            "fatal_failures": fatal_failures,
            "warnings": warnings,
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
                return [], [], "", [
                    f"{entry.get('id') or forward_id}: 获取合并聊天记录内容失败或记录已过期"
                ]
        elif nodes_component is not None:
            try:
                payload = await nodes_component.to_dict()
                raw_nodes = self._extract_forward_raw_nodes(payload)
            except Exception as e:
                return [], [], "", [
                    f"{entry.get('id') or '合并记录'}: 转换内联记录失败：{e}"
                ]
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
                primary_nodes.append(
                    {"type": "node", "data": {"id": message_id}}
                )
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
        forward_refs,
        prepared_attachments: dict,
    ) -> dict:
        refs = self._normalize_attachment_refs(forward_refs)
        if not refs:
            return {"forwards": [], "fatal_failures": [], "warnings": []}
        if not self.enable_wake_forwards:
            return {
                "forwards": [],
                "fatal_failures": [
                    self._wake_prepare_issue(
                        "合并记录", "disabled", "合并聊天记录发送功能已在配置中关闭"
                    )
                ],
                "warnings": [],
            }
        if self.max_wake_forwards <= 0:
            return {
                "forwards": [],
                "fatal_failures": [
                    self._wake_prepare_issue(
                        "合并记录", "count_limit", "合并聊天记录发送数量上限为 0"
                    )
                ],
                "warnings": [],
            }
        if len(refs) > self.max_wake_forwards:
            return {
                "forwards": [],
                "fatal_failures": [
                    self._wake_prepare_issue(
                        "合并记录",
                        "count_limit",
                        f"已有合并记录/消息引用超过上限 {self.max_wake_forwards}，本次不提交",
                    )
                ],
                "warnings": [],
            }

        registry = self._ensure_forward_registry(event)
        primary_nodes = []
        fallback_nodes = []
        previews = []
        failures = []
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

        for ref in refs:
            await append_registry_ref(ref)

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
        return {
            "forwards": forwards,
            "fatal_failures": [
                self._wake_prepare_issue(
                    "合并记录", "prepare_failed", failure
                )
                for failure in failures
            ],
            "warnings": [],
        }

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

            try:
                result = await caller(
                    action,
                    **build_action_kwargs(package["nodes"]),
                )
                if self._onebot_action_succeeded(result):
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
                    "按原消息 ID 投递合并记录失败，改用已保存的自定义节点重试"
                )
                try:
                    result = await caller(
                        action,
                        **build_action_kwargs(fallback_nodes),
                    )
                    if not self._onebot_action_succeeded(result):
                        raise RuntimeError(f"平台返回失败结果: {result}")
                except Exception as fallback_error:
                    logger.warning(
                        "目标 QQ 合并记录自定义节点降级投递失败: "
                        f"{fallback_error}",
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
                chain.chain.append(
                    File(name=file_info["name"], file=file_info["path"])
                )
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
                lines.append(
                    f"- 合并聊天记录: {forward.get('node_count', 0)} 个节点"
                )
                if preview:
                    lines.append(f"  预览: {preview[:600]}")
        for warning in prepared.get("warnings", []):
            lines.append(f"- 内容准备警告: {self._wake_issue_text(warning)}")
        return "\n".join(lines)

    def _cleanup_wake_snapshots(self, prepared: dict) -> None:
        cleanup_paths = list(prepared.get("cleanup_paths", []))
        prepared["cleanup_paths"] = []
        for cleanup_path in cleanup_paths:
            path = Path(cleanup_path)
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning(f"清理跨会话附件快照失败 {path}: {e}")

    async def _reserve_wake_signature(self, signature: str) -> str:
        async with self._wake_signature_lock:
            now = time.time()
            expired = [
                item
                for item, timestamp in self._recent_wake_signatures.items()
                if now - timestamp >= WAKE_DEDUP_WINDOW_SECONDS
            ]
            for item in expired:
                self._recent_wake_signatures.pop(item, None)
            if signature in self._inflight_wake_signatures:
                return "inflight"
            if signature in self._recent_wake_signatures:
                return "recent"
            self._inflight_wake_signatures.add(signature)
            return "reserved"

    async def _release_wake_signature(self, signature: str) -> None:
        async with self._wake_signature_lock:
            self._inflight_wake_signatures.discard(signature)

    async def _commit_wake_signature(self, signature: str) -> None:
        async with self._wake_signature_lock:
            self._inflight_wake_signatures.discard(signature)
            self._recent_wake_signatures[signature] = time.time()

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
            f"你可以优先考虑联系姐姐({self.sister_qq})的私聊会话，"
            "也可以选择与内容和关系更匹配的白名单群或允许的好友会话。"
            if self.sister_qq
            else "请选择与内容和关系匹配的白名单群或允许的好友会话。"
        )
        return (
            "[插件临时上下文｜非用户原话]\n"
            "以下内容仅用于帮助你自主判断，不代表当前用户提出了转发要求，"
            "也不要向用户复述本段内容。\n"
            "[主动社交提醒]\n"
            f"当前会话已经连续 {count} 次 LLM 请求没有发起跨会话行动。"
            "请回顾近期对话中是否出现了值得分享的趣事、吐槽、告状、请求转达、"
            "邀请他人回应，或适合发送的图片、文件和合并聊天记录。"
            "如果符合你的人设、关系和当下语境，可以主动调用 `wake_qq_session_task`，"
            "不必等待用户明确说出“转发”“告诉她”或“发过去”。"
            f"{target_hint}"
            "如果要保留聊天记录形式，可选择提示中的 forward_1，或把多个 message_* 整理到 forward_refs；"
            "请在 task 中写清目标会话里的你应如何自然表达和处理；"
            "如果确实没有值得分享的内容，正常回复即可，不要提及这条内部提醒。"
        )

    def _build_target_task_text(self, task_payload: dict) -> str:
        requester_id = str(task_payload.get("requester_id") or "").strip()
        requester_name = str(task_payload.get("requester_name") or requester_id).strip()
        source_session = str(task_payload.get("source_session") or "").strip()
        source_message = str(task_payload.get("source_message") or "").strip()
        task = str(task_payload.get("task") or "").strip()
        attachment_summary = str(
            task_payload.get("attachment_summary") or ""
        ).strip()

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

        # 同一请求者在短时间内对同一目标提交相同任务时，只允许首次投递。
        # 签名有意不包含附件/转发参数，避免模型换一种参数写法绕过去重。
        wake_signature = f"{requester_id}|{session_id}|{task}"
        reservation = await self._reserve_wake_signature(wake_signature)
        if reservation != "reserved":
            logger.info(
                f"命中唤醒幂等状态({reservation})，跳过重复唤醒: "
                f"requester={requester_id}, target={session_id}, task={task}"
            )
            if reservation == "inflight":
                return (
                    "本次唤醒正在准备或提交，请勿重复调用，等待处理完成："
                    f"{session_id} <- {task}"
                )
            return (
                "本次唤醒刚刚已提交并锁定投递，请勿重复调用，等待投递完成："
                f"{session_id} <- {task}"
            )

        prepared = None
        try:
            prepared = await self._prepare_wake_attachments(
                event,
                image_refs,
                file_refs,
            )
            forward_prepared = await self._prepare_wake_forwards(
                event,
                platform_id,
                forward_refs,
                prepared,
            )
        except Exception as e:
            if isinstance(prepared, dict):
                self._cleanup_wake_snapshots(prepared)
            await self._release_wake_signature(wake_signature)
            logger.warning(f"准备跨会话内容时发生未处理异常: {e}", exc_info=True)
            return f"唤醒失败：内容准备失败，目标会话未提交：{e}"

        prepared["forwards"] = forward_prepared.get("forwards", [])
        prepared["fatal_failures"].extend(
            forward_prepared.get("fatal_failures", [])
        )
        prepared["warnings"].extend(forward_prepared.get("warnings", []))
        if prepared["fatal_failures"]:
            failure_text = "；".join(
                self._wake_issue_text(issue)
                for issue in prepared["fatal_failures"]
            )
            self._cleanup_wake_snapshots(prepared)
            await self._release_wake_signature(wake_signature)
            logger.warning(f"跨会话内容准备失败，未提交目标事件: {failure_text}")
            return f"唤醒失败：内容准备失败，目标会话未提交：{failure_text}"

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
            "source_session": self._event_key(event),
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
            target_event.set_extra(PENDING_WAKE_ATTACHMENTS_EXTRA, prepared)
            platform.commit_event(target_event)
            await self._commit_wake_signature(wake_signature)
        except Exception as e:
            self._cleanup_wake_snapshots(prepared)
            await self._release_wake_signature(wake_signature)
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

    async def _deliver_pending_wake_payloads(
        self, event: AstrMessageEvent, *, empty_reply: bool
    ) -> None:
        prepared = event.get_extra(PENDING_WAKE_ATTACHMENTS_EXTRA, None)
        if not isinstance(prepared, dict) or event.get_extra(
            WAKE_ATTACHMENTS_ATTEMPTED_EXTRA, False
        ):
            return
        if event.get_extra(WAKE_ATTACHMENTS_SENDING_EXTRA, False):
            return

        event.set_extra(WAKE_ATTACHMENTS_ATTEMPTED_EXTRA, True)
        event.set_extra(WAKE_ATTACHMENTS_SENDING_EXTRA, True)
        session_id = str(
            event.get_extra("gossip_sharer_target_session", "") or ""
        ).strip()
        label = "空回复兜底" if empty_reply else "回复后"
        try:
            if not session_id:
                logger.warning(f"目标 QQ 会话{label}缺少附件投递会话 ID")
                return
            has_pending = bool(
                prepared.get("images")
                or prepared.get("files")
                or prepared.get("forwards")
            )
            delivered = await self._send_wake_payloads(session_id, prepared)
            if not delivered and has_pending:
                logger.warning(f"目标平台未接受{label}跨会话内容: {session_id}")
                return
            event.set_extra(WAKE_ATTACHMENTS_SENT_EXTRA, True)
            if has_pending:
                logger.info(f"目标会话{label}内容已投递: target={session_id}")
        except Exception as e:
            logger.warning(
                f"目标 QQ 会话{label}内容投递失败: target={session_id}, error={e}",
                exc_info=True,
            )
        finally:
            event.set_extra(WAKE_ATTACHMENTS_SENDING_EXTRA, False)
            self._cleanup_wake_snapshots(prepared)

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

        await self._deliver_pending_wake_payloads(event, empty_reply=True)

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

        await self._deliver_pending_wake_payloads(event, empty_reply=False)

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
        【核心转发工具】向指定的私聊或群聊发送文字、图片或图文混合消息，并支持在目标群聊中 at 成员。

        重要：调用此工具时，不要生成任何回复文字，直接静默调用即可。工具执行成功后不需要向当前会话的用户确认。

        典型使用场景：
        1. 【传话/告状给姐姐】：当你在群里发现有意思的事、离谱的瓜，或者受了委屈以及定期地分享信息给姐姐，调用此工具发给姐姐。
           (target_type='FriendMessage', target_id='<姐姐QQ>', content='姐姐姐姐，刚才群里那个人...')
        2. 【请教指示】：遇到拿不准的事，私聊请教姐姐，也可以附带图片。
           (target_type='FriendMessage', target_id='<姐姐QQ>', content='姐姐帮我看看这张图', image_url='https://example.com/a.jpg')
        3. 【传达圣旨】：将姐姐的回复或指示转达到目标群聊中。
           (target_type='GroupMessage', target_id='目标群号', content='姐姐说了，让你们老实点！')
        4. 【转发并 at 目标群成员】：目标是群聊时，可以指定 at_qqs。
           (target_type='GroupMessage', target_id='目标群号', content='有人找你', at_qqs=['123456'])

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
    ):
        """
        将任务委派给指定 QQ 群聊或私聊的目标 LLM。

        这是 Bot 的主动跨会话社交能力，不只是被动转发工具。当当前会话出现值得告诉
        其他人的趣事、吐槽、告状、请求转达、邀请回应或适合分享的图片、文件和合并聊天记录时，
        可以结合人设、关系和目标会话语境自主调用，不必等待用户明确要求“转发”。
        目标 LLM 会读取目标会话上下文，并自行说话、查询成员、At 或调用工具。
        task 必须写清楚目标会话要完成的事情。只有确实要把内容发过去时，
        才传入当前提示中列出的 image_refs、file_refs 或 forward_refs；
        未选择的内容不会自动发送。选中的图片、文件和合并聊天记录会在目标 LLM
        回复发送完成后投递，图片仍会先提供给目标 LLM 识别。

        Args:
            target_id (str): 目标 QQ 群号或好友 QQ。群目标必须在白名单中；私聊目标遵循私聊安全配置。
            task (str): 目标 LLM 要完成的自然语言行动。
            target_type (str): 目标会话类型。支持 GroupMessage 和 FriendMessage，默认 GroupMessage。
            target_platform (str): 可选。QQ 平台 ID。默认使用 default_platform；未配置时尝试使用当前 QQ 平台。
            image_refs (list[string]): 可选。要主动发送的图片引用。本轮当前消息或引用消息中的图片必须使用提示提供的 image_1 这类短引用；历史 media_image 临时路径仅在能精确映射到本轮附件时兼容，否则整次唤醒失败。也支持允许路径、URL 或 base64。
            file_refs (list[string]): 可选。要主动发送的文件短引用、允许路径或 HTTP/HTTPS URL。
            forward_refs (list[string]): 可选。要发送为原生 QQ 合并聊天记录的来源引用。使用提示中列出的 forward_1（已有合并记录）或 message_1、message_2（零散消息）；多个引用会按顺序整理成一张可展开卡片。
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
