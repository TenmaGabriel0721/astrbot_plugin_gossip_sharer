import json
import os
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


PLUGIN_VERSION = "1.3.0"


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
        self.attempt_target_llm_trigger = bool(
            self.config.get("attempt_target_llm_trigger", False)
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
            f"保底阈值: {self.guarantee_threshold}，任意私聊目标: {self.enable_arbitrary_friend_targets}"
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

    def _normalize_base64_data_uri(self, image_base64: str) -> str:
        data = image_base64.strip()
        if data.startswith("data:image/"):
            return data
        if data.startswith("base64://"):
            data = data.removeprefix("base64://")
        return f"data:image/jpeg;base64,{data}"

    def _build_image_context_parts(
        self,
        image_url: str | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
    ) -> tuple[list[dict], list[str]]:
        parts = []
        notes = []
        if image_url:
            parts.append({"type": "image_url", "image_url": {"url": image_url.strip()}})
        if image_base64:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._normalize_base64_data_uri(image_base64)},
                }
            )
        if image_path:
            abs_path = os.path.abspath(image_path.strip())
            parts.append({"type": "image_url", "image_url": {"url": f"file:///{abs_path}"}})
            notes.append(f"本地图片路径: {abs_path}")
        return parts, notes

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
        image_parts, image_notes = self._build_image_context_parts(image_url, image_path, image_base64)
        at_note = self._format_at_note(at_qqs, at_all)
        target_note = f"目标会话: {session_id}"
        if at_note:
            target_note += f"\n目标提及: {at_note}"
        image_note = ""
        if image_parts:
            image_note = f"\n图片数量: {len(image_parts)}"
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
        if image_parts:
            user_content = [{"type": "text", "text": bridge_text}, *image_parts]
        else:
            user_content = bridge_text
        user_message = {
            "role": "user",
            "content": user_content,
        }
        assistant_message = {
            "role": "assistant",
            "content": (
                "我已收到这条来自其他会话的转述消息。"
                "后续如果当前会话有人回复，应把它理解为对上面这条转述内容的继续回应，而不是一条完全无上下文的新话题。"
            ),
        }
        return user_message, assistant_message

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

    async def _try_trigger_target_llm(self, session_id: str) -> None:
        if not self.attempt_target_llm_trigger:
            return
        logger.warning(
            "已开启 attempt_target_llm_trigger，但当前版本未默认注入跨平台模拟事件；"
            f"已完成消息发送与上下文持久化，目标会话: {session_id}"
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

        await self._try_trigger_target_llm(session_id)
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

    @filter.llm_tool("get_available_groups")
    async def get_groups(self, event: AstrMessageEvent):
        """
        获取 Bot 当前可感知到的群聊列表，并标注哪些群在白名单中可用于转发。
        """
        try:
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

    @filter.on_llm_request()
    async def auto_share_logic(self, event: AstrMessageEvent, req: ProviderRequest):
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
