import json
import mimetypes
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, File, Image, Plain, Reply
from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import (
    get_astrbot_temp_path,
    get_astrbot_workspaces_path,
)
from astrbot.core.utils.media_utils import file_uri_to_path, is_file_uri

PLUGIN_VERSION = "1.8.1"
SYNTHETIC_EVENT_EXTRA = "gossip_sharer_synthetic_event"
DELEGATED_TASK_EXTRA = "gossip_sharer_delegated_target_task"
ATTACHMENT_REGISTRY_EXTRA = "gossip_sharer_attachment_registry"
PENDING_WAKE_ATTACHMENTS_EXTRA = "gossip_sharer_pending_wake_attachments"
WAKE_ATTACHMENTS_SENT_EXTRA = "gossip_sharer_wake_attachments_sent"


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
        self.allow_remote_attachment_urls = self._normalize_bool(
            self.config.get("allow_remote_attachment_urls", True)
        )
        self.max_wake_images = self._config_int("max_wake_images", 4, minimum=0)
        self.max_wake_files = self._config_int("max_wake_files", 3, minimum=0)
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
            f"唤醒图片/文件: {self.enable_wake_images}/{self.enable_wake_files}"
        )

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
    ) -> dict:
        """Resolve selected images and files for delivery and target LLM context.

        Args:
            event: Source message event.
            image_refs: Model-selected image references.
            file_refs: Model-selected file references.

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

        normalized_images = self._normalize_attachment_refs(image_refs)
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

                encoded = await component.convert_to_base64()
                size = len(encoded) * 3 // 4
                if size > self.max_wake_image_mb * 1024 * 1024:
                    raise ValueError(f"超过单张图片 {self.max_wake_image_mb} MB 限制")
                if total_bytes + size > total_limit:
                    raise ValueError(f"超过附件总大小 {self.max_wake_total_mb} MB 限制")
                total_bytes += size
                images.append(
                    {"ref": ref, "name": name, "base64": encoded, "size": size}
                )
            except Exception as e:
                failures.append(f"图片 {ref}: {e}")

        normalized_files = self._normalize_attachment_refs(file_refs)
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
                        "name": name,
                        "path": str(snapshot_path),
                        "size": size,
                        "mime_type": mime_type,
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
            chain.base64_image(image["base64"])
        for file_info in prepared.get("files", []):
            chain.chain.append(File(name=file_info["name"], file=file_info["path"]))
        if not chain.chain:
            return True
        return bool(await self.context.send_message(session_id, chain))

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
        if images or files:
            if delivered is None:
                delivery_text = "将在本次回复发送完成后投递到目标会话"
            else:
                delivery_text = (
                    "已发送到目标会话" if delivered else "未能发送到目标会话"
                )
            lines.append(f"附件投递状态: {delivery_text}")
        if delivered is not False:
            for image in images:
                lines.append(
                    f"- 图片: {image['name']} ({image['size'] / 1024 / 1024:.2f} MB)"
                )
            for file_info in files:
                lines.append(
                    f"- 文件: {file_info['name']}，{file_info['mime_type']} "
                    f"({file_info['size'] / 1024 / 1024:.2f} MB)"
                )
        for failure in prepared.get("failures", []):
            lines.append(f"- 附件失败: {failure}")
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
            "邀请他人回应，或适合发送的图片和文件。"
            "如果符合你的人设、关系和当下语境，可以主动调用 `wake_qq_session_task`，"
            "不必等待用户明确说出“转发”“告诉她”或“发过去”。"
            f"{target_hint}"
            "请在 task 中写清目标会话里的你应如何自然表达和处理；"
            "如果确实没有值得分享的内容，正常回复即可，不要提及这条内部提醒。"
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

        prepared = await self._prepare_wake_attachments(event, image_refs, file_refs)
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
            "origin": "gossip_sharer",
        }

        try:
            target_event = await self._build_qq_task_wake_event(
                platform,
                task_payload,
                [image["base64"] for image in prepared["images"]],
            )
            target_event.set_extra(PENDING_WAKE_ATTACHMENTS_EXTRA, prepared)
            for cleanup_path in prepared.get("cleanup_paths", []):
                target_event.track_temporary_local_file(str(cleanup_path))
            platform.commit_event(target_event)
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
            f"requester={requester_id}, task={task}"
        )
        self._reset_no_share_count(event)
        result = f"{session_id} <- {task}"
        if attachment_summary:
            result += f"\n{attachment_summary}"
        return result

    @filter.after_message_sent(priority=1000)
    async def send_pending_wake_attachments(self, event: AstrMessageEvent) -> None:
        """Deliver delegated attachments after the target reply is sent.

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
            delivered = await self._send_wake_attachments(session_id, prepared)
            if not delivered and (prepared.get("images") or prepared.get("files")):
                logger.warning(f"目标平台未接受回复后的附件消息: {session_id}")
                return
            if prepared.get("images") or prepared.get("files"):
                logger.info(f"目标会话回复后附件已投递: target={session_id}")
        except Exception as e:
            logger.warning(
                f"目标 QQ 会话回复后附件投递失败: target={session_id}, error={e}",
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
    ):
        """
        将任务委派给指定 QQ 群聊或私聊的目标 LLM。

        这是 Bot 的主动跨会话社交能力，不只是被动转发工具。当当前会话出现值得告诉
        其他人的趣事、吐槽、告状、请求转达、邀请回应或适合分享的图片和文件时，
        可以结合人设、关系和目标会话语境自主调用，不必等待用户明确要求“转发”。
        目标 LLM 会读取目标会话上下文，并自行说话、查询成员、At 或调用工具。
        task 必须写清楚目标会话要完成的事情。只有确实要把附件发过去时，
        才传入当前提示中列出的 image_refs 或 file_refs；未选择的附件不会自动发送。
        选中的附件会在目标 LLM 回复发送完成后投递，图片仍会先提供给目标 LLM 识别。

        Args:
            target_id (str): 目标 QQ 群号或好友 QQ。群目标必须在白名单中；私聊目标遵循私聊安全配置。
            task (str): 目标 LLM 要完成的自然语言行动。
            target_type (str): 目标会话类型。支持 GroupMessage 和 FriendMessage，默认 GroupMessage。
            target_platform (str): 可选。QQ 平台 ID。默认使用 default_platform；未配置时尝试使用当前 QQ 平台。
            image_refs (list[string]): 可选。要主动发送的图片短引用、允许路径、URL 或 base64 引用。
            file_refs (list[string]): 可选。要主动发送的文件短引用、允许路径或 HTTP/HTTPS URL。
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
            )
            if result.startswith("唤醒失败："):
                return result
            return f"目标会话 LLM 已唤醒：{result}"
        except Exception as e:
            return f"唤醒失败：{str(e)}"

    @filter.on_llm_request()
    async def auto_share_logic(self, event: AstrMessageEvent, req: ProviderRequest):
        if self._is_synthetic_event(event):
            return

        registry = self._ensure_attachment_registry(event, req.image_urls)
        attachment_catalog = self._format_attachment_catalog(registry)
        if attachment_catalog:
            req.extra_user_content_parts.append(
                TextPart(text=attachment_catalog).mark_as_temp()
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
