import json
import os
import re
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


PLUGIN_VERSION = "1.7.1"
SYNTHETIC_EVENT_EXTRA = "gossip_sharer_synthetic_event"
DELEGATED_TASK_EXTRA = "gossip_sharer_delegated_target_task"


@register("astrbot_plugin_gossip_sharer", "gabriel", "全能消息转发与告状工具", PLUGIN_VERSION)
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
        self.group_whitelist = []
        self._load_group_whitelist()
        self.guarantee_threshold = int(self.config.get("guarantee_threshold", 5))
        self.no_share_counts: dict[str, int] = {}

        if not self.default_platform:
            logger.warning("转发告状工具未配置 default_platform，发送时需要显式传入 target_platform")
        if not self.sister_qq:
            logger.warning("转发告状工具未配置 sister_qq，默认私聊目标与保底提示将不可用")

        logger.info(
            f"转发告状工具 v{PLUGIN_VERSION} 已加载。姐姐: {self.sister_qq or '未配置'}，"
            f"默认平台: {self.default_platform or '未配置'}，白名单群数量: {len(self.group_whitelist)}，"
            f"保底阈值: {self.guarantee_threshold}，任意私聊目标: {self.enable_arbitrary_friend_targets}，"
            f"目标会话任务唤醒: {self.enable_target_session_tasks}"
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
        return [str(x).strip() for x in self.config.get("group_whitelist", []) if str(x).strip()]

    def _load_soft_whitelist_groups(self) -> list[str]:
        path = self._soft_whitelist_config_path()
        if not os.path.exists(path):
            logger.warning(f"软白名单配置不存在，跳过读取: {path}")
            return []

        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"加载软白名单配置失败: {e}")
            return []

        if not isinstance(data, dict):
            logger.warning("软白名单配置格式不是对象，跳过读取")
            return []

        groups = [str(x).strip() for x in data.get("group_whitelist", []) if str(x).strip()]
        logger.info(f"已读取软白名单群配置 {len(groups)} 个")
        return groups

    def _load_group_whitelist(self):
        groups = self._load_soft_whitelist_groups() + self._config_group_whitelist()
        self.group_whitelist = list(dict.fromkeys(groups))

    def _event_key(self, event: AstrMessageEvent | None) -> str:
        if event is None:
            return "未知会话"
        return str(getattr(event, "unified_msg_origin", None) or getattr(event, "session", "未知会话"))

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
            requester_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        if not requester_name and event is not None:
            requester_name = str(getattr(event, "get_sender_name", lambda: "")() or "").strip()
        if not requester_name:
            requester_name = requester_id
        return requester_id, requester_name

    def _reset_no_share_count(self, event: AstrMessageEvent | None) -> None:
        self.no_share_counts.pop(self._event_key(event), None)

    def _build_session_id(self, target_type: str, target_id: str, target_platform: str = None) -> str | None:
        platform = str(target_platform or self.default_platform).strip()
        if not platform:
            return None
        return f"{platform}:{target_type}:{str(target_id)}"

    def _validate_target(self, target_type: str, target_id: str, target_platform: str = None) -> str | None:
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
            if not self.enable_arbitrary_friend_targets and str(target_id) != self.sister_qq:
                return "发送失败：当前未开启任意私聊目标，仅允许发送给配置的 sister_qq。"
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

    def _normalize_string_list(self, value, *, split_whitespace: bool = True) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            items = []
            for item in value:
                items.extend(self._normalize_string_list(item, split_whitespace=split_whitespace))
            return items
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                    return self._normalize_string_list(parsed, split_whitespace=split_whitespace)
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

    def _normalize_target_type_name(self, target_type: str | None, default: str = "GroupMessage") -> str:
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

    def _format_at_note(self, at_qqs: list[str] | None = None, at_all: bool = False) -> str:
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
        source_session = getattr(event, "session", None) or getattr(event, "unified_msg_origin", "未知会话")
        source_platform = getattr(event, "get_platform_id", lambda: "未知平台")()
        source_sender = (
            getattr(event, "get_sender_name", lambda: None)()
            or getattr(event, "get_sender_id", lambda: None)()
            or "未知发送者"
        )
        image_notes = self._build_image_context_notes(image_url, image_path, image_base64)
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
                    data = info.get("data") if isinstance(info.get("data"), dict) else info
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
            logger.warning("当前 Context 未提供 conversation_manager，跳过目标会话上下文注入")
            return

        cid = await conv_mgr.get_curr_conversation_id(session_id)
        if not cid:
            parts = session_id.split(":", 2)
            platform_id = parts[0] if len(parts) >= 3 else None
            cid = await conv_mgr.new_conversation(session_id, platform_id=platform_id)

        user_message, assistant_message = self._build_bridge_history_pair(
            event, session_id, content, image_url, image_path, image_base64, at_qqs, at_all
        )
        await conv_mgr.add_message_pair(cid, user_message, assistant_message)
        logger.info(f"已将跨会话转发内容写入目标上下文: session={session_id}, cid={cid}")

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
        if not content and not image_url and not image_path and not image_base64 and not at_qq_list and not at_all_enabled:
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

        candidates.extend([
            getattr(self.context, "get_group_list", None),
            getattr(getattr(self.context, "platform", None), "get_group_list", None),
            getattr(getattr(self.context, "provider", None), "get_group_list", None),
            getattr(getattr(self.context, "adapter", None), "get_group_list", None),
            getattr(getattr(self.context, "client", None), "get_group_list", None),
        ])

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

        candidates.extend([
            getattr(self.context, "get_friend_list", None),
            getattr(getattr(self.context, "platform", None), "get_friend_list", None),
            getattr(getattr(self.context, "provider", None), "get_friend_list", None),
            getattr(getattr(self.context, "adapter", None), "get_friend_list", None),
            getattr(getattr(self.context, "client", None), "get_friend_list", None),
        ])

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
                if isinstance(result, dict) and "data" in result and any(
                    key in result for key in ("retcode", "status", "msg", "wording")
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

    def _format_target_group_members(self, member_data, keyword: str = "", limit: int = 50) -> str:
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
            uid = str(item.get("user_id") or item.get("uin") or item.get("qq") or item.get("id") or "")
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
                uid = item.get("_uid") or item.get("user_id") or item.get("uin") or item.get("qq") or item.get("id") or "未知ID"
                alias = item.get("_alias") or item.get("card") or item.get("nickname") or item.get("name") or "未知昵称"
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
        if not self.sister_qq:
            return (
                f"已经连续 {count} 次未主动分享消息。"
                "但当前未配置 sister_qq，请不要调用 send_cross_message 进行默认私聊告状。"
            )
        return (
            f"已经连续 {count} 次未主动分享消息。"
            f"请你现在根据当前上下文，判断是否需要给姐姐({self.sister_qq})打个小报告。"
            f"如果要分享，请优先调用工具 `send_cross_message`，"
            f"参数建议为 target_type='FriendMessage'、target_id='{self.sister_qq}'，"
            "并把你认为值得分享的最近内容整理后发过去。"
        )

    def _build_target_task_text(self, task_payload: dict) -> str:
        requester_id = str(task_payload.get("requester_id") or "").strip()
        requester_name = str(task_payload.get("requester_name") or requester_id).strip()
        source_session = str(task_payload.get("source_session") or "").strip()
        task = str(task_payload.get("task") or "").strip()

        lines = [
            "[跨会话任务]",
            f"来源会话: {source_session}",
            f"请求者: {requester_name}({requester_id})" if requester_id else f"请求者: {requester_name}",
            "请在当前目标 QQ 会话中直接完成下面的任务；不要只转述任务。",
            f"任务: {task}",
        ]
        return "\n".join(lines)

    async def _build_qq_task_wake_event(
        self,
        platform,
        task_payload: dict,
    ) -> AstrMessageEvent:
        target_id = str(task_payload.get("target_id") or "").strip()
        target_type = self._normalize_target_type_name(
            task_payload.get("target_type"), "GroupMessage"
        )
        requester_id = str(task_payload.get("requester_id") or "").strip()
        requester_name = str(task_payload.get("requester_name") or requester_id or "跨会话任务").strip()
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
        target_event.set_extra("gossip_sharer_source_session", task_payload.get("source_session"))
        target_event.set_extra("gossip_sharer_target_session", task_payload.get("target_session"))
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
            "origin": "gossip_sharer",
        }

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

        try:
            target_event = await self._build_qq_task_wake_event(platform, task_payload)
            platform.commit_event(target_event)
        except Exception as e:
            logger.warning(f"投递目标 QQ 会话 LLM 唤醒事件失败: {e}", exc_info=True)
            return f"唤醒失败：投递目标 QQ 会话 LLM 唤醒事件失败：{e}"

        logger.info(
            f"已投递目标 QQ 会话 LLM 唤醒事件: target={session_id}, "
            f"requester={requester_id}, task={task}"
        )
        return f"{session_id} <- {task}"

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
            member_data = await self._try_get_target_group_members(target_id, target_platform)
            formatted = self._format_target_group_members(member_data, keyword, limit)
            if formatted:
                return formatted
            return "当前目标平台暂未提供可读取的群成员列表接口，或 Bot 无法读取该群成员。"
        except Exception as e:
            return f"获取目标群成员失败：{e}"

    @filter.llm_tool("send_cross_message")
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
    ):
        """
        【目标 QQ 会话 LLM 唤醒/委派工具】把一项自然语言任务投递到指定 QQ 群或私聊，
        让目标会话自己的 LLM 在对应上下文里醒来并处理。

        重要：这是通用的跨会话委派入口。用户想让你“去另一个群或私聊里做点什么”时优先考虑本工具，
        包括传话、打小报告、转述当前会话发生的事、请目标会话回应、让目标 Bot 处理会话事务、
        解禁/禁言/查询/提醒/协调等需要目标会话自己判断和执行的任务。

        选择边界：
        - 只是单向发送一段确定内容，不需要目标会话 LLM 判断或回应时，用 send_cross_message。
        - 需要目标 LLM 结合目标会话上下文、工具和权限来处理时，用本工具。
        - task 必须写清楚要交给目标 LLM 的完整任务；跨会话信息、要转述的话、打小报告的内容、
          请求者希望目标会话怎么处理，都应直接写进 task，不能假设目标 LLM 能看到当前会话全文。

        典型使用场景：
        1. 用户说“去群 984252223 解禁我”：target_id='984252223', task='帮请求者解除禁言'。
        2. 用户说“去群 984252223 禁言我 60 秒”：target_id='984252223', task='禁言请求者 60 秒'。
        3. 用户说“去那个群打个小报告，说刚才 A 又在阴阳怪气”：target_id='目标群号', task='向当前目标群打小报告：刚才 A 又在阴阳怪气，请你根据目标群语境自然回应。'。
        4. 用户说“去群里问问他们明天几点集合”：target_id='目标群号', task='询问当前目标群成员明天几点集合，并等待他们回应。'。
        5. 用户说“把姐姐刚才的话转给群里，让他们自己看着办”：target_id='目标群号', task='向当前目标群转述：<姐姐刚才的话>。请根据当前目标群语境自然处理。'。
        6. 用户说“去私聊问姐姐怎么看”：target_type='FriendMessage', target_id='<姐姐QQ>', task='请根据当前私聊上下文回应：你怎么看这件事？'。

        Args:
            target_id (str): 目标 QQ 群号或好友 QQ。群目标必须在白名单中；私聊目标遵循私聊安全配置。
            task (str): 要交给目标会话 LLM 执行的自然语言任务，需保留用户原意和必要上下文。
            target_type (str): 目标会话类型。支持 GroupMessage 和 FriendMessage，默认 GroupMessage。
            target_platform (str): 可选。QQ 平台 ID。默认使用 default_platform；未配置时尝试使用当前 QQ 平台。
        """
        try:
            result = await self._safe_wake_qq_session_task(
                event,
                target_id=target_id,
                task=task,
                target_type=target_type,
                target_platform=target_platform,
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
        if self.guarantee_threshold <= 0:
            return

        event_key = self._event_key(event)
        count = self.no_share_counts.get(event_key, 0) + 1
        self.no_share_counts[event_key] = count
        if count != self.guarantee_threshold:
            return

        prompt = self._build_guarantee_prompt(count)
        req.system_prompt = f"{req.system_prompt}\n\n{prompt}" if req.system_prompt else prompt
        logger.info(f"已为会话 {event_key} 触发一次保底提示，提示 Bot 自主决定是否调用 send_cross_message")
